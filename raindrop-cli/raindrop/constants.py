"""raindrop/constants.py

Paths and fixed strings used across raindrop.

Paths are functions, not module-level constants, so they are computed when
called. That way tests can point HOME at a temp directory and nothing touches
the real ~/.raindrop or ~/.claude.
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# raindrop's own files
# ---------------------------------------------------------------------------


def raindrop_home() -> Path:
    return Path.home() / ".raindrop"


def config_path() -> Path:
    return raindrop_home() / "config.json"


def cursor_dir() -> Path:
    return raindrop_home() / "cursors"


def log_path() -> Path:
    return raindrop_home() / "sync.log"


# ---------------------------------------------------------------------------
# Claude Code's files
# ---------------------------------------------------------------------------


def claude_dir() -> Path:
    return Path.home() / ".claude"


def claude_settings_path() -> Path:
    return claude_dir() / "settings.json"


def claude_md_path() -> Path:
    return claude_dir() / "CLAUDE.md"


def claude_json_path() -> Path:
    """Fallback location for MCP registration if `claude mcp add` isn't usable."""
    return Path.home() / ".claude.json"


# ---------------------------------------------------------------------------
# Fixed strings
# ---------------------------------------------------------------------------

MCP_SERVER_NAME = "raindrop"

# Substring used to find our own hook entries so re-running install replaces
# them instead of adding duplicates.
HOOK_MARKER = "raindrop sync"

# Delimiters for the block we add to CLAUDE.md, so it can be updated or removed
# without touching the user's own content.
RULES_START = "<!-- raindrop:start -->"
RULES_END = "<!-- raindrop:end -->"

# Max exchanges per push_messages request.
BATCH_SIZE = 50
