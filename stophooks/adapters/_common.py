"""Shared loading and file helpers for isolated stop-hook adapters."""

from __future__ import annotations

import difflib
import json
import os
import sys
from pathlib import Path
from typing import Callable

CLI_DIR = Path(__file__).resolve().parents[2] / "raindrop-cli"
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from raindrop.adapters.base import Adapter, Change, SessionRef
from raindrop.errors import ConfigParseError, TranscriptError
from raindrop.models import Cursor, Exchange, ReadResult, ToolRecord


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigParseError(path, str(exc)) from exc
    if not isinstance(value, dict):
        raise ConfigParseError(path, "expected a JSON object")
    return value


def dump_json(value: dict) -> str:
    return json.dumps(value, indent=2) + "\n"


def diff(path: Path, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"{path} (current)",
            tofile=f"{path} (after)",
        )
    )


def json_change(
    path: Path,
    description: str,
    transform: Callable[[dict], dict],
) -> Change:
    old_text = path.read_text(encoding="utf-8") if path.exists() else ""
    old_data = load_json(path)
    new_text = dump_json(transform(old_data))

    def apply() -> None:
        data = load_json(path)
        updated = dump_json(transform(data))
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".raindrop-tmp")
        try:
            temporary.write_text(updated, encoding="utf-8")
            if path.exists():
                os.chmod(temporary, path.stat().st_mode)
            os.replace(temporary, path)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    return Change(
        kind="merge",
        target=str(path),
        description=description,
        preview=diff(path, old_text, new_text) or "(already installed, no change)",
        component="hook",
        _apply=apply,
    )


def validate_session_id(value: object, tool: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-" for char in value)
        or ".." in value
    ):
        raise TranscriptError(f"Hook payload has no valid {tool} session ID")
    return value


__all__ = [
    "Adapter",
    "Change",
    "Cursor",
    "Exchange",
    "ReadResult",
    "SessionRef",
    "ToolRecord",
    "TranscriptError",
    "diff",
    "dump_json",
    "json_change",
    "load_json",
    "validate_session_id",
]
