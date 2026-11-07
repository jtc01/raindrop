import json

import httpx
import pytest
from typer.testing import CliRunner

from raindrop import api_client
from raindrop import cli
from raindrop import config
from raindrop import sync_cmd
from raindrop import constants
from raindrop.adapters._common import is_our_hook_command

runner = CliRunner()
KEY = "rd_supersecretkey_1234567890abcdef"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / ".raindrop")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / ".raindrop" / "config.json")
    (tmp_path / ".claude").mkdir()
    config.save({"api_url": "https://api.test", "api_key": KEY, "agent_id": "a",
                 "team_id": "t", "display_name": "D", "focus": "", "tools": {}})
    return tmp_path


def transcript(tmp_path, *lines):
    p = tmp_path / "t.jsonl"
    p.write_text("".join(json.dumps(x) + "\n" for x in lines))
    return {"session_id": "sess1", "transcript_path": str(p), "cwd": str(tmp_path)}


def turn(prompt, reply):
    return [{"type": "user", "timestamp": "2026-01-01T00:00:00Z", "message": {"content": prompt}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": reply}]}}]


def test_tools_add_dry_run_then_add_then_remove(env):
    r = runner.invoke(cli.app, ["tools", "add", "claude-code", "--dry-run"])
    assert r.exit_code == 0 and "dry run" in r.output
    assert not constants.claude_settings_path().exists()

    r = runner.invoke(cli.app, ["tools", "add", "claude-code", "--yes", "--rules"])
    assert r.exit_code == 0, r.output
    assert "Claude Code [ok] MCP + auto-sync hook + rules" in r.output
    cfg = config.load()
    assert cfg["tools"]["claude-code"]["hook"] is True
    assert cfg["tools"]["claude-code"]["extra"]["mcp_method"] == "file"
    assert is_our_hook_command(json.loads(constants.claude_settings_path().read_text())
                               ["hooks"]["Stop"][0]["hooks"][0]["command"])

    r = runner.invoke(cli.app, ["tools", "remove", "claude-code"])
    assert r.exit_code == 0 and config.load()["tools"] == {}
    assert "Stop" not in json.loads(constants.claude_settings_path().read_text()).get("hooks", {})


def test_tools_add_requires_join(env):
    config.CONFIG_PATH.unlink()
    r = runner.invoke(cli.app, ["tools", "add", "claude-code", "--yes"])
    assert r.exit_code == 1 and "join" in r.output


def test_sync_redacts_pushes_and_advances_cursor(env):
    payload = transcript(env, *turn(f"my key is {KEY}", "ok"), *turn("second", "done")[:1])
    sent = []

    def handler(req):
        sent.append(json.loads(req.content))
        assert req.headers["authorization"] == f"Bearer {KEY}"
        return httpx.Response(200, json={"accepted": len(sent[-1]["exchanges"])})

    n = sync_cmd.run_sync(payload, "claude-code", httpx.MockTransport(handler))
    assert n == 1  # unanswered 2nd prompt held back
    assert KEY not in json.dumps(sent)
    cur = (env / ".raindrop/cursors/claude-code/sess1").read_text()
    assert int(cur) > 0
    # nothing new -> nothing sent
    assert sync_cmd.run_sync(payload, "claude-code", httpx.MockTransport(handler)) == 0


def test_failed_push_leaves_cursor_unchanged(env):
    payload = transcript(env, *turn("hi", "yo"))
    bad = httpx.MockTransport(lambda r: httpx.Response(500, json={"error": "boom"}))
    with pytest.raises(api_client.ApiError):
        sync_cmd.run_sync(payload, "claude-code", bad)
    assert not (env / ".raindrop/cursors/claude-code/sess1").exists()


def test_sync_command_reads_stdin(env):
    r = runner.invoke(cli.app, ["sync", "--tool", "claude-code"], input="not json")
    assert r.exit_code == 1 and "failed" in r.output
