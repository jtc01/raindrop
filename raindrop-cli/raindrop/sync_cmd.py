"""`raindrop sync`: push new exchanges from a tool's transcript to the team.

Run by the tool's Stop hook (hook JSON on stdin). Works for any adapter in
raindrop/adapters/registry.py; the hook passes `--tool <name>`.

Cursor handling: one file per session at ~/.raindrop/cursors/<tool>/<session_id>
holding the adapter's opaque cursor. It only advances after the API confirms
every batch, so a failed push is retried next turn. Exchange ids are
deterministic, so a resend overwrites rather than duplicates.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import httpx
import typer

from raindrop import api_client
from raindrop import config
from raindrop.redact import load_env_secrets, redact_obj
from raindrop.adapters.base import Adapter
from raindrop.adapters.registry import get_adapter
from raindrop.constants import BATCH_SIZE
from raindrop.errors import NotJoinedError, RaindropError

SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _cursor_path(tool: str, session_id: str) -> Path:
    if not SAFE_SESSION_ID.fullmatch(session_id) or ".." in session_id:
        raise RaindropError(f"Refusing unsafe session_id: {session_id!r}")
    # ":" is legal in session ids but not in Windows filenames.
    return config.CONFIG_DIR / "cursors" / tool / session_id.replace(":", "_")


def _load_cursor(path: Path) -> int:
    try:
        return int(path.read_text().strip())
    except FileNotFoundError:
        return 0
    except (OSError, ValueError) as e:
        raise RaindropError(f"Sync cursor is unreadable: {path}") from e


def _save_cursor(path: Path, cursor: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(str(cursor))
    tmp.replace(path)


def run_sync(payload: dict, tool: str, transport: httpx.BaseTransport | None = None) -> int:
    """Sync one hook event. Returns the number of exchanges pushed."""
    cfg = config.load()
    if not cfg:
        raise NotJoinedError()
    adapter = get_adapter(tool)
    # Event-capture tools (e.g. Kiro) spool every hook event before we read.
    if type(adapter).capture is not Adapter.capture:
        adapter.capture(payload)
    session = adapter.session_from_hook(payload)
    cursor_file = _cursor_path(tool, session.session_id)
    cursor = _load_cursor(cursor_file)

    result = adapter.read_new(session, cursor)

    if not config.should_share(session.cwd, cfg):
        # Paused, or opt-in mode and this repo isn't shared: advance past the
        # content without uploading so it is skipped for good, never queued.
        if result.new_cursor != cursor:
            _save_cursor(cursor_file, result.new_cursor)
        return 0

    secrets = [cfg["api_key"]]
    if session.cwd:
        secrets += load_env_secrets(Path(session.cwd))
    wire = [redact_obj(e.to_wire(), known_secrets=secrets) for e in result.exchanges]

    for i in range(0, len(wire), BATCH_SIZE):
        batch = wire[i : i + BATCH_SIZE]
        resp = api_client._post(
            cfg["api_url"], "/messages", {"exchanges": batch},
            api_key=cfg["api_key"], transport=transport,
        )
        if resp.get("accepted") != len(batch):
            raise RaindropError("API did not confirm the full batch was stored.")

    if result.new_cursor != cursor:
        _save_cursor(cursor_file, result.new_cursor)
    return len(wire)


def _log_failure(message: str) -> None:
    try:
        config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with (config.CONFIG_DIR / "sync.log").open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def _hook_reply(tool: str) -> None:
    """Print the tool's required hook answer, if it has one (see Adapter.hook_stdout)."""
    try:
        reply = get_adapter(tool).hook_stdout()
    except Exception:
        return
    if reply:
        typer.echo(reply)


def sync(
    tool: str = typer.Option("claude-code", "--tool", help="Which tool's hook is calling."),
    fail_open: bool = typer.Option(
        False, "--fail-open",
        help="Always exit 0 and log errors to ~/.raindrop/sync.log. Needed for hooks "
             "that run before a prompt or tool call, where a non-zero exit blocks the tool.",
    ),
):
    """Sync new exchanges to your team. Reads the hook's JSON payload from stdin.

    Output goes to stderr only (some tools add a hook's stdout to the agent's context), except
    for the answer a tool requires on stdout (Adapter.hook_stdout).
    """
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise RaindropError("Hook input must be a JSON object.")
        n = run_sync(payload, tool)
    except (api_client.ApiError, RaindropError, json.JSONDecodeError, OSError) as e:
        message = getattr(e, "message", None) or str(e)
        _log_failure(f"{tool}: {message}")
        typer.secho(f"raindrop sync failed: {message}", fg="red", err=True)
        if fail_open:
            _hook_reply(tool)
        raise typer.Exit(0 if fail_open else 1)
    _hook_reply(tool)
    if n:
        typer.echo(f"raindrop synced {n} exchange{'' if n == 1 else 's'}.", err=True)
