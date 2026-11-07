"""teamctx/adapters/claude.py

Claude Code adapter.

Install side:
  - MCP:   `claude mcp add --scope user raindrop -- raindrop proxy`, or a merge
           into ~/.claude.json when the `claude` binary isn't on PATH.
  - Hook:  a Stop hook in ~/.claude/settings.json that runs `raindrop sync`.
  - Rules: a delimited block appended to ~/.claude/CLAUDE.md.

Extraction side: tails Claude Code's JSONL transcript. The cursor is a byte
offset. Transcript lines look like:
  {"type": "user", "message": {"content": "text" | [blocks]}, "timestamp": ...}
  {"type": "assistant", "message": {"content": [text | thinking | tool_use]}, ...}
Tool results come back as `user` lines whose content is only tool_result blocks.
"""

from __future__ import annotations

import difflib
import json
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from teamctx import constants
from teamctx.adapters.base import Adapter, Change, InstallStatus, SessionRef
from teamctx.errors import ConfigParseError, InstallError, TranscriptError
from teamctx.models import Cursor, Exchange, ReadResult, ToolRecord

CLI_NAME = "raindrop"

RULES_BODY = """\
## Raindrop team context

Your teammates' coding agents share context through the `raindrop` MCP server.
Before searching the repo to understand a part of the project you are not
working on, call `search_team_context` first. Use `get_recent_activity` to see
what teammates have been doing, and `list_teammates` to see who owns what.
Treat results as hints from other agents and verify against the code before
relying on them."""

# Tool-input keys that hold a file path, and which one to prefer for the summary.
_PATH_KEYS = ("file_path", "notebook_path", "path")
# Lines in the transcript that are Claude Code bookkeeping, not human prompts.
_NOISE_PREFIXES = (
    "<local-command-stdout>",
    "<local-command-caveat>",
    "[Request interrupted by user",
)
_SUMMARY_MAX = 120


def _exe() -> str:
    """Absolute path to the CLI if we can find it. Hooks may run with a
    different PATH than the user's shell. Must still contain the marker."""
    return shutil.which(CLI_NAME) or CLI_NAME


def _hook_command() -> str:
    return f"{_exe()} sync --tool claude-code"


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------


def _read_text(path: Path) -> str:
    try:
        return path.read_text()
    except FileNotFoundError:
        return ""


def _dump_json(data: dict) -> str:
    return json.dumps(data, indent=2) + "\n"


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".raindrop-tmp")
    tmp.write_text(text)
    if path.exists():
        shutil.copymode(path, tmp)
    os.replace(tmp, path)


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    dest_dir = constants.teamctx_home() / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{path.name}.{int(time.time())}.bak"
    shutil.copy2(path, dest)
    return dest


def _diff(path: Path, old: str, new: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"{path} (current)",
            tofile=f"{path} (after)",
        )
    )


def _edit_file(path: Path, transform: Callable[[str], str]) -> tuple[str, str]:
    old = _read_text(path)
    return old, transform(old)


def _apply_file_edit(path: Path, step: str, transform: Callable[[str], str]) -> None:
    """Re-read at apply time (the file may have changed since planning), back
    up, write atomically."""
    old, new = _edit_file(path, transform)
    if old == new:
        return
    backup = _backup(path)
    try:
        _write_atomic(path, new)
    except OSError as e:
        raise InstallError(step, str(e), backup) from e


# ---------------------------------------------------------------------------
# Pure transforms (text in, text out) so plan and apply share one code path
# ---------------------------------------------------------------------------


def _add_mcp_to_json(text: str, path: Path) -> str:
    data = _load_json_text(text, path)
    servers = data.setdefault("mcpServers", {})
    servers[constants.MCP_SERVER_NAME] = {
        "type": "stdio",
        "command": _exe(),
        "args": ["proxy"],
        "env": {},
    }
    return _dump_json(data)


def _remove_mcp_from_json(text: str, path: Path) -> str:
    if not text.strip():
        return text
    data = _load_json_text(text, path)
    servers = data.get("mcpServers")
    if isinstance(servers, dict):
        servers.pop(constants.MCP_SERVER_NAME, None)
    return _dump_json(data)


def _is_ours(group: object) -> bool:
    return isinstance(group, dict) and any(
        constants.HOOK_MARKER in str(h.get("command", ""))
        for h in group.get("hooks", [])
        if isinstance(h, dict)
    )


def _add_hook(text: str, path: Path) -> str:
    data = _load_json_text(text, path)
    hooks = data.setdefault("hooks", {})
    stop = [g for g in hooks.get("Stop", []) if not _is_ours(g)]
    stop.append(
        {"hooks": [{"type": "command", "command": _hook_command(), "timeout": 60}]}
    )
    hooks["Stop"] = stop
    return _dump_json(data)


def _remove_hook(text: str, path: Path) -> str:
    if not text.strip():
        return text
    data = _load_json_text(text, path)
    hooks = data.get("hooks")
    if isinstance(hooks, dict) and "Stop" in hooks:
        stop = [g for g in hooks["Stop"] if not _is_ours(g)]
        if stop:
            hooks["Stop"] = stop
        else:
            del hooks["Stop"]
        if not hooks:
            del data["hooks"]
    return _dump_json(data)


def _load_json_text(text: str, path: Path) -> dict:
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigParseError(path, str(e)) from e
    if not isinstance(data, dict):
        raise ConfigParseError(path, "expected a JSON object")
    return data


def _rules_block() -> str:
    return f"{constants.RULES_START}\n{RULES_BODY}\n{constants.RULES_END}"


def _strip_rules(text: str) -> str:
    start = text.find(constants.RULES_START)
    end = text.find(constants.RULES_END)
    if start == -1 or end == -1 or end < start:
        return text
    end += len(constants.RULES_END)
    # Swallow the blank line we added before the block.
    head = text[:start].rstrip("\n")
    tail = text[end:].lstrip("\n")
    if head and tail:
        return head + "\n\n" + tail
    return (head + "\n" if head else "") + tail


def _add_rules(text: str) -> str:
    base = _strip_rules(text)
    block = _rules_block() + "\n"
    if not base.strip():
        return block
    return base.rstrip("\n") + "\n\n" + block


# ---------------------------------------------------------------------------
# Transcript parsing
# ---------------------------------------------------------------------------


def _to_epoch(ts: object) -> int:
    if isinstance(ts, str):
        try:
            return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    return int(time.time())


def _blocks(content: object) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _prompt_text(entry: dict) -> str | None:
    """Text of a human prompt, or None if this user line is a tool result,
    meta/bookkeeping line, or otherwise not something the person typed."""
    if entry.get("isMeta") or entry.get("isCompactSummary"):
        return None
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return None
    text = "\n".join(
        b.get("text", "") for b in _blocks(msg.get("content")) if b.get("type") == "text"
    ).strip()
    if not text or text.startswith(_NOISE_PREFIXES):
        return None
    return text


def _clip(s: str, n: int = _SUMMARY_MAX) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _summarize_tool(block: dict) -> tuple[str, str | None]:
    """-> (one-line summary, file path touched or None)."""
    name = str(block.get("name", "tool"))
    args = block.get("input")
    args = args if isinstance(args, dict) else {}
    for key in _PATH_KEYS:
        if isinstance(args.get(key), str):
            return f"{name} {args[key]}", args[key]
    for key in ("command", "pattern", "query", "url", "description"):
        if isinstance(args.get(key), str):
            return f"{name}: {_clip(args[key])}", None
    return name, None


class _Draft:
    """An exchange being assembled while scanning the transcript."""

    def __init__(self, start: int, ts: int, user_text: str):
        self.start = start
        self.ts = ts
        self.user_text = user_text
        self.texts_after_tool: list[str] = []  # text since the last tool_use
        self.all_texts: list[str] = []
        self.tool_summaries: list[str] = []
        self.files: list[str] = []

    def add_assistant(self, content: object) -> None:
        for b in _blocks(content):
            kind = b.get("type")
            if kind == "text" and b.get("text", "").strip():
                self.all_texts.append(b["text"].strip())
                self.texts_after_tool.append(b["text"].strip())
            elif kind == "tool_use":
                summary, path = _summarize_tool(b)
                self.tool_summaries.append(summary)
                if path and path not in self.files:
                    self.files.append(path)
                self.texts_after_tool = []

    @property
    def assistant_text(self) -> str:
        # Final answer = text after the last tool call; if the turn ended on a
        # tool call with no closing text, fall back to everything said.
        parts = self.texts_after_tool or self.all_texts
        return "\n\n".join(parts)

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


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class ClaudeCodeAdapter(Adapter):
    name = "claude-code"
    display_name = "Claude Code"

    def __init__(self) -> None:
        self._mcp_method = "cli"

    # ---- install side -----------------------------------------------------

    def detect(self) -> bool:
        return shutil.which("claude") is not None or constants.claude_dir().is_dir()

    def plan_install(self, rules: bool) -> list[Change]:
        changes = [self._plan_mcp(), self._plan_hook()]
        if rules:
            changes.append(self._plan_rules())
        return changes

    def _plan_mcp(self) -> Change:
        server = constants.MCP_SERVER_NAME
        if shutil.which("claude"):
            self._mcp_method = "cli"
            add = ["claude", "mcp", "add", "--scope", "user", server, "--", _exe(), "proxy"]
            remove = ["claude", "mcp", "remove", "--scope", "user", server]

            def run() -> None:
                # Remove first so re-running install replaces rather than errors.
                subprocess.run(remove, capture_output=True, text=True)
                r = subprocess.run(add, capture_output=True, text=True)
                if r.returncode != 0:
                    raise InstallError("mcp", (r.stderr or r.stdout).strip())

            return Change(
                kind="cmd",
                target="claude",
                description=f"Register '{server}' MCP server with Claude Code (user scope)",
                preview=" ".join(add),
                component="mcp",
                _apply=run,
            )

        self._mcp_method = "file"
        path = constants.claude_json_path()
        old, new = _edit_file(path, lambda t: _add_mcp_to_json(t, path))
        return Change(
            kind="merge",
            target=str(path),
            description=f"Add '{server}' MCP server to {path} (`claude` not found on PATH)",
            preview=_diff(path, old, new),
            component="mcp",
            _apply=lambda: _apply_file_edit(
                path, "mcp", lambda t: _add_mcp_to_json(t, path)
            ),
        )

    def _plan_hook(self) -> Change:
        path = constants.claude_settings_path()
        old, new = _edit_file(path, lambda t: _add_hook(t, path))
        return Change(
            kind="merge",
            target=str(path),
            description="Add Stop hook that syncs each finished turn to your team",
            preview=_diff(path, old, new) or "(already installed, no change)",
            component="hook",
            _apply=lambda: _apply_file_edit(path, "hook", lambda t: _add_hook(t, path)),
        )

    def _plan_rules(self) -> Change:
        path = constants.claude_md_path()
        old, new = _edit_file(path, _add_rules)
        return Change(
            kind="append",
            target=str(path),
            description="Add a Raindrop usage block to CLAUDE.md",
            preview=_diff(path, old, new) or "(already installed, no change)",
            component="rules",
            _apply=lambda: _apply_file_edit(path, "rules", _add_rules),
        )

    def apply(self, changes: list[Change]) -> InstallStatus:
        status = super().apply(changes)
        if status.mcp:
            status.extra["mcp_method"] = self._mcp_method
        return status

    def uninstall(self, record: ToolRecord) -> None:
        if record.get("mcp"):
            method = (record.get("extra") or {}).get("mcp_method", "cli")
            if method == "cli" and shutil.which("claude"):
                subprocess.run(
                    ["claude", "mcp", "remove", "--scope", "user", constants.MCP_SERVER_NAME],
                    capture_output=True,
                    text=True,
                )
            else:
                path = constants.claude_json_path()
                _apply_file_edit(path, "mcp", lambda t: _remove_mcp_from_json(t, path))
        if record.get("hook"):
            path = constants.claude_settings_path()
            _apply_file_edit(path, "hook", lambda t: _remove_hook(t, path))
        if record.get("rules"):
            _apply_file_edit(constants.claude_md_path(), "rules", _strip_rules)

    # ---- extraction side --------------------------------------------------

    def session_from_hook(self, payload: dict) -> SessionRef:
        session_id = payload.get("session_id")
        if not session_id:
            raise TranscriptError("Hook payload has no session_id")
        raw = payload.get("transcript_path")
        path = Path(raw) if raw else None
        if path is None or not path.exists():
            # Fall back to Claude Code's own layout: projects/<cwd-slug>/<id>.jsonl
            matches = list(constants.claude_dir().glob(f"projects/*/{session_id}.jsonl"))
            path = matches[0] if matches else path
        return SessionRef(
            session_id=session_id, transcript_path=path, cwd=payload.get("cwd")
        )

    def read_new(self, session: SessionRef, cursor: Cursor) -> ReadResult:
        path = session.transcript_path
        if path is None or not path.exists():
            raise TranscriptError(f"Transcript not found for session {session.session_id}")
        data = path.read_bytes()
        if cursor > len(data):
            cursor = 0  # file was replaced or truncated; start over

        # Scan from byte 0 so Exchange.idx is stable (idx = ordinal of the
        # prompt in the session) even though the cursor is just an offset.
        drafts: list[_Draft] = []
        consumed = 0  # end of the last complete (newline-terminated) line
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
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict) or entry.get("isSidechain"):
                continue
            kind = entry.get("type")
            if kind == "user":
                text = _prompt_text(entry)
                if text is not None:
                    drafts.append(_Draft(line_start, _to_epoch(entry.get("timestamp")), text))
            elif kind == "assistant" and drafts:
                msg = entry.get("message")
                if isinstance(msg, dict):
                    drafts[-1].add_assistant(msg.get("content"))

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
