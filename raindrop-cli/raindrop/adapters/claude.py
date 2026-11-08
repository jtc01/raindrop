"""raindrop/adapters/claude.py

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

import json
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from raindrop import constants
from raindrop.adapters._common import (
    add_rules_block,
    apply_file_edit,
    diff,
    dump_json,
    edit_file,
    display_cmd,
    exe,
    is_our_hook_command,
    jsonl_head,
    newest_session_in_repo,
    quote_cmd,
    strip_rules_block,
    load_json_text,
    summarize_tool,
    repo_relative_files,
)
from raindrop.adapters.base import Adapter, Change, InstallStatus, SessionRef
from raindrop.errors import InstallError, TranscriptError
from raindrop.models import Cursor, Exchange, ReadResult, ToolRecord

# Lines in the transcript that are Claude Code bookkeeping, not human prompts.
_NOISE_PREFIXES = (
    "<local-command-stdout>",
    "<local-command-caveat>",
    "[Request interrupted by user",
)


def _hook_command() -> str:
    return f"{quote_cmd(exe())} sync --tool claude-code"


# ---------------------------------------------------------------------------
# Pure transforms (text in, text out) so plan and apply share one code path
# ---------------------------------------------------------------------------


def _mcp_entry() -> dict:
    # alwaysLoad: Claude Code otherwise defers MCP tools behind a ToolSearch step, which
    # costs the agent a whole extra turn before its first search_team_context call.
    return {"type": "stdio", "command": exe(), "args": ["proxy"], "env": {}, "alwaysLoad": True}


def _add_mcp_to_json(text: str, path: Path) -> str:
    data = load_json_text(text, path)
    servers = data.setdefault("mcpServers", {})
    servers[constants.MCP_SERVER_NAME] = _mcp_entry()
    return dump_json(data)


def _remove_mcp_from_json(text: str, path: Path) -> str:
    if not text.strip():
        return text
    data = load_json_text(text, path)
    servers = data.get("mcpServers")
    if isinstance(servers, dict):
        servers.pop(constants.MCP_SERVER_NAME, None)
    return dump_json(data)


def _is_ours(group: object) -> bool:
    return isinstance(group, dict) and any(
        is_our_hook_command(h.get("command", ""))
        for h in group.get("hooks", [])
        if isinstance(h, dict)
    )


def _add_hook(text: str, path: Path) -> str:
    data = load_json_text(text, path)
    hooks = data.setdefault("hooks", {})
    stop = [g for g in hooks.get("Stop", []) if not _is_ours(g)]
    stop.append(
        {"hooks": [{"type": "command", "command": _hook_command(), "timeout": 60}]}
    )
    hooks["Stop"] = stop
    return dump_json(data)


def _remove_hook(text: str, path: Path) -> str:
    if not text.strip():
        return text
    data = load_json_text(text, path)
    hooks = data.get("hooks")
    if isinstance(hooks, dict) and "Stop" in hooks:
        stop = [g for g in hooks["Stop"] if not _is_ours(g)]
        if stop:
            hooks["Stop"] = stop
        else:
            del hooks["Stop"]
        if not hooks:
            del data["hooks"]
    return dump_json(data)


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
                summary, paths = summarize_tool(b.get("name"), b.get("input"))
                self.tool_summaries.append(summary)
                self.files.extend(p for p in paths if p not in self.files)
                self.texts_after_tool = []

    @property
    def assistant_text(self) -> str:
        # Final answer = text after the last tool call; if the turn ended on a
        # tool call with no closing text, fall back to everything said.
        parts = self.texts_after_tool or self.all_texts
        return "\n\n".join(parts)

    def finish(self, session: SessionRef, idx: int) -> Exchange:
        return Exchange(
            session_id=session.session_id,
            idx=idx,
            ts=self.ts,
            user_text=self.user_text,
            assistant_text=self.assistant_text,
            tool_summaries=self.tool_summaries,
            files_touched=repo_relative_files(self.files, session),
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
        claude = shutil.which("claude")
        if claude:
            self._mcp_method = "cli"
            # Full path from which(): on Windows `claude` is often a .cmd shim
            # that a bare name can't launch.
            add = [claude, "mcp", "add-json", "--scope", "user", server, json.dumps(_mcp_entry())]
            # Older Claude Code without add-json (or without alwaysLoad): plain add still works,
            # the tools are just deferred behind ToolSearch.
            fallback = [claude, "mcp", "add", "--scope", "user", server, "--", exe(), "proxy"]
            remove = [claude, "mcp", "remove", "--scope", "user", server]

            def run() -> None:
                # Remove first so re-running install replaces rather than errors.
                subprocess.run(remove, capture_output=True, text=True)
                r = subprocess.run(add, capture_output=True, text=True)
                if r.returncode != 0:
                    r = subprocess.run(fallback, capture_output=True, text=True)
                if r.returncode != 0:
                    raise InstallError("mcp", (r.stderr or r.stdout).strip())

            return Change(
                kind="cmd",
                target="claude",
                description=f"Register '{server}' MCP server with Claude Code (user scope)",
                preview=display_cmd(["claude", *add[1:]]),
                component="mcp",
                _apply=run,
            )

        self._mcp_method = "file"
        path = constants.claude_json_path()
        old, new = edit_file(path, lambda t: _add_mcp_to_json(t, path))
        return Change(
            kind="merge",
            target=str(path),
            description=f"Add '{server}' MCP server to {path} (`claude` not found on PATH)",
            preview=diff(path, old, new),
            component="mcp",
            _apply=lambda: apply_file_edit(
                path, "mcp", lambda t: _add_mcp_to_json(t, path)
            ),
        )

    def _plan_hook(self) -> Change:
        path = constants.claude_settings_path()
        old, new = edit_file(path, lambda t: _add_hook(t, path))
        return Change(
            kind="merge",
            target=str(path),
            description="Add Stop hook that syncs each finished turn to your team",
            preview=diff(path, old, new) or "(already installed, no change)",
            component="hook",
            _apply=lambda: apply_file_edit(path, "hook", lambda t: _add_hook(t, path)),
        )

    def _plan_rules(self) -> Change:
        path = constants.claude_md_path()
        old, new = edit_file(path, add_rules_block)
        return Change(
            kind="append",
            target=str(path),
            description="Add a Raindrop usage block to CLAUDE.md",
            preview=diff(path, old, new) or "(already installed, no change)",
            component="rules",
            _apply=lambda: apply_file_edit(path, "rules", add_rules_block),
        )

    def apply(self, changes: list[Change]) -> InstallStatus:
        status = super().apply(changes)
        if status.mcp:
            status.extra["mcp_method"] = self._mcp_method
        return status

    def uninstall(self, record: ToolRecord) -> None:
        if record.get("mcp"):
            method = (record.get("extra") or {}).get("mcp_method", "cli")
            claude = shutil.which("claude")
            if method == "cli" and claude:
                subprocess.run(
                    [claude, "mcp", "remove", "--scope", "user", constants.MCP_SERVER_NAME],
                    capture_output=True,
                    text=True,
                )
            else:
                path = constants.claude_json_path()
                apply_file_edit(path, "mcp", lambda t: _remove_mcp_from_json(t, path))
        if record.get("hook"):
            path = constants.claude_settings_path()
            apply_file_edit(path, "hook", lambda t: _remove_hook(t, path))
        if record.get("rules"):
            apply_file_edit(constants.claude_md_path(), "rules", strip_rules_block)

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

    def latest_session(self, repo: Path) -> SessionRef | None:
        # Transcripts live at projects/<cwd-slug>/<session_id>.jsonl. The slug
        # is lossy, so match on the `cwd` recorded inside each transcript.
        def probe(path: Path) -> tuple[str, str] | None:
            for entry in jsonl_head(path):
                if isinstance(entry.get("cwd"), str):
                    return path.stem, entry["cwd"]
            return None

        files = constants.claude_dir().glob("projects/*/*.jsonl")
        return newest_session_in_repo(files, probe, repo)

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
            d.finish(session, i)
            for i, d in enumerate(drafts[:held])
            if d.start >= cursor and d.assistant_text
        ]
        return ReadResult(exchanges=exchanges, new_cursor=max(new_cursor, cursor))
