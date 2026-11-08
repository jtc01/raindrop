"""raindrop/adapters/antigravity.py

Google Antigravity (App, IDE and `agy` CLI share these global files).

Install side:
  - MCP:   ~/.gemini/config/mcp_config.json, under `mcpServers`
  - Hook:  ~/.gemini/config/hooks.json, a `raindrop` entry with a Stop hook that
           runs `raindrop sync --tool antigravity --fail-open`
  - Rules: ~/.gemini/config/rules/raindrop.md. Modular rule files must start
           with YAML frontmatter declaring a `trigger`, or Antigravity silently
           discards them; `always_on` injects the text on every turn.

Extraction side: tails the conversation transcript the Stop hook points at
(<app data>/brain/<conversationId>/.system_generated/logs/transcript.jsonl).
Each line is one step:
  {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT",
   "status": "DONE", "created_at": "...Z",
   "content": "<USER_REQUEST>\\ntext\\n</USER_REQUEST>\\n<ADDITIONAL_METADATA>..."}
  {"source": "MODEL", "type": "PLANNER_RESPONSE", "content": "reply", "thinking": ...}
  {"source": "MODEL", "type": "CODE_ACTION" | "VIEW_FILE" | "RUN_COMMAND" | ...}
Tool steps and system steps carry no prompt or reply text, so only USER_INPUT and
PLANNER_RESPONSE steps become exchange text. The cursor is a byte offset.

Stop hooks must answer on stdout with {"decision": ...}; "continue" keeps the
agent running, so we always answer "allow" (see hook_stdout).
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

from raindrop.adapters._common import (
    RULES_BODY,
    apply_file_edit,
    diff,
    dump_json,
    edit_file,
    exe,
    load_json_text,
)
from raindrop.adapters._file_config import FileConfigAdapter
from raindrop.adapters.base import Change, SessionRef
from raindrop.errors import TranscriptError
from raindrop.models import Cursor, Exchange, ReadResult, ToolRecord

HOOK_NAME = "raindrop"  # our key in hooks.json; the whole entry is ours to add and remove
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_USER_REQUEST = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)
_FILE_URI = re.compile(r"file:///[^\s`'\"<>]+")
# Tool steps whose content names the files they touched.
_FILE_STEPS = ("CODE_ACTION", "VIEW_FILE")
_TRANSCRIPT_REL = Path(".system_generated") / "logs" / "transcript.jsonl"


def gemini_dir() -> Path:
    return Path.home() / ".gemini"


def _short_path(path: str) -> str:
    """The Windows 8.3 short form of `path`, or `path` unchanged if there isn't one."""
    if sys.platform != "win32":
        return path
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(32768)
        n = ctypes.windll.kernel32.GetShortPathNameW(path, buf, len(buf))
        return buf.value.replace("\\", "/") if 0 < n < len(buf) else path
    except Exception:
        return path


def _no_space_path(path: str) -> str:
    return _short_path(path) if any(c.isspace() for c in path) else path


def _uri_to_path(uri: str) -> str:
    path = unquote(uri[len("file://"):])  # "file:///C:/x" -> "/C:/x"
    return path[1:] if re.match(r"^/[A-Za-z]:", path) else path


def _to_epoch(ts: object) -> int:
    if isinstance(ts, str):
        try:
            return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    return int(time.time())


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

    def add_tool(self, step: dict) -> None:
        kind = str(step.get("type", "")).lower()
        paths: list[str] = []
        if step.get("type") in _FILE_STEPS:
            for uri in _FILE_URI.findall(str(step.get("content", ""))):
                p = _uri_to_path(uri)
                # Antigravity's own scratch/artifact files are not the user's project.
                if "/.gemini/" not in p.replace("\\", "/") and p not in paths:
                    paths.append(p)
        self.tool_summaries.append(f"{kind} {paths[0]}" if paths else kind)
        self.files.extend(p for p in paths if p not in self.files)
        self.texts_after_tool = []

    @property
    def assistant_text(self) -> str:
        # Final answer = text after the last tool step; if the turn ended on a
        # tool step with no closing text, fall back to everything said.
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


class AntigravityAdapter(FileConfigAdapter):
    name = "antigravity"
    display_name = "Google Antigravity"
    rules_owned = True

    def detect(self) -> bool:
        # ~/.gemini alone isn't enough: Gemini CLI uses that folder too.
        g = gemini_dir()
        return (
            shutil.which("agy") is not None
            or shutil.which("antigravity") is not None
            or any((g / d).is_dir() for d in ("antigravity", "antigravity-cli", "config"))
        )

    # ---- install side -----------------------------------------------------

    def mcp_path(self) -> Path:
        return gemini_dir() / "config" / "mcp_config.json"

    def mcp_entry(self) -> dict:
        return {"command": exe(), "args": ["proxy"], "env": {}}

    def rules_path(self) -> Path:
        return gemini_dir() / "config" / "rules" / "raindrop.md"

    def rules_text(self) -> str:
        return (
            "---\n"
            "trigger: always_on\n"
            "description: Use Raindrop to read what teammates' coding agents have done\n"
            "---\n\n"
            f"{RULES_BODY}\n"
        )

    def hooks_path(self) -> Path:
        return gemini_dir() / "config" / "hooks.json"

    def hook_command(self) -> str:
        # Antigravity splits the command on spaces and passes quote characters through
        # untouched, so a quoted program path ("C:/Jo Smith/...") cannot work. Use the
        # space-free short form of the path when there is one.
        # So no quoting at all, on any OS (shell quoting would add literal quotes).
        prog = _no_space_path(exe()).replace("\\", "/")
        return f"{prog} sync --tool antigravity --fail-open"

    def _add_hook(self, text: str, path: Path) -> str:
        data = load_json_text(text, path)
        data[HOOK_NAME] = {
            "Stop": [{"type": "command", "command": self.hook_command(), "timeout": 60}]
        }
        return dump_json(data)

    def _remove_hook(self, text: str, path: Path) -> str:
        if not text.strip():
            return text
        data = load_json_text(text, path)
        data.pop(HOOK_NAME, None)
        return dump_json(data)

    def _plan_hook(self) -> Change:
        path = self.hooks_path()
        old, new = edit_file(path, lambda t: self._add_hook(t, path))
        return Change(
            kind="merge",
            target=str(path),
            description="Add Stop hook that syncs each finished turn to your team",
            preview=diff(path, old, new) or "(already installed, no change)",
            component="hook",
            _apply=lambda: apply_file_edit(path, "hook", lambda t: self._add_hook(t, path)),
        )

    def plan_install(self, rules: bool) -> list[Change]:
        changes = [self._plan_mcp(), self._plan_hook()]
        if rules:
            changes.append(self._plan_rules())
        return changes

    def uninstall(self, record: ToolRecord) -> None:
        super().uninstall(record)
        if record.get("hook"):
            path = self.hooks_path()
            apply_file_edit(path, "hook", lambda t: self._remove_hook(t, path))

    # ---- extraction side --------------------------------------------------

    def hook_stdout(self) -> str | None:
        return json.dumps({"decision": "allow"})  # never "continue": that keeps the agent running

    def session_from_hook(self, payload: dict) -> SessionRef:
        session_id = payload.get("conversationId")
        if not isinstance(session_id, str) or not _SAFE_ID.fullmatch(session_id):
            raise TranscriptError("Antigravity hook payload has no valid conversationId")
        cwd = payload.get("workspacePaths")
        cwd = cwd[0] if isinstance(cwd, list) and cwd else cwd
        if isinstance(cwd, str) and cwd.startswith("file:///"):
            cwd = _uri_to_path(cwd)
        return SessionRef(
            session_id=session_id,
            transcript_path=self._transcript(payload.get("transcriptPath"), session_id),
            cwd=cwd if isinstance(cwd, str) else None,
        )

    @staticmethod
    def _transcript(raw: object, session_id: str) -> Path | None:
        # The docs disagree on whether transcriptPath is the file or the
        # conversation folder, so accept both, then fall back to the known layout.
        if isinstance(raw, str) and raw:
            p = Path(raw).expanduser()
            if p.is_file():
                return p
            if (p / _TRANSCRIPT_REL).is_file():
                return p / _TRANSCRIPT_REL
        for app in sorted(gemini_dir().glob("antigravity*")):
            p = app / "brain" / session_id / _TRANSCRIPT_REL
            if p.is_file():
                return p
        return None

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
                step = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(step, dict):
                continue
            kind, source = step.get("type"), step.get("source")
            if kind == "USER_INPUT" and source == "USER_EXPLICIT":
                content = str(step.get("content", ""))
                m = _USER_REQUEST.search(content)
                text = (m.group(1) if m else content).strip()
                if text:
                    drafts.append(_Draft(line_start, _to_epoch(step.get("created_at")), text))
            elif not drafts:
                continue
            elif kind == "PLANNER_RESPONSE":
                text = str(step.get("content") or "").strip()  # thinking is never shared
                if text:
                    drafts[-1].add_reply(text)
            elif source == "MODEL" and kind != "GENERIC":
                drafts[-1].add_tool(step)

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
