import json
from pathlib import Path

import pytest

from raindrop import constants
from raindrop.adapters.claude import ClaudeCodeAdapter


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda name: None)  # force file-merge path
    return tmp_path


def line(**kw):
    return json.dumps(kw) + "\n"


def user(text, ts="2026-01-01T00:00:00Z"):
    return line(type="user", timestamp=ts, message={"role": "user", "content": text})


def tool_result():
    return line(type="user", message={"content": [{"type": "tool_result", "content": "x"}]})


def asst(*blocks):
    return line(type="assistant", message={"content": list(blocks)})


def text(t):
    return {"type": "text", "text": t}


def tool(name, **inp):
    return {"type": "tool_use", "name": name, "input": inp}


def session(tmp_path, content):
    (tmp_path / ".git").mkdir(exist_ok=True)  # tmp_path is the repo root, wherever the temp dir lives
    p = tmp_path / "s.jsonl"
    p.write_bytes(content.encode())  # bytes, not write_text: Windows would turn \n into \r\n
    return ClaudeCodeAdapter().session_from_hook(
        {"session_id": "abc", "transcript_path": str(p), "cwd": str(tmp_path)}
    ), p


def test_parses_exchange_and_skips_tool_results(tmp_path):
    ref, _ = session(tmp_path, user("fix it") + asst(text("looking"), tool("Edit", file_path="a.py"))
                     + tool_result() + asst({"type": "thinking", "thinking": "hm"}, text("done")))
    res = ClaudeCodeAdapter().read_new(ref, 0)
    [ex] = res.exchanges
    assert (ex.idx, ex.user_text, ex.assistant_text) == (0, "fix it", "done")
    assert ex.tool_summaries == ["Edit a.py"] and ex.files_touched == ["a.py"]


def test_holds_back_unanswered_prompt(tmp_path):
    first = user("one") + asst(text("a1"))
    ref, p = session(tmp_path, first + user("two"))
    res = ClaudeCodeAdapter().read_new(ref, 0)
    assert [e.user_text for e in res.exchanges] == ["one"]
    assert res.new_cursor == len(first.encode())
    # reply arrives; next run picks up "two" with a stable idx
    p.write_bytes((first + user("two") + asst(text("a2"))).encode())
    res2 = ClaudeCodeAdapter().read_new(ref, res.new_cursor)
    assert [(e.idx, e.user_text) for e in res2.exchanges] == [(1, "two")]
    assert ClaudeCodeAdapter().read_new(ref, res2.new_cursor).exchanges == []


def test_ignores_partial_trailing_line(tmp_path):
    ref, _ = session(tmp_path, user("q") + asst(text("a")) + '{"type": "assi')
    res = ClaudeCodeAdapter().read_new(ref, 0)
    assert len(res.exchanges) == 1


def test_install_uninstall_roundtrip_is_idempotent(home):
    constants.claude_settings_path().parent.mkdir()
    constants.claude_settings_path().write_text(json.dumps(
        {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other"}]}]}, "model": "x"}))
    constants.claude_md_path().write_text("# mine\n")
    a = ClaudeCodeAdapter()
    for _ in range(2):
        status = a.apply(a.plan_install(rules=True))
    assert status.mcp and status.hook and status.rules
    settings = json.loads(constants.claude_settings_path().read_text())
    stop = settings["hooks"]["Stop"]
    assert len(stop) == 2 and settings["model"] == "x"
    assert constants.claude_md_path().read_text().count(constants.RULES_START) == 1
    assert constants.MCP_SERVER_NAME in json.loads(constants.claude_json_path().read_text())["mcpServers"]

    a.uninstall(status.to_tool_record())
    settings = json.loads(constants.claude_settings_path().read_text())
    assert settings["hooks"]["Stop"] == [{"hooks": [{"type": "command", "command": "other"}]}]
    assert constants.claude_md_path().read_text() == "# mine\n"
    assert constants.MCP_SERVER_NAME not in json.loads(constants.claude_json_path().read_text()).get("mcpServers", {})


def test_plan_does_not_modify(home):
    ClaudeCodeAdapter().plan_install(rules=True)
    assert not constants.claude_settings_path().exists()
    assert not constants.claude_json_path().exists()
