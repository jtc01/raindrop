"""raindrop/adapters/antigravity.py

Google Antigravity (App, IDE and `agy` CLI share these global files).

  - MCP:   ~/.gemini/config/mcp_config.json, under `mcpServers`
  - Rules: ~/.gemini/config/rules/raindrop.md. Modular rule files must start
           with YAML frontmatter declaring a `trigger`, or Antigravity silently
           discards them; `always_on` injects the text on every turn.

No stop hook yet: it can read team context but doesn't share its own sessions.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from raindrop.adapters._common import RULES_BODY, exe
from raindrop.adapters._file_config import FileConfigAdapter


def gemini_dir() -> Path:
    return Path.home() / ".gemini"


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
