"""Local Raindrop config: ~/.raindrop/config.json (0600, dir 0700).

Keys match TeamctxConfig in models.py: api_url, api_key, agent_id, team_id,
display_name, focus, tools. Also stores team_name for display.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("RAINDROP_HOME", Path.home() / ".raindrop"))
CONFIG_PATH = CONFIG_DIR / "config.json"


def load() -> dict | None:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except FileNotFoundError:
        return None


def save(cfg: dict) -> Path:
    """Writes atomically with 0600 permissions so the key is never world-readable,
    not even briefly."""
    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_PATH)
    return CONFIG_PATH


def build(api_url: str, resp: dict, display_name: str, focus: str, existing: dict | None) -> dict:
    """Turns a /teams or /join response into a config. Keeps installed tools
    only if rejoining the same team."""
    tools = existing.get("tools", {}) if existing and existing.get("team_id") == resp["team_id"] else {}
    return {
        "api_url": api_url.rstrip("/"),
        "api_key": resp["api_key"],
        "agent_id": resp["agent_id"],
        "team_id": resp["team_id"],
        "team_name": resp.get("team_name") or "",
        "display_name": display_name,
        "focus": focus,
        "tools": tools,
    }
