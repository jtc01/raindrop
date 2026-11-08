"""Local Raindrop config: ~/.raindrop/config.json (0600, dir 0700).

Keys match RaindropConfig in models.py: api_url, api_key, agent_id, team_id,
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


def delete() -> bool:
    """Removes the local config (used by `raindrop leave`). Returns True if a file was removed."""
    try:
        CONFIG_PATH.unlink()
        return True
    except FileNotFoundError:
        return False


def permissions_ok() -> bool | None:
    """True if config.json is private to the user. None on Windows, where Unix modes don't apply."""
    if os.name == "nt":
        return None
    try:
        return (CONFIG_PATH.stat().st_mode & 0o077) == 0
    except FileNotFoundError:
        return False


# ---------- sharing (checked by sync) ----------
#
# config["sharing"] = {
#   "mode": "opt_in" | "all",   # opt_in (default): only shared_repos upload
#   "shared_repos": [...],        # repo roots added with `raindrop share --repo`
#   "paused": bool,              # `raindrop pause`: wins over everything
#   "paused_repos": [...],        # `raindrop pause --repo`
# }

SHARING_MODES = ("opt_in", "all")
DEFAULT_SHARING_MODE = "opt_in"

def _main_checkout(root: Path) -> Path:
    """A linked git worktree has a `.git` FILE pointing into the main repo's `.git/worktrees/<name>`.
    Return the main checkout's folder for it, so worktrees count as the repo they belong to
    (sharing `raindrop share --repo` in the main folder also covers its worktrees). Any other
    folder is returned unchanged."""
    dotgit = root / ".git"
    try:
        if not dotgit.is_file():
            return root
        text = dotgit.read_text(encoding="utf-8").strip()
        if not text.startswith("gitdir:"):
            return root
        gitdir = (root / text[len("gitdir:"):].strip()).resolve()
        commondir = gitdir / "commondir"
        if gitdir.parent.name != "worktrees" or not commondir.is_file():
            return root  # a submodule or something else, not a linked worktree
        common = (gitdir / commondir.read_text(encoding="utf-8").strip()).resolve()
        return common.parent if common.name == ".git" else root
    except (OSError, ValueError):
        return root


def repo_root(path: str | os.PathLike | None = None) -> Path:
    """The git repo containing `path` (default: cwd), or the path itself if not in a repo.
    A linked worktree resolves to its main checkout."""
    start = Path(path or Path.cwd()).resolve()
    for p in (start, *start.parents):
        if (p / ".git").exists():
            return _main_checkout(p)
    return start


def sharing_paused(path: str | os.PathLike | None = None, cfg: dict | None = None) -> bool:
    """True if sharing is paused globally or for the repo containing `path`.
    Sync should call this with the session's working directory and, when it returns
    True, advance the cursor WITHOUT uploading, so paused content is never shared later."""
    cfg = cfg if cfg is not None else (load() or {})
    sharing = cfg.get("sharing", {})
    if sharing.get("paused"):
        return True
    return str(repo_root(path)) in sharing.get("paused_repos", [])


def sharing_mode(cfg: dict | None = None) -> str:
    """'opt_in' unless the config explicitly says 'all'. Missing or unknown values
    (including every config written before modes existed) mean opt-in."""
    cfg = cfg if cfg is not None else (load() or {})
    mode = cfg.get("sharing", {}).get("mode")
    return mode if mode in SHARING_MODES else DEFAULT_SHARING_MODE


def should_share(path: str | os.PathLike | None = None, cfg: dict | None = None) -> bool:
    """Whether content from the repo containing `path` may be uploaded.
    False if paused globally or for this repo. Otherwise true in 'all' mode, and in
    'opt_in' mode only if the repo root was added with `raindrop share --repo`.
    Sync calls this with the session's working directory and, when it returns False,
    advances the cursor WITHOUT uploading, so skipped content is never shared later."""
    cfg = cfg if cfg is not None else (load() or {})
    if sharing_paused(path, cfg):
        return False
    if sharing_mode(cfg) == "all":
        return True
    return str(repo_root(path)) in cfg.get("sharing", {}).get("shared_repos", [])


def build(api_url: str, resp: dict, display_name: str, focus: str, existing: dict | None) -> dict:
    """Turns a /teams or /join response into a config. Keeps installed tools
    only if rejoining the same team."""
    same_team = existing and existing.get("team_id") == resp["team_id"]
    tools = existing.get("tools", {}) if same_team else {}
    sharing = dict(existing.get("sharing", {})) if existing else {}  # sharing settings survive rejoining
    sharing.setdefault("mode", DEFAULT_SHARING_MODE)
    return {
        "api_url": api_url.rstrip("/"),
        "api_key": resp["api_key"],
        "agent_id": resp["agent_id"],
        "team_id": resp["team_id"],
        "team_name": resp.get("team_name") or "",
        "display_name": display_name,
        "focus": focus,
        "tools": tools,
        "sharing": sharing,
    }
