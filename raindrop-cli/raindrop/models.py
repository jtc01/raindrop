"""Shared types for raindrop.

Anything that crosses a module boundary lives here so that sync.py,
api_client.py, redact.py, config.py, cli.py and the adapters can all import
the same definitions without importing from each other.

Adapter-only types (Adapter, Change, InstallStatus, SessionRef) live in
raindrop/adapters/base.py. Hook/transcript payload shapes live in
raindrop/adapters/payloads.py.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal, NamedTuple, TypedDict

# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------

# Opaque to sync.py. Each adapter decides what it means:
#   Claude Code / Cursor / Codex -> byte offset into the transcript or spool
#   Gemini (no hooks)            -> number of messages already processed
#
# OPEN QUESTION: Exchange.idx cannot be derived from a byte offset. If you
# decide to store next_idx alongside the offset, replace this alias with a
# small dataclass (offset: int, next_idx: int) and update cursor.py.
Cursor = int


# ---------------------------------------------------------------------------
# Exchange: the normalized unit every adapter produces
# ---------------------------------------------------------------------------


@dataclass
class Exchange:
    session_id: str
    idx: int  # position within the session, starts at 0
    ts: int  # epoch seconds of the user prompt
    user_text: str
    assistant_text: str  # final text only: no thinking, no tool output
    tool_summaries: list[str] = field(default_factory=list)  # e.g. "Edit src/app.py"
    files_touched: list[str] = field(default_factory=list)  # deduped, first-seen order

    @property
    def id(self) -> str:
        return f"{self.session_id}:{self.idx}"

    def to_wire(self) -> ExchangeWire:
        return ExchangeWire(**asdict(self), id=self.id)


class ReadResult(NamedTuple):
    exchanges: list[Exchange]
    # Points at the start of any held-back (unanswered) exchange, not at EOF.
    new_cursor: Cursor


# ---------------------------------------------------------------------------
# Wire types (must match what the server's push_messages accepts)
# ---------------------------------------------------------------------------


class ExchangeWire(TypedDict):
    id: str
    session_id: str
    idx: int
    ts: int
    user_text: str
    assistant_text: str
    tool_summaries: list[str]
    files_touched: list[str]


class PushMessagesRequest(TypedDict):
    exchanges: list[ExchangeWire]  # max 50 per request


class PushMessagesResponse(TypedDict):
    accepted: int
    ids: list[str]


# ---------------------------------------------------------------------------
# Config types (~/.raindrop/config.json)
# ---------------------------------------------------------------------------


class ToolRecord(TypedDict):
    mcp: bool
    hook: bool
    rules: bool
    installed_at: int  # prompt or response text; "" for stop
    files: list[str]
    raw_event_name: (
        str  # the tool's own event name, for debugging           # epoch seconds
    )
    extra: dict  # adapter-specific, e.g. {"mcp_method": "cli"}


class RaindropConfig(TypedDict):
    api_url: str
    api_key: str  # shown once at join, file is chmod 0600
    agent_id: str
    team_id: str
    display_name: str
    focus: str
    tools: dict[str, ToolRecord]  # keyed by adapter name, e.g. "claude-code"


# ---------------------------------------------------------------------------
# Spool types (event-capture adapters such as Cursor)
# ---------------------------------------------------------------------------

SpoolEvent = Literal["prompt", "response", "stop"]


class SpoolRecord(TypedDict):
    event: SpoolEvent
    ts: int
    session_id: str
    text: str
