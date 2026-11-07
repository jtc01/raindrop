"""raindrop/adapters/copilot.py

GitHub Copilot via the Copilot CLI's config directory ($COPILOT_HOME, default
~/.copilot). VS Code's docs describe the same `mcp-config.json` as the
"Copilot Global" destination; its own `mcp.json` (a different `servers` schema
at a per-OS path) is not written.

  - MCP:   <dir>/mcp-config.json, under `mcpServers`, `type: "local"`
  - Rules: a delimited block in <dir>/copilot-instructions.md (personal
           instructions, applied to every session; the file is the user's)

Copilot passes MCP servers only PATH from its own environment, so the entry
sets RAINDROP_HOME explicitly; the proxy needs it to find config.json.

No stop hook yet: it can read team context but doesn't share its own sessions.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from raindrop import constants
from raindrop.adapters._common import exe
from raindrop.adapters._file_config import FileConfigAdapter


def copilot_dir() -> Path:
    override = os.environ.get("COPILOT_HOME")
    return Path(override).expanduser() if override else Path.home() / ".copilot"


class CopilotAdapter(FileConfigAdapter):
    name = "copilot"
    display_name = "GitHub Copilot"
    rules_owned = False

    def detect(self) -> bool:
        return shutil.which("copilot") is not None or copilot_dir().is_dir()

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
