"""raindrop/adapters/_file_config.py

Base for tools that are installed purely by editing config files: one MCP
entry in a JSON file under `mcpServers`, plus optional rules text. There is no
stop hook yet (NoSyncMixin), so these tools can read team context but don't
share the user's own sessions.

Subclasses set name/display_name and implement the path and content hooks.
"""

from __future__ import annotations

from abc import abstractmethod
from pathlib import Path

from raindrop import constants
from raindrop.adapters._common import (
    NoSyncMixin,
    add_rules_block,
    apply_file_edit,
    delete_file,
    diff,
    dump_json,
    edit_file,
    load_json_text,
    strip_rules_block,
)
from raindrop.adapters.base import Adapter, Change
from raindrop.models import ToolRecord


class FileConfigAdapter(NoSyncMixin, Adapter):
    # True: the rules file is entirely ours (uninstall deletes it).
    # False: it's the user's file and we only manage a delimited block in it.
    rules_owned: bool = True

    # ---- subclass hooks ---------------------------------------------------

    @abstractmethod
    def mcp_path(self) -> Path: ...

    @abstractmethod
    def mcp_entry(self) -> dict:
        """The server definition written under mcpServers[MCP_SERVER_NAME]."""

    @abstractmethod
    def rules_path(self) -> Path: ...

    def rules_text(self) -> str:
        """Full file content when rules_owned (default: just our block)."""
        return ""

    # ---- install side -----------------------------------------------------

    def _add_mcp(self, text: str, path: Path) -> str:
        data = load_json_text(text, path)
        servers = data.setdefault("mcpServers", {})
        servers[constants.MCP_SERVER_NAME] = self.mcp_entry()
        return dump_json(data)

    def _remove_mcp(self, text: str, path: Path) -> str:
        if not text.strip():
            return text
        data = load_json_text(text, path)
        servers = data.get("mcpServers")
        if isinstance(servers, dict):
            servers.pop(constants.MCP_SERVER_NAME, None)
        return dump_json(data)

    def _rules_transform(self, text: str) -> str:
        return self.rules_text() if self.rules_owned else add_rules_block(text)

    def plan_install(self, rules: bool) -> list[Change]:
        changes = [self._plan_mcp()]
        if rules:
            changes.append(self._plan_rules())
        return changes

    def _plan_mcp(self) -> Change:
        path = self.mcp_path()
        old, new = edit_file(path, lambda t: self._add_mcp(t, path))
        return Change(
            kind="merge",
            target=str(path),
            description=f"Add '{constants.MCP_SERVER_NAME}' MCP server to {path}",
            preview=diff(path, old, new) or "(already installed, no change)",
            component="mcp",
            _apply=lambda: apply_file_edit(path, "mcp", lambda t: self._add_mcp(t, path)),
        )

    def _plan_rules(self) -> Change:
        path = self.rules_path()
        old, new = edit_file(path, self._rules_transform)
        owned = self.rules_owned
        return Change(
            kind="create" if owned else "append",
            target=str(path),
            description=(f"Add a Raindrop rules file at {path}" if owned
                         else f"Add a Raindrop usage block to {path}"),
            preview=diff(path, old, new) or "(already installed, no change)",
            component="rules",
            _apply=lambda: apply_file_edit(path, "rules", self._rules_transform),
        )

    def uninstall(self, record: ToolRecord) -> None:
        if record.get("mcp"):
            path = self.mcp_path()
            apply_file_edit(path, "mcp", lambda t: self._remove_mcp(t, path))
        if record.get("rules"):
            if self.rules_owned:
                delete_file(self.rules_path(), "rules")
            else:
                apply_file_edit(self.rules_path(), "rules", strip_rules_block)
