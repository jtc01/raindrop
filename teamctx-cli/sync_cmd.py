"""`raindrop sync`: push new exchanges from a tool's transcript to the team.

Run by the tool's Stop hook (hook JSON on stdin). Works for any adapter in
teamctx/adapters/registry.py; the hook passes `--tool <name>`.

Cursor handling: one file per session at ~/.raindrop/cursors/<tool>/<session_id>
holding the adapter's opaque cursor. It only advances after the API confirms
every batch, so a failed push is retried next turn. Exchange ids are
deterministic, so a resend overwrites rather than duplicates.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import httpx
import typer

import api_client
import config
from redact import load_env_secrets, redact_obj
from teamctx.adapters.registry import get_adapter
from teamctx.constants import BATCH_SIZE
from teamctx.errors import NotJoinedError, TeamctxError

SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _cursor_path(tool: str, session_id: str) -> Path:
    if not SAFE_SESSION_ID.fullmatch(session_id) or ".." in session_id:
        raise TeamctxError(f"Refusing unsafe session_id: {session_id!r}")
    return config.CONFIG_DIR / "cursors" / tool / session_id


def _load_cursor(path: Path) -> int:
    try:
        return int(path.read_text().strip())
    except FileNotFoundError:
        return 0
    except (OSError, ValueError) as e:
        raise TeamctxError(f"Sync cursor is unreadable: {path}") from e


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
    session = adapter.session_from_hook(payload)
    cursor_file = _cursor_path(tool, session.session_id)
    cursor = _load_cursor(cursor_file)

    result = adapter.read_new(session, cursor)

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
            raise TeamctxError("API did not confirm the full batch was stored.")

    if result.new_cursor != cursor:
        _save_cursor(cursor_file, result.new_cursor)
    return len(wire)


def sync(
    tool: str = typer.Option("claude-code", "--tool", help="Which tool's hook is calling."),
):
    """Sync new exchanges to your team. Reads the hook's JSON payload from stdin."""
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise TeamctxError("Hook input must be a JSON object.")
        n = run_sync(payload, tool)
    except api_client.ApiError as e:
        typer.secho(f"raindrop sync failed: {e.message}", fg="red", err=True)
        raise typer.Exit(1)
    except (TeamctxError, json.JSONDecodeError, OSError) as e:
        typer.secho(f"raindrop sync failed: {e}", fg="red", err=True)
        raise typer.Exit(1)
    typer.echo(f"raindrop synced {n} exchange{'' if n == 1 else 's'}.")
