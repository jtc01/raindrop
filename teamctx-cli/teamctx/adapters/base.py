"""teamctx/adapters/base.py

The contract every tool adapter (Claude Code, Cursor, ...) implements.

Shared data types (Exchange, ReadResult, Cursor, ToolRecord) live in
teamctx/models.py. This file only holds what adapters and the `tools` CLI
commands need.

To add a new tool:
  1. Subclass Adapter in teamctx/adapters/<tool>.py
  2. Add one line to ADAPTERS in teamctx/adapters/registry.py
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, ClassVar, Literal

from teamctx.models import Cursor, ReadResult, ToolRecord

Component = Literal["mcp", "hook", "rules"]
ChangeKind = Literal["create", "merge", "append", "cmd"]


@dataclass
class Change:
    """One planned modification, e.g. 'merge Stop hook into settings.json'.

    plan_install() returns these without touching anything. The CLI prints
    description + preview (dry-run and confirm prompt), then apply() runs them.
    """

    kind: ChangeKind
    target: str  # file path, or the command name for kind="cmd"
    description: str  # one human-readable line for the confirm prompt
    preview: str  # unified diff, or the exact command string
    component: Component  # which ToolRecord flag this sets
    _apply: Callable[[], None]  # performs the change

    def run(self) -> None:
        self._apply()


@dataclass
class InstallStatus:
    """What apply() actually did. Maps onto ToolRecord in config.json."""

    mcp: bool = False
    hook: bool = False
    rules: bool = False
    extra: dict = field(default_factory=dict)  # e.g. {"mcp_method": "cli"}

    def to_tool_record(self) -> ToolRecord:
        return ToolRecord(
            mcp=self.mcp,
            hook=self.hook,
            rules=self.rules,
            installed_at=int(time.time()),
            extra=self.extra,
        )


@dataclass
class SessionRef:
    """Identifies the session a hook fired for."""

    session_id: str
    transcript_path: Path | None  # None for tools that don't expose a transcript file
    cwd: str | None = None


class Adapter(ABC):
    name: ClassVar[str]  # registry key and CLI argument, e.g. "claude-code"
    display_name: ClassVar[str]  # for humans, e.g. "Claude Code"

    # ---- install side -----------------------------------------------------

    @abstractmethod
    def detect(self) -> bool:
        """Is this tool installed on this machine?"""

    @abstractmethod
    def plan_install(self, rules: bool) -> list[Change]:
        """Return the changes install would make. Must not modify anything."""

    def apply(self, changes: list[Change]) -> InstallStatus:
        """Run the planned changes and report what was done.

        Default implementation runs each change and flags its component.
        Override only if an adapter needs extra bookkeeping (put it in
        status.extra).
        """
        status = InstallStatus()
        for change in changes:
            change.run()
            setattr(status, change.component, True)
        return status

    @abstractmethod
    def uninstall(self, record: ToolRecord) -> None:
        """Undo whatever `record` says was installed."""

    # ---- extraction side --------------------------------------------------

    @abstractmethod
    def session_from_hook(self, payload: dict) -> SessionRef:
        """Build a SessionRef from the hook's stdin JSON."""

    @abstractmethod
    def read_new(self, session: SessionRef, cursor: Cursor) -> ReadResult:
        """Return complete exchanges after `cursor`.

        Never return a half-finished exchange. If the last user prompt has no
        assistant reply yet, leave it out and set new_cursor to the start of
        that prompt, not EOF, so it is picked up on the next run.
        """

    def capture(self, payload: dict) -> None:
        """Event-capture tools (no readable transcript) override this to spool
        hook events locally. Transcript-tail adapters leave it alone."""
        raise NotImplementedError(f"{self.name} does not use event capture")
