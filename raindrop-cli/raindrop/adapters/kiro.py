"""raindrop/adapters/kiro.py

Kiro CLI adapter (event-capture style).

Kiro keeps chat history in an undocumented SQLite database, so instead of
tailing a transcript we spool hook events locally and build exchanges from
that. Hooks installed:
  UserPromptSubmit -> spool the prompt
  PreToolUse       -> spool a tool summary + file paths
  Stop             -> spool the final assistant text, then sync completed turns
Each hook runs `raindrop sync --tool kiro --fail-open`. sync_cmd.py calls
capture() for every event, then read_new() and pushes.

Install side (all under ~/.kiro, the global scope):
  - MCP:   merge into settings/mcp.json
  - Hooks: own file hooks/raindrop.json (CLI V3 standalone hook format)
  - Rules: own file steering/raindrop.md (only with rules=True)

Spool: ~/.raindrop/spool/kiro/<session_id>.jsonl, one JSON object per line
(SpoolRecord in models.py; tool records add event="tool" and "files"). The
cursor is a byte offset into that file.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import time
from hashlib import sha1
from pathlib import Path

from raindrop import constants
from raindrop.adapters._common import (
    RULES_BODY,
    apply_file_edit,
    delete_file,
    diff,
    dump_json,
    edit_file,
    exe,
    load_json_text,
    summarize_tool,
)
from raindrop.adapters.base import Adapter, Change, SessionRef
from raindrop.errors import TranscriptError
from raindrop.models import Cursor, Exchange, ReadResult, ToolRecord

# Hook trigger names (CLI V3 spelling) we install, and the event each maps to.
_TRIGGERS = {
    "UserPromptSubmit": "Record your prompt so the finished turn can be shared with your team",
    "PreToolUse": "Record tool use (names and file paths only) for team context",
    "Stop": "Sync the finished turn to your team",
}


def kiro_dir() -> Path:
    return Path.home() / ".kiro"


def mcp_path() -> Path:
    return kiro_dir() / "settings" / "mcp.json"


def hooks_path() -> Path:
    return kiro_dir() / "hooks" / "raindrop.json"


def steering_path() -> Path:
    return kiro_dir() / "steering" / "raindrop.md"


def spool_path(session_id: str) -> Path:
    return constants.raindrop_home() / "spool" / "kiro" / f"{session_id}.jsonl"


# ---------------------------------------------------------------------------
# Pure transforms
# ---------------------------------------------------------------------------


def _add_mcp(text: str, path: Path) -> str:
    data = load_json_text(text, path)
    data.setdefault("mcpServers", {})[constants.MCP_SERVER_NAME] = {
        "command": exe(),
        "args": ["proxy"],
        "env": {},
    }
    return dump_json(data)


def _remove_mcp(text: str, path: Path) -> str:
    if not text.strip():
        return text
    data = load_json_text(text, path)
    servers = data.get("mcpServers")
    if isinstance(servers, dict):
        servers.pop(constants.MCP_SERVER_NAME, None)
    return dump_json(data)


def _hooks_text() -> str:
    command = f"{shlex.quote(exe())} sync --tool kiro --fail-open"
    hooks = [
        {
            "name": f"raindrop-{trigger.lower()}",
            "description": description,
            "trigger": trigger,
            "action": {"type": "command", "command": command},
            "timeout": 30,
        }
        for trigger, description in _TRIGGERS.items()
    ]
    return dump_json({"version": "v1", "hooks": hooks})


def _rules_text() -> str:
    return f"{constants.RULES_START}\n{RULES_BODY}\n{constants.RULES_END}\n"


# ---------------------------------------------------------------------------
# Event capture
# ---------------------------------------------------------------------------


def _event_kind(payload: dict) -> str | None:
    name = str(payload.get("hook_event_name", "")).replace("_", "").lower()
    return {
        "userpromptsubmit": "prompt",
        "promptsubmit": "prompt",
        "pretooluse": "tool",
        "stop": "stop",
    }.get(name)


def _session_id(payload: dict) -> str:
    sid = payload.get("session_id")
    if isinstance(sid, str) and sid.strip():
        return re.sub(r"[^A-Za-z0-9._:-]", "_", sid.strip()).replace("..", "_")[:128]
    # Older CLI builds send no session_id; fall back to one session per directory.
    cwd = str(payload.get("cwd") or "")
    return "kiro-" + sha1(cwd.encode()).hexdigest()[:12]


def _append(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (json.dumps(record) + "\n").encode())
    finally:
        os.close(fd)


class _Draft:
    def __init__(self, start: int, ts: int, text: str):
        self.start, self.ts, self.user_text = start, ts, text
        self.tools: list[str] = []
        self.files: list[str] = []


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class KiroAdapter(Adapter):
    name = "kiro"
    display_name = "Kiro CLI"

    # ---- install side -----------------------------------------------------

    def detect(self) -> bool:
        return shutil.which("kiro-cli") is not None or kiro_dir().is_dir()

    def plan_install(self, rules: bool) -> list[Change]:
        changes = [self._plan_mcp(), self._plan_hooks()]
        if rules:
            changes.append(self._plan_rules())
        return changes

    def _plan_mcp(self) -> Change:
        path = mcp_path()
        old, new = edit_file(path, lambda t: _add_mcp(t, path))
        return Change(
            kind="merge",
            target=str(path),
            description=f"Add '{constants.MCP_SERVER_NAME}' MCP server to {path}",
            preview=diff(path, old, new) or "(already installed, no change)",
            component="mcp",
            _apply=lambda: apply_file_edit(path, "mcp", lambda t: _add_mcp(t, path)),
        )

    def _plan_hooks(self) -> Change:
        path = hooks_path()
        new = _hooks_text()
        old, _ = edit_file(path, lambda t: new)
        return Change(
            kind="create",
            target=str(path),
            description="Add hooks that capture each turn and sync it to your team",
            preview=diff(path, old, new) or "(already installed, no change)",
            component="hook",
            _apply=lambda: apply_file_edit(path, "hook", lambda t: _hooks_text()),
        )

    def _plan_rules(self) -> Change:
        path = steering_path()
        new = _rules_text()
        old, _ = edit_file(path, lambda t: new)
        return Change(
            kind="create",
            target=str(path),
            description="Add a Raindrop steering file telling Kiro to use team context",
            preview=diff(path, old, new) or "(already installed, no change)",
            component="rules",
            _apply=lambda: apply_file_edit(path, "rules", lambda t: _rules_text()),
        )

    def uninstall(self, record: ToolRecord) -> None:
        if record.get("mcp"):
            path = mcp_path()
            apply_file_edit(path, "mcp", lambda t: _remove_mcp(t, path))
        if record.get("hook"):
            delete_file(hooks_path(), "hook")
        if record.get("rules"):
            delete_file(steering_path(), "rules")

    # ---- extraction side --------------------------------------------------

    def session_from_hook(self, payload: dict) -> SessionRef:
        return SessionRef(
            session_id=_session_id(payload),
            transcript_path=None,
            cwd=payload.get("cwd") if isinstance(payload.get("cwd"), str) else None,
        )

    def capture(self, payload: dict) -> None:
        kind = _event_kind(payload)
        if kind is None:
            return
        record: dict = {"ts": int(time.time()), "session_id": _session_id(payload)}
        if kind == "prompt":
            text = payload.get("prompt") or payload.get("user_prompt") or os.environ.get("USER_PROMPT")
            record.update(event="prompt", text=text if isinstance(text, str) else "")
        elif kind == "tool":
            summary, files = summarize_tool(payload.get("tool_name"), payload.get("tool_input"))
            record.update(event="tool", text=summary, files=files)
        else:
            text = payload.get("assistant_response")
            record.update(event="stop", text=text if isinstance(text, str) else "")
        if kind == "prompt" and not record["text"].strip():
            return
        _append(spool_path(record["session_id"]), record)

    def read_new(self, session: SessionRef, cursor: Cursor) -> ReadResult:
        path = spool_path(session.session_id)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return ReadResult(exchanges=[], new_cursor=cursor)
        except OSError as e:
            raise TranscriptError(f"Could not read spool {path}: {e}") from e
        if cursor > len(data):
            cursor = 0

        # Scan from byte 0 so idx (ordinal of completed exchange) is stable.
        done: list[tuple[int, Exchange]] = []  # (start offset, exchange)
        draft: _Draft | None = None
        consumed = 0
        pos = 0
        while pos < len(data):
            nl = data.find(b"\n", pos)
            if nl == -1:
                break  # partial line mid-write
            line, start, pos = data[pos:nl], pos, nl + 1
            consumed = pos
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            event, text = rec.get("event"), rec.get("text")
            text = text if isinstance(text, str) else ""
            if event == "prompt":
                # A new prompt with no Stop in between means the earlier turn
                # was interrupted; drop it.
                draft = _Draft(start, int(rec.get("ts") or 0), text)
            elif event == "tool" and draft:
                draft.tools.append(text)
                files = rec.get("files")
                for f in files if isinstance(files, list) else []:
                    if isinstance(f, str) and f not in draft.files:
                        draft.files.append(f)
            elif event == "stop" and draft:
                if text.strip():
                    done.append((draft.start, Exchange(
                        session_id=session.session_id,
                        idx=len(done),
                        ts=draft.ts,
                        user_text=draft.user_text,
                        assistant_text=text.strip(),
                        tool_summaries=draft.tools,
                        files_touched=draft.files,
                    )))
                draft = None

        # An open draft is a turn still in progress: hold it back.
        new_cursor = draft.start if draft else consumed
        exchanges = [ex for start, ex in done if start >= cursor]
        return ReadResult(exchanges=exchanges, new_cursor=max(new_cursor, cursor))
