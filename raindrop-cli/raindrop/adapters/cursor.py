"""Cursor adapter prototype using hook-event spooling."""

from __future__ import annotations

import json
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from raindrop.adapters._common import json_change, repo_relative_files, validate_session_id
from raindrop.adapters.base import Adapter, Change, SessionRef
from raindrop.errors import TranscriptError
from raindrop.models import Cursor, Exchange, ReadResult, ToolRecord
from raindrop import config

HOOK_MARKER = "raindrop sync --tool cursor"
CAPTURE_EVENTS = ("beforeSubmitPrompt", "postToolUse", "afterAgentResponse", "stop")


def _cursor_dir() -> Path:
    return Path.home() / ".cursor"


def _hooks_path() -> Path:
    return _cursor_dir() / "hooks.json"


def _spool_path(session_id: str) -> Path:
    return config.CONFIG_DIR / "spools" / "cursor" / f"{session_id.replace(':', '_')}.jsonl"


def _hook_transform(data: dict, install: bool) -> dict:
    data.setdefault("version", 1)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Cursor hooks config 'hooks' must be an object")
    for event in CAPTURE_EVENTS:
        handlers = hooks.get(event, [])
        if not isinstance(handlers, list):
            raise ValueError(f"Cursor hooks config 'hooks.{event}' must be an array")
        handlers = [
            item for item in handlers
            if not (
                isinstance(item, dict)
                and HOOK_MARKER in str(item.get("command", ""))
            )
        ]
        if install:
            handlers.append({"command": HOOK_MARKER, "timeout": 60})
        if handlers:
            hooks[event] = handlers
        else:
            hooks.pop(event, None)
    if not hooks:
        data.pop("hooks", None)
    return data


def _timestamp(value: object) -> int:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    return int(time.time())


def _paths(value: Any) -> list[str]:
    """Raw path strings from a tool call, spooled as-is. read_new() makes them
    repo-relative (see _common.repo_relative_files)."""
    if not isinstance(value, dict):
        return []
    result: list[str] = []
    for key in ("file_path", "path", "filename", "notebook_path"):
        item = value.get(key)
        if isinstance(item, str) and item and item not in result:
            result.append(item)
    return result


class CursorAdapter(Adapter):
    """Normalize Cursor prompt/response/tool hooks into common exchanges."""

    name = "cursor"
    display_name = "Cursor"

    def detect(self) -> bool:
        return shutil.which("cursor") is not None or _cursor_dir().is_dir()

    def plan_install(self, rules: bool) -> list[Change]:
        del rules
        return [json_change(
            _hooks_path(),
            "Add Cursor hooks to capture prompts, responses, tools, and stop events",
            lambda data: _hook_transform(data, install=True),
        )]

    def uninstall(self, record: ToolRecord) -> None:
        if record.get("hook"):
            change = json_change(
                _hooks_path(),
                "Remove Raindrop Cursor hooks",
                lambda data: _hook_transform(data, install=False),
            )
            change.run()

    def session_from_hook(self, payload: dict) -> SessionRef:
        session_id = validate_session_id(payload.get("conversation_id"), self.display_name)
        # run_sync() calls capture() itself; capturing here too would spool every event twice.
        cwd = payload.get("workspace_roots") or payload.get("cwd")
        if isinstance(cwd, list):
            cwd = cwd[0] if cwd and isinstance(cwd[0], str) else None
        return SessionRef(
            session_id=session_id,
            transcript_path=None,
            cwd=cwd if isinstance(cwd, str) else None,
        )

    def capture(self, payload: dict) -> None:
        session_id = validate_session_id(payload.get("conversation_id"), self.display_name)
        event = payload.get("hook_event_name") or payload.get("event")
        if not isinstance(event, str):
            raise TranscriptError("Cursor hook payload has no hook_event_name")
        cwd = payload.get("workspace_roots") or payload.get("cwd")
        if isinstance(cwd, list):
            cwd = cwd[0] if cwd and isinstance(cwd[0], str) else None
        cwd_path = Path(cwd) if isinstance(cwd, str) else Path.cwd()

        record: dict[str, Any] = {
            "event": event,
            "ts": _timestamp(payload.get("timestamp")),
        }
        if event == "beforeSubmitPrompt":
            text = payload.get("prompt")
            if not isinstance(text, str) or not text.strip():
                raise TranscriptError("Cursor beforeSubmitPrompt payload has no prompt text")
            record.update({"text": text, "cwd": str(cwd_path)})
        elif event == "afterAgentResponse":
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip():
                raise TranscriptError("Cursor afterAgentResponse payload has no response text")
            record["text"] = text
        elif event == "postToolUse":
            name = payload.get("tool_name")
            if not isinstance(name, str) or not name:
                raise TranscriptError("Cursor postToolUse payload has no tool_name")
            record["tool_name"] = name
            record["files_touched"] = _paths(payload.get("tool_input"))
        elif event != "stop":
            return

        path = _spool_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
        except OSError as exc:
            raise TranscriptError(f"Could not spool Cursor hook data: {path}") from exc

    def read_new(self, session: SessionRef, cursor: Cursor) -> ReadResult:
        path = _spool_path(session.session_id)
        if not path.is_file():
            return ReadResult([], cursor)
        data = path.read_bytes()
        if cursor < 0:
            raise TranscriptError("Cursor sync cursor must be non-negative")
        if cursor > len(data):
            cursor = 0

        records: list[tuple[int, dict]] = []
        pos = 0
        consumed = 0
        while pos < len(data):
            newline = data.find(b"\n", pos)
            if newline < 0:
                break
            line_start = pos
            line = data[pos:newline]
            pos = newline + 1
            consumed = pos
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise TranscriptError(f"Invalid Cursor spool record at byte {line_start}") from exc
            if not isinstance(value, dict):
                raise TranscriptError(f"Unexpected Cursor spool record at byte {line_start}")
            records.append((line_start, value))

        drafts: list[dict[str, Any]] = []
        for offset, record in records:
            event = record.get("event")
            if event == "beforeSubmitPrompt":
                drafts.append({
                    "start": offset,
                    "ts": record["ts"],
                    "user_text": record["text"],
                    "assistant_text": "",
                    "tools": [],
                    "files": [],
                    "complete": False,
                })
            elif event == "postToolUse" and drafts:
                name = record.get("tool_name")
                if isinstance(name, str) and name not in drafts[-1]["tools"]:
                    drafts[-1]["tools"].append(name)
                for file_path in record.get("files_touched", []):
                    if file_path not in drafts[-1]["files"]:
                        drafts[-1]["files"].append(file_path)
            elif event == "afterAgentResponse" and drafts:
                drafts[-1]["assistant_text"] = record["text"]
                drafts[-1]["complete"] = True
            elif event == "stop" and drafts and drafts[-1]["assistant_text"]:
                drafts[-1]["complete"] = True

        exchanges = [
            Exchange(
                session_id=session.session_id,
                idx=idx,
                ts=draft["ts"],
                user_text=draft["user_text"],
                assistant_text=draft["assistant_text"],
                tool_summaries=draft["tools"],
                files_touched=repo_relative_files(draft["files"], session),
            )
            for idx, draft in enumerate(drafts)
            if draft["complete"] and draft["start"] >= cursor
        ]
        incomplete = next(
            (draft["start"] for draft in drafts if not draft["complete"]),
            None,
        )
        if incomplete is not None:
            exchanges = [
                exchange for exchange in exchanges
                if drafts[exchange.idx]["start"] < incomplete
            ]
        new_cursor = incomplete if incomplete is not None else consumed
        return ReadResult(exchanges, max(cursor, new_cursor))
