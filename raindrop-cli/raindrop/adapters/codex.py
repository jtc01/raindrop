"""Codex adapter: reads Codex transcripts via its Stop hook."""

from __future__ import annotations

import json
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from raindrop.adapters._common import json_change, repo_relative_files, validate_session_id, newest_session_in_repo, jsonl_head
from raindrop.adapters.base import Adapter, Change, SessionRef
from raindrop.errors import TranscriptError
from raindrop.models import Cursor, Exchange, ReadResult, ToolRecord

HOOK_MARKER = "raindrop sync --tool codex"
_UUID_TAIL = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _codex_dir() -> Path:
    return Path.home() / ".codex"


def _hooks_path() -> Path:
    return _codex_dir() / "hooks.json"


def _session_root() -> Path:
    return _codex_dir() / "sessions"


def _hook_transform(data: dict, install: bool) -> dict:
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Codex hooks config 'hooks' must be an object")
    groups = hooks.get("Stop", [])
    if not isinstance(groups, list):
        raise ValueError("Codex hooks config 'hooks.Stop' must be an array")
    groups = [
        group for group in groups
        if not (
            isinstance(group, dict)
            and any(
                isinstance(handler, dict)
                and HOOK_MARKER in str(handler.get("command", ""))
                for handler in group.get("hooks", [])
            )
        )
    ]
    if install:
        groups.append({
            "hooks": [{
                "type": "command",
                "command": HOOK_MARKER,
                "timeout": 60,
            }]
        })
    if groups:
        hooks["Stop"] = groups
    else:
        hooks.pop("Stop", None)
    if not hooks:
        data.pop("hooks", None)
    return data


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    return "\n".join(
        item["text"].strip()
        for item in content
        if isinstance(item, dict)
        and item.get("type") in {"input_text", "output_text", "text"}
        and isinstance(item.get("text"), str)
        and item["text"].strip()
    )


def _epoch(value: object) -> int:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    return int(time.time())


def _path_arguments(arguments: object) -> list[str]:
    """Raw path strings from a tool call. Made repo-relative in _Draft.finish."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return []
    if not isinstance(arguments, dict):
        return []
    paths: list[str] = []
    for key in ("file_path", "path", "filename", "notebook_path"):
        value = arguments.get(key)
        if isinstance(value, str) and value and value not in paths:
            paths.append(value)
    return paths


class _Draft:
    def __init__(self, start: int, ts: int, user_text: str):
        self.start = start
        self.ts = ts
        self.user_text = user_text
        self.assistant_parts: list[str] = []
        self.after_tool: list[str] = []
        self.tools: list[str] = []
        self.files: list[str] = []

    def finish(self, session: SessionRef, idx: int) -> Exchange | None:
        answer = "\n\n".join(self.after_tool or self.assistant_parts).strip()
        if not self.user_text or not answer:
            return None
        return Exchange(
            session_id=session.session_id,
            idx=idx,
            ts=self.ts,
            user_text=self.user_text,
            assistant_text=answer,
            tool_summaries=self.tools,
            files_touched=repo_relative_files(self.files, session),
        )


def _record_data(record: dict) -> tuple[str | None, dict]:
    record_type = record.get("type")
    payload = record.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    if record_type == "event_msg" and payload.get("type") == "user_message":
        message = payload.get("message")
        return ("user", {"content": message}) if isinstance(message, str) else (None, payload)
    if record_type == "response_item" and payload.get("type") == "message":
        role = payload.get("role")
        return (role, payload) if role in {"user", "assistant"} else (None, payload)
    return None, payload


class CodexAdapter(Adapter):
    """Translate Codex JSONL transcript records to the common Exchange model."""

    name = "codex"
    display_name = "Codex"

    def detect(self) -> bool:
        return shutil.which("codex") is not None or _codex_dir().is_dir()

    def plan_install(self, rules: bool) -> list[Change]:
        del rules
        path = _hooks_path()
        return [json_change(
            path,
            "Add a Codex Stop hook for Raindrop sync",
            lambda data: _hook_transform(data, install=True),
        )]

    def uninstall(self, record: ToolRecord) -> None:
        if record.get("hook"):
            path = _hooks_path()
            change = json_change(
                path,
                "Remove the Raindrop Codex Stop hook",
                lambda data: _hook_transform(data, install=False),
            )
            change.run()

    def session_from_hook(self, payload: dict) -> SessionRef:
        session_id = validate_session_id(payload.get("session_id"), self.display_name)
        cwd = payload.get("cwd")
        transcript_value = payload.get("transcript_path")
        transcript = Path(transcript_value).expanduser() if isinstance(transcript_value, str) else None
        if transcript is None:
            root = _session_root()
            matches = list(root.rglob(f"*{session_id}*.jsonl")) if root.is_dir() else []
            if len(matches) != 1:
                raise TranscriptError(
                    f"Expected one Codex transcript for session {session_id}; found {len(matches)}"
                )
            transcript = matches[0]
        if not transcript.is_file():
            raise TranscriptError(f"Codex transcript not found: {transcript}")
        return SessionRef(
            session_id=session_id,
            transcript_path=transcript,
            cwd=cwd if isinstance(cwd, str) else None,
        )

    def latest_session(self, repo: Path) -> SessionRef | None:
        # Rollouts live at sessions/YYYY/MM/DD/rollout-<time>-<uuid>.jsonl and
        # open with a session_meta record carrying the id and cwd.
        def probe(path: Path) -> tuple[str, str] | None:
            session_id = cwd = None
            for record in jsonl_head(path):
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                if record.get("type") == "session_meta" and isinstance(payload.get("id"), str):
                    session_id = payload["id"]
                if cwd is None and isinstance(payload.get("cwd"), str):
                    cwd = payload["cwd"]
                if session_id and cwd:
                    break
            if session_id is None:
                m = _UUID_TAIL.search(path.stem)
                session_id = m.group(0) if m else None
            return (session_id, cwd) if session_id and cwd else None

        root = _session_root()
        files = root.rglob("*.jsonl") if root.is_dir() else []
        return newest_session_in_repo(files, probe, repo)

    def read_new(self, session: SessionRef, cursor: Cursor) -> ReadResult:
        path = session.transcript_path
        if path is None or not path.is_file():
            raise TranscriptError(f"Codex transcript not found for session {session.session_id}")
        data = path.read_bytes()
        if cursor < 0:
            raise TranscriptError("Codex sync cursor must be non-negative")
        if cursor > len(data):
            cursor = 0

        drafts: list[_Draft] = []
        consumed = 0
        pos = 0
        while pos < len(data):
            newline = data.find(b"\n", pos)
            if newline == -1:
                break
            start = pos
            line = data[pos:newline]
            pos = newline + 1
            consumed = pos
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise TranscriptError(f"Invalid Codex JSONL record at byte {start}") from exc
            if not isinstance(record, dict):
                raise TranscriptError(f"Unexpected Codex transcript record at byte {start}")

            role, payload = _record_data(record)
            if role == "user":
                prompt = _text(payload.get("content"))
                if prompt:
                    drafts.append(_Draft(start, _epoch(record.get("timestamp")), prompt))
            elif role == "assistant" and drafts:
                content = payload.get("content")
                if isinstance(content, list):
                    for item in content:
                        if not isinstance(item, dict):
                            continue
                        if item.get("type") in {"output_text", "text"}:
                            text = item.get("text")
                            if isinstance(text, str) and text.strip():
                                drafts[-1].assistant_parts.append(text.strip())
                                drafts[-1].after_tool.append(text.strip())
                        elif item.get("type") in {"tool_use", "function_call"}:
                            name = item.get("name")
                            if isinstance(name, str) and name:
                                drafts[-1].tools.append(name)
                            paths = _path_arguments(item.get("input") or item.get("arguments"))
                            for file_path in paths:
                                if file_path not in drafts[-1].files:
                                    drafts[-1].files.append(file_path)
                            drafts[-1].after_tool = []
                else:
                    text = _text(content)
                    if text:
                        drafts[-1].assistant_parts.append(text)
                        drafts[-1].after_tool.append(text)
            elif record.get("type") == "response_item" and payload.get("type") == "function_call":
                if drafts:
                    name = payload.get("name")
                    if isinstance(name, str) and name:
                        drafts[-1].tools.append(name)
                    paths = _path_arguments(payload.get("arguments"))
                    for file_path in paths:
                        if file_path not in drafts[-1].files:
                            drafts[-1].files.append(file_path)
                    drafts[-1].after_tool = []

        finalized = [draft.finish(session, idx) for idx, draft in enumerate(drafts)]
        held_start = next(
            (draft.start for draft, exchange in zip(drafts, finalized) if exchange is None),
            None,
        )
        exchanges: list[Exchange] = []
        for draft, exchange in zip(drafts, finalized):
            if exchange is None:
                continue
            if draft.start >= cursor and (held_start is None or draft.start < held_start):
                exchanges.append(exchange)

        new_cursor = held_start if held_start is not None else consumed
        return ReadResult(exchanges, max(cursor, new_cursor))
