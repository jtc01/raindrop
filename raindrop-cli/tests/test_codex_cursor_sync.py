"""Codex and Cursor go through the same sync + redaction path as Claude Code."""
import json
import sys
from pathlib import Path

import httpx
import pytest

# stophooks/ lives beside raindrop-cli/ in the repo, not inside the installed package.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from raindrop import config  # noqa: E402
from raindrop import sync_cmd  # noqa: E402

KEY = "rd_supersecretkey_1234567890abcdef"
LEAK = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"
SID = "019a1b2c-3d4e-7f80-9a1b-2c3d4e5f6a7b"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / ".raindrop")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / ".raindrop" / "config.json")
    config.save({"api_url": "https://api.test", "api_key": KEY, "agent_id": "a",
                 "team_id": "t", "display_name": "D", "focus": "", "tools": {},
                 "sharing": {"mode": "all"}})
    return tmp_path


def _transport(sent):
    def handler(req):
        sent.append(json.loads(req.content))
        ids = [ex["id"] for ex in sent[-1]["exchanges"]]
        return httpx.Response(200, json={"accepted": len(ids), "ids": ids})
    return httpx.MockTransport(handler)


def _check(sent):
    blob = json.dumps(sent)
    assert LEAK not in blob and KEY not in blob
    assert "[REDACTED" in blob
    ex = sent[0]["exchanges"][0]
    assert ex["session_id"] == SID  # ids survive redaction


def test_codex_sync_redacts(env):
    recs = [
        {"timestamp": "2026-11-07T18:00:00Z", "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": f"use {LEAK}"}]}},
        {"timestamp": "2026-11-07T18:00:02Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant", "phase": "final",
                     "content": [{"type": "output_text", "text": f"done, key {KEY}"}]}},
    ]
    p = env / f"rollout-{SID}.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    sent = []
    n = sync_cmd.run_sync({"session_id": SID, "transcript_path": str(p), "cwd": str(env)},
                          "codex", _transport(sent))
    assert n == 1
    _check(sent)


def test_cursor_sync_redacts(env, monkeypatch):
    base = {"conversation_id": SID, "workspace_roots": [str(env)]}
    sent, n = [], 0
    for payload in (
        {**base, "hook_event_name": "beforeSubmitPrompt", "prompt": f"use {LEAK}"},
        {**base, "hook_event_name": "afterAgentResponse", "text": f"done, key {KEY}"},
        {**base, "hook_event_name": "stop"},
    ):
        n += sync_cmd.run_sync(payload, "cursor", _transport(sent))
    assert n == 1
    _check(sent)
