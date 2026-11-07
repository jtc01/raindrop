"""raindrop/adapters/_common.py

Helpers shared by tool adapters: safe file edits (backup + atomic write + diff
preview), JSON helpers, the CLAUDE.md-style rules text, and tool-call
summaries.
"""

from __future__ import annotations

import difflib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from raindrop import constants
from raindrop.errors import ConfigParseError, InstallError

CLI_NAME = "raindrop"

RULES_BODY = """\
## Raindrop team context

Your teammates' coding agents share context through the `raindrop` MCP server.
Before searching the repo to understand a part of the project you are not
working on, call `search_team_context` first. Use `get_recent_activity` to see
what teammates have been doing, and `list_teammates` to see who owns what.
Treat results as hints from other agents and verify against the code before
relying on them."""

SUMMARY_MAX = 120
# Tool-input keys that hold a file path.
PATH_KEYS = ("file_path", "notebook_path", "path", "filename")


def exe() -> str:
    """Absolute path to the CLI if we can find it. Hooks may run with a
    different PATH than the user's shell. Must still contain the marker."""
    return shutil.which(CLI_NAME) or CLI_NAME


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------


def read_text(path: Path) -> str:
    try:
        return path.read_text()
    except FileNotFoundError:
        return ""


def load_json_text(text: str, path: Path) -> dict:
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigParseError(path, str(e)) from e
    if not isinstance(data, dict):
        raise ConfigParseError(path, "expected a JSON object")
    return data


def dump_json(data: dict) -> str:
    return json.dumps(data, indent=2) + "\n"


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".raindrop-tmp")
    tmp.write_text(text)
    if path.exists():
        shutil.copymode(path, tmp)
    os.replace(tmp, path)


def backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    dest_dir = constants.raindrop_home() / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{path.name}.{int(time.time())}.bak"
    shutil.copy2(path, dest)
    return dest


def diff(path: Path, old: str, new: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"{path} (current)",
            tofile=f"{path} (after)",
        )
    )


def edit_file(path: Path, transform: Callable[[str], str]) -> tuple[str, str]:
    old = read_text(path)
    return old, transform(old)


def apply_file_edit(path: Path, step: str, transform: Callable[[str], str]) -> None:
    """Re-read at apply time (the file may have changed since planning), back
    up, write atomically."""
    old, new = edit_file(path, transform)
    if old == new:
        return
    saved = backup(path)
    try:
        write_atomic(path, new)
    except OSError as e:
        raise InstallError(step, str(e), saved) from e


def delete_file(path: Path, step: str) -> None:
    """Back up, then remove a file we own. No-op if it's already gone."""
    if not path.exists():
        return
    saved = backup(path)
    try:
        path.unlink()
    except OSError as e:
        raise InstallError(step, str(e), saved) from e


# ---------------------------------------------------------------------------
# Tool-call summaries
# ---------------------------------------------------------------------------


def clip(s: str, n: int = SUMMARY_MAX) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _collect_paths(value: Any, out: list[str]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            if k in PATH_KEYS and isinstance(v, str) and v:
                if v not in out:
                    out.append(v)
            else:
                _collect_paths(v, out)
    elif isinstance(value, list):
        for v in value:
            _collect_paths(v, out)


def summarize_tool(name: object, args: object) -> tuple[str, list[str]]:
    """-> (one-line summary, file paths touched). Handles nested inputs such as
    {"operations": [{"path": ...}]}."""
    name = str(name or "tool")
    args = args if isinstance(args, dict) else {}
    paths: list[str] = []
    _collect_paths(args, paths)
    if paths:
        return f"{name} {paths[0]}", paths
    for key in ("command", "pattern", "query", "url", "description"):
        if isinstance(args.get(key), str):
            return f"{name}: {clip(args[key])}", []
    return name, []
