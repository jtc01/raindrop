"""Opt-in sharing: config.should_share and the sync skip that uses it."""
import json

import httpx
import pytest

from raindrop import config, sync_cmd

KEY = "rd_supersecretkey_1234567890abcdef"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / ".raindrop")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / ".raindrop" / "config.json")
    return tmp_path


def _cfg(sharing):
    return {"api_url": "https://api.test", "api_key": KEY, "agent_id": "a", "team_id": "t",
            "display_name": "D", "focus": "", "tools": {}, "sharing": sharing}


def _repo(tmp_path, name):
    root = tmp_path / name
    (root / ".git").mkdir(parents=True)
    (root / "src").mkdir()
    return root


def test_default_is_opt_in_and_unshared(env):
    root = _repo(env, "work")
    for sharing in ({}, {"mode": "bogus"}):
        assert config.sharing_mode(_cfg(sharing)) == "opt_in"
        assert config.should_share(root / "src", _cfg(sharing)) is False
    # configs written before modes existed have no "sharing" key at all
    assert config.should_share(root, {"api_key": KEY}) is False


def test_opt_in_shares_only_listed_repo_roots(env):
    shared, other = _repo(env, "shared"), _repo(env, "other")
    cfg = _cfg({"mode": "opt_in", "shared_repos": [str(shared)]})
    assert config.should_share(shared / "src", cfg) is True  # subdir resolves to the repo root
    assert config.should_share(other / "src", cfg) is False


def test_all_mode_shares_everything_but_pause_wins(env):
    root = _repo(env, "r")
    assert config.should_share(root, _cfg({"mode": "all"})) is True
    assert config.should_share(root, _cfg({"mode": "all", "paused": True})) is False
    assert config.should_share(root, _cfg({"mode": "all", "paused_repos": [str(root)]})) is False
    # pause also beats an explicit share of the same repo
    assert config.should_share(root, _cfg({"shared_repos": [str(root)], "paused_repos": [str(root)]})) is False
    assert config.sharing_paused(root, _cfg({"paused": True})) is True


def test_build_defaults_mode_and_keeps_existing_sharing():
    resp = {"api_key": "k", "agent_id": "a", "team_id": "t"}
    assert config.build("https://x", resp, "D", "", None)["sharing"] == {"mode": "opt_in"}
    existing = {"team_id": "t", "sharing": {"mode": "all", "paused_repos": ["/p"]}}
    assert config.build("https://x", resp, "D", "", existing)["sharing"] == {"mode": "all", "paused_repos": ["/p"]}


def _transcript(root):
    p = root / "t.jsonl"
    p.write_text("".join(json.dumps(x) + "\n" for x in [
        {"type": "user", "message": {"content": "hello"}, "timestamp": "2026-01-01T00:00:00Z"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
    ]))
    return {"session_id": "sess1", "transcript_path": str(p), "cwd": str(root)}


def test_sync_skips_unshared_repo_and_advances_cursor(env):
    root = _repo(env, "work")
    config.save(_cfg({"mode": "opt_in"}))
    sent = []

    def handler(req):
        sent.append(json.loads(req.content))
        ids = [ex["id"] for ex in sent[-1]["exchanges"]]
        return httpx.Response(200, json={"accepted": len(ids), "ids": ids})

    payload = _transcript(root)
    assert sync_cmd.run_sync(payload, "claude-code", httpx.MockTransport(handler)) == 0
    assert sent == []  # nothing uploaded, nothing queued
    cursor = sync_cmd._cursor_path("claude-code", "sess1")
    assert cursor.exists() and int(cursor.read_text()) > 0  # content is skipped for good

    # sharing the repo later does NOT upload what was skipped
    cfg = config.load()
    cfg["sharing"]["shared_repos"] = [str(root)]
    config.save(cfg)
    assert sync_cmd.run_sync(payload, "claude-code", httpx.MockTransport(handler)) == 0
    assert sent == []

    # but new content in the now-shared repo does upload
    with open(payload["transcript_path"], "a") as f:
        f.write(json.dumps({"type": "user", "message": {"content": "again"}}) + "\n")
        f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "yes"}]}}) + "\n")
    assert sync_cmd.run_sync(payload, "claude-code", httpx.MockTransport(handler)) == 1
    assert [e["user_text"] for e in sent[0]["exchanges"]] == ["again"]
