"""raindrop/adapters/copilot.py

GitHub Copilot via the Copilot CLI's config directory ($COPILOT_HOME, default
~/.copilot). VS Code's docs describe the same `mcp-config.json` as the
"Copilot Global" destination; its own `mcp.json` (a different `servers` schema
at a per-OS path) is not written.

  - MCP:   <dir>/mcp-config.json, under `mcpServers`, `type: "local"`
  - Hook:  <dir>/hooks/raindrop.json, an `agentStop` hook that runs
           `raindrop sync --tool copilot --fail-open`. VS Code, the Copilot app
           and the CLI share this user-level hooks folder; the file is entirely
           ours, so uninstall deletes it.
  - Rules: a delimited block in <dir>/copilot-instructions.md (personal
           instructions, applied to every session; the file is the user's)

Copilot passes MCP servers only PATH from its own environment, so the entry
sets RAINDROP_HOME explicitly; the proxy needs it to find config.json.

Extraction side: tails the chat transcript the stop hook points at. VS Code
keeps it at <VS Code user dir>/workspaceStorage/<id>/GitHub.copilot-chat/
transcripts/<sessionId>.jsonl, one event per line:
  {"type": "user.message", "data": {"content": "prompt", "attachments": []}, "timestamp": ...}
  {"type": "assistant.message", "data": {"content": "reply", "toolRequests": [...],
                                          "reasoningText": "..."}}
  {"type": "tool.execution_start", "data": {"toolName": ..., "arguments": {...}}}
  {"type": "tool.execution_complete" | "assistant.turn_start" | "assistant.turn_end" | ...}
A prompt can span many assistant turns (a reply, tool calls, another reply...),
so the exchange's reply is the text after the last tool call. Reasoning text and
tool output are never shared. The cursor is a byte offset.

The record format above was checked against real VS Code transcripts. The
Copilot CLI may write a different transcript format; if a hook points at one the
parser doesn't recognise it finds no prompts and syncs nothing.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from raindrop import constants
from raindrop.adapters._common import (
    apply_file_edit,
    delete_file,
    diff,
    dump_json,
    edit_file,
    exe,
    quote_cmd,
    summarize_tool,
)
from raindrop.adapters._file_config import FileConfigAdapter
from raindrop.adapters.base import Change, SessionRef
from raindrop.errors import TranscriptError
from raindrop.models import Cursor, Exchange, ReadResult, ToolRecord

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.MULTILINE)


def copilot_dir() -> Path:
    override = os.environ.get("COPILOT_HOME")
    return Path(override).expanduser() if override else Path.home() / ".copilot"


def _vscode_user_dirs() -> list[Path]:
    """Where VS Code (stable and Insiders) keeps per-workspace storage."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return [base / name / "User" for name in ("Code", "Code - Insiders")]


def _to_epoch(ts: object) -> int:
    if isinstance(ts, str):
        try:
            return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    return int(time.time())


def _tool_paths_and_summary(name: object, args: object) -> tuple[str, list[str]]:
    args = args if isinstance(args, dict) else {}
    # Copilot spells the key filePath; the shared helper looks for file_path.
    normalized = {("file_path" if k == "filePath" else k): v for k, v in args.items()}
    summary, paths = summarize_tool(name, normalized)
    patch = args.get("input")
    if isinstance(patch, str):  # apply_patch carries the files in its patch text
        for p in _PATCH_FILE.findall(patch):
            p = p.strip()
            if p not in paths:
                paths.append(p)
        if paths and summary == str(name):
            summary = f"{name} {paths[0]}"
    return summary, paths


class _Draft:
    """An exchange being assembled while scanning the transcript."""

    def __init__(self, start: int, ts: int, user_text: str):
        self.start, self.ts, self.user_text = start, ts, user_text
        self.texts_after_tool: list[str] = []
        self.all_texts: list[str] = []
        self.tool_summaries: list[str] = []
        self.files: list[str] = []

    def add_reply(self, text: str) -> None:
        self.all_texts.append(text)
        self.texts_after_tool.append(text)

    def add_tool(self, name: object, args: object) -> None:
        summary, paths = _tool_paths_and_summary(name, args)
        self.tool_summaries.append(summary)
        self.files.extend(p for p in paths if p not in self.files)
        self.texts_after_tool = []

    @property
    def assistant_text(self) -> str:
        # Final answer = text after the last tool call; if the turn ended on a
        # tool call with no closing text, fall back to everything said.
        return "\n\n".join(self.texts_after_tool or self.all_texts)

    def finish(self, session_id: str, idx: int) -> Exchange:
        return Exchange(
            session_id=session_id,
            idx=idx,
            ts=self.ts,
            user_text=self.user_text,
            assistant_text=self.assistant_text,
            tool_summaries=self.tool_summaries,
            files_touched=self.files,
        )


class CopilotAdapter(FileConfigAdapter):
    name = "copilot"
    display_name = "GitHub Copilot"
    rules_owned = False

    def detect(self) -> bool:
        return shutil.which("copilot") is not None or copilot_dir().is_dir()

    # ---- install side -----------------------------------------------------

    def mcp_path(self) -> Path:
        return copilot_dir() / "mcp-config.json"

    def mcp_entry(self) -> dict:
        home = os.environ.get("RAINDROP_HOME") or str(constants.raindrop_home())
        return {
            "type": "local",
            "command": exe(),
            "args": ["proxy"],
            "env": {"RAINDROP_HOME": home},
            "tools": ["*"],
        }

    def rules_path(self) -> Path:
        return copilot_dir() / "copilot-instructions.md"

    def hooks_path(self) -> Path:
        return copilot_dir() / "hooks" / "raindrop.json"

    def hook_command(self) -> str:
        return f"{quote_cmd(exe())} sync --tool copilot --fail-open"

    def _hook_text(self, _old: str) -> str:
        return dump_json({
            "version": 1,
            "hooks": {
                "agentStop": [
                    {"type": "command", "command": self.hook_command(), "timeoutSec": 60}
                ]
            },
        })

    def _plan_hook(self) -> Change:
        path = self.hooks_path()
        old, new = edit_file(path, self._hook_text)
        return Change(
            kind="create",
            target=str(path),
            description=f"Add a Stop hook at {path} that syncs each finished turn to your team",
            preview=diff(path, old, new) or "(already installed, no change)",
            component="hook",
            _apply=lambda: apply_file_edit(path, "hook", self._hook_text),
        )

    def plan_install(self, rules: bool) -> list[Change]:
        changes = [self._plan_mcp(), self._plan_hook()]
        if rules:
            changes.append(self._plan_rules())
        return changes

    def uninstall(self, record: ToolRecord) -> None:
        super().uninstall(record)
        if record.get("hook"):
            delete_file(self.hooks_path(), "hook")

    # ---- extraction side --------------------------------------------------

    def session_from_hook(self, payload: dict) -> SessionRef:
        # The CLI docs spell the payload in camelCase, the VS Code-compatible variant in
        # snake_case; accept both.
        session_id = payload.get("sessionId") or payload.get("session_id")
        if not isinstance(session_id, str) or not _SAFE_ID.fullmatch(session_id):
            raise TranscriptError("Copilot hook payload has no valid sessionId")
        cwd = payload.get("cwd")
        return SessionRef(
            session_id=session_id,
            transcript_path=self._transcript(
                payload.get("transcriptPath") or payload.get("transcript_path"), session_id
            ),
            cwd=cwd if isinstance(cwd, str) else None,
        )

    @staticmethod
    def _transcript(raw: object, session_id: str) -> Path | None:
        if isinstance(raw, str) and raw:
            p = Path(raw).expanduser()
            if p.is_file():
                return p
        # Not given, or stale: look where VS Code keeps transcripts, then Copilot's own store.
        for user_dir in _vscode_user_dirs():
            matches = sorted(
                (user_dir / "workspaceStorage").glob(
                    f"*/GitHub.copilot-chat/transcripts/{session_id}.jsonl"
                )
            )
            if matches:
                return matches[0]
        # Copilot's own engine (the CLI, and chat sessions it runs for VS Code) keeps its
        # sessions here, in the same event style.
        own = copilot_dir() / "session-state" / session_id / "events.jsonl"
        return own if own.is_file() else None

    def read_new(self, session: SessionRef, cursor: Cursor) -> ReadResult:
        path = session.transcript_path
        if path is None or not path.exists():
            raise TranscriptError(f"Transcript not found for session {session.session_id}")
        data = path.read_bytes()
        if cursor > len(data):
            cursor = 0  # file was replaced or truncated; start over

        # Scan from byte 0 so Exchange.idx is stable (idx = ordinal of the prompt
        # in the session) even though the cursor is just an offset.
        drafts: list[_Draft] = []
        consumed = 0
        pos = 0
        while pos < len(data):
            nl = data.find(b"\n", pos)
            if nl == -1:
                break  # partial line still being written; leave it
            line, line_start, pos = data[pos:nl], pos, nl + 1
            consumed = pos
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            kind, body = event.get("type"), event.get("data")
            if not isinstance(body, dict):
                continue
            if kind == "user.message":
                text = str(body.get("content") or "").strip()
                if text:
                    drafts.append(_Draft(line_start, _to_epoch(event.get("timestamp")), text))
            elif not drafts:
                continue
            elif kind == "assistant.message":
                text = str(body.get("content") or "").strip()  # reasoningText is never shared
                if text:
                    drafts[-1].add_reply(text)
            elif kind == "tool.execution_start":
                drafts[-1].add_tool(body.get("toolName"), body.get("arguments"))

        new_cursor = consumed
        if drafts and not drafts[-1].assistant_text:
            new_cursor = drafts[-1].start  # hold back the unanswered prompt
            held = len(drafts) - 1
        else:
            held = len(drafts)

        exchanges = [
            d.finish(session.session_id, i)
            for i, d in enumerate(drafts[:held])
            if d.start >= cursor and d.assistant_text
        ]
        return ReadResult(exchanges=exchanges, new_cursor=max(new_cursor, cursor))
