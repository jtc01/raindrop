import json

import httpx
import pytest
from typer.testing import CliRunner

from raindrop import cli
from raindrop import config
from raindrop import sync_cmd
from raindrop import constants
from raindrop.adapters import kiro

runner = CliRunner()
KEY = "rd_supersecretkey_1234567890abcdef"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("USER_PROMPT", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / ".raindrop")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / ".raindrop" / "config.json")
    (tmp_path / ".kiro").mkdir()
    config.save({"api_url": "https://api.test", "api_key": KEY, "agent_id": "a",
                 "team_id": "t", "display_name": "D", "focus": "", "tools": {},
                 "sharing": {"mode": "all"}})
    return tmp_path


def ev(name, sid="s-1", **kw):
    return {"hook_event_name": name, "session_id": sid, "cwd": "/proj", **kw}


class Pusher:
    def __init__(self):
        self.sent = []

    def __call__(self, req):
        body = json.loads(req.content)
        self.sent.append(body)
        return httpx.Response(200, json={"accepted": len(body["exchanges"])})

    @property
    def transport(self):
        return httpx.MockTransport(self)


def fire(payload, push):
    return sync_cmd.run_sync(payload, "kiro", push.transport)


def test_full_turn_is_captured_redacted_and_pushed(env):
    push = Pusher()
    assert fire(ev("UserPromptSubmit", prompt=f"use key {KEY} to fix the bug"), push) == 0
    assert fire(ev("PreToolUse", tool_name="fs_read",
                   tool_input={"operations": [{"path": "/proj/a.py", "mode": "Line"}]}), push) == 0
    assert fire(ev("PreToolUse", tool_name="execute_bash", tool_input={"command": "pytest -q"}), push) == 0
    assert push.sent == []  # nothing pushed until the turn ends

    assert fire(ev("Stop", assistant_response="Fixed it."), push) == 1
    [ex] = push.sent[0]["exchanges"]
    assert KEY not in json.dumps(push.sent)
    assert ex["assistant_text"] == "Fixed it." and ex["idx"] == 0 and ex["id"] == "s-1:0"
    assert ex["tool_summaries"] == ["fs_read /proj/a.py", "execute_bash: pytest -q"]
    assert ex["files_touched"] == ["/proj/a.py"]

    # next turn gets idx 1, and the first is not resent
    fire(ev("UserPromptSubmit", prompt="and again"), push)
    assert fire(ev("Stop", assistant_response="Done."), push) == 1
    assert [e["id"] for e in push.sent[1]["exchanges"]] == ["s-1:1"]


def test_prompt_from_env_var_and_interrupted_turn_dropped(env, monkeypatch):
    push = Pusher()
    monkeypatch.setenv("USER_PROMPT", "from env")
    fire({"hook_event_name": "UserPromptSubmit", "session_id": "x"}, push)
    monkeypatch.setenv("USER_PROMPT", "second try")
    fire({"hook_event_name": "UserPromptSubmit", "session_id": "x"}, push)  # first was interrupted
    fire({"hook_event_name": "Stop", "session_id": "x", "assistant_response": "ok"}, push)
    [ex] = push.sent[0]["exchanges"]
    assert ex["user_text"] == "second try"


def test_stop_without_prompt_and_failed_push_cursor(env):
    push = Pusher()
    assert fire(ev("Stop", assistant_response="orphan"), push) == 0

    bad = httpx.MockTransport(lambda r: httpx.Response(500, json={"error": "boom"}))
    sync_cmd.run_sync(ev("UserPromptSubmit", prompt="hi"), "kiro", bad)
    with pytest.raises(Exception):
        sync_cmd.run_sync(ev("Stop", assistant_response="yo"), "kiro", bad)
    # the failed push never advanced the cursor, so the next sync resends the turn
    assert fire(ev("Stop", assistant_response="yo"), push) == 1
    assert push.sent[0]["exchanges"][0]["user_text"] == "hi"


def test_session_id_fallback_and_sanitizing(env):
    a = kiro.KiroAdapter()
    assert a.session_from_hook({"session_id": "../../etc/passwd"}).session_id == "_/_/etc/passwd".replace("/", "_")
    one = a.session_from_hook({"cwd": "/proj"}).session_id
    assert one.startswith("kiro-") and one == a.session_from_hook({"cwd": "/proj"}).session_id


def test_install_remove_roundtrip_preserves_user_config(env):
    mcp = kiro.mcp_path()
    mcp.parent.mkdir(parents=True)
    mcp.write_text(json.dumps({"mcpServers": {"fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}}}))

    r = runner.invoke(cli.app, ["tools", "add", "kiro", "--dry-run", "--rules"])
    assert r.exit_code == 0 and "dry run" in r.output
    assert not kiro.hooks_path().exists() and not kiro.steering_path().exists()

    for _ in range(2):  # idempotent
        r = runner.invoke(cli.app, ["tools", "add", "kiro", "--yes", "--rules"])
        assert r.exit_code == 0, r.output
    assert "Kiro CLI [ok] MCP + auto-sync hook + rules" in r.output

    servers = json.loads(mcp.read_text())["mcpServers"]
    assert set(servers) == {"fetch", constants.MCP_SERVER_NAME}
    hooks = json.loads(kiro.hooks_path().read_text())
    assert hooks["version"] == "v1"
    assert [h["trigger"] for h in hooks["hooks"]] == ["UserPromptSubmit", "PreToolUse", "Stop"]
    for h in hooks["hooks"]:
        assert h["action"]["command"].endswith("sync --tool kiro --fail-open")
    assert constants.RULES_START in kiro.steering_path().read_text()
    assert config.load()["tools"]["kiro"]["hook"] is True

    r = runner.invoke(cli.app, ["tools", "remove", "kiro"])
    assert r.exit_code == 0
    assert set(json.loads(mcp.read_text())["mcpServers"]) == {"fetch"}
    assert not kiro.hooks_path().exists() and not kiro.steering_path().exists()


def test_fail_open_exits_zero_and_logs(env):
    r = runner.invoke(cli.app, ["sync", "--tool", "kiro", "--fail-open"], input="not json")
    assert r.exit_code == 0
    assert "kiro:" in (env / ".raindrop" / "sync.log").read_text()
    r = runner.invoke(cli.app, ["sync", "--tool", "kiro"], input="not json")
    assert r.exit_code == 1


def test_successful_sync_prints_nothing_to_stdout(env):
    # Kiro adds a hook's stdout to the agent's context, so stdout must stay empty.
    payload = ev("Stop", assistant_response="x")
    r = runner.invoke(cli.app, ["sync", "--tool", "kiro", "--fail-open"], input=json.dumps(payload))
    assert r.exit_code == 0 and r.stdout == ""
