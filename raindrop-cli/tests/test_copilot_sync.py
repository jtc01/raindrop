"""Copilot transcript parsing and the sync path (event shapes taken from real VS Code
GitHub.copilot-chat/transcripts/<sessionId>.jsonl files)."""
import json
from datetime import datetime, timezone

import httpx
import pytest

from raindrop import config, sync_cmd
from raindrop.adapters import copilot
from raindrop.adapters.copilot import CopilotAdapter
from raindrop.errors import TranscriptError

KEY = "rd_supersecretkey_1234567890abcdef"
LEAK = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"
SID = "bc2fe3cb-216e-4c85-b203-75064a38a1d2"
TS = "2026-11-07T18:00:00.000Z"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("COPILOT_HOME", raising=False)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / ".raindrop")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / ".raindrop" / "config.json")
    monkeypatch.setattr(copilot, "_vscode_user_dirs", lambda: [tmp_path / "Code" / "User"])
    config.save({"api_url": "https://api.test", "api_key": KEY, "agent_id": "a", "team_id": "t",
                 "display_name": "D", "focus": "", "tools": {}, "sharing": {"mode": "all"}})
    return tmp_path


def ev(type_, **data):
    return {"type": type_, "data": data, "id": "x", "timestamp": TS, "parentId": None}


def user(text):
    return ev("user.message", content=text, attachments=[])


def reply(text, reasoning="private reasoning"):
    return ev("assistant.message", messageId="m", content=text, toolRequests=[], reasoningText=reasoning)


def tool(name, **args):
    return ev("tool.execution_start", toolCallId="c", toolName=name, arguments=args)


def write(tmp_path, *events):
    p = tmp_path / f"{SID}.jsonl"
    p.write_bytes("".join(json.dumps(e) + "\n" for e in events).encode())
    return p


def payload(path, **kw):
    return {"sessionId": SID, "transcriptPath": str(path), "cwd": str(path.parent), **kw}


def read(path, cursor=0):
    a = CopilotAdapter()
    return a.read_new(a.session_from_hook(payload(path)), cursor)


START = ev("session.start", sessionId=SID, producer="copilot-agent", version=1)


def test_turn_with_tools_becomes_one_exchange(env):
    p = write(env, START, user("fix the bug"),
              ev("assistant.turn_start", turnId="0"),
              reply("Let me look first.", reasoning="hidden thoughts"),
              tool("read_file", filePath="C:/proj/src/app.py", startLine=1, endLine=40),
              ev("tool.execution_complete", toolCallId="c", success=True),
              tool("run_in_terminal", command="pytest -q", explanation="run tests", goal="g", mode="sync"),
              tool("apply_patch", input="*** Begin Patch\n*** Update File: C:/proj/src/app.py\n*** Add File: C:/proj/new.py\n*** End Patch", explanation="e"),
              reply("Fixed it."),
              ev("assistant.turn_end", turnId="1"))
    [e] = read(p).exchanges
    assert e.user_text == "fix the bug" and e.idx == 0 and e.session_id == SID
    assert e.assistant_text == "Fixed it."  # text after the last tool call
    assert "hidden thoughts" not in json.dumps(e.__dict__)
    assert e.files_touched == ["C:/proj/src/app.py", "C:/proj/new.py"]
    assert e.tool_summaries[0] == "read_file C:/proj/src/app.py"
    assert e.tool_summaries[1] == "run_in_terminal: pytest -q"
    assert e.tool_summaries[2] == "apply_patch C:/proj/src/app.py"
    assert e.ts == int(datetime(2026, 11, 7, 18, tzinfo=timezone.utc).timestamp())


def test_unanswered_prompt_is_held_back_and_picked_up_later(env):
    first = [START, user("one"), reply("a1")]
    p = write(env, *first, user("two"))
    res = read(p)
    assert [e.user_text for e in res.exchanges] == ["one"]
    assert res.new_cursor == len("".join(json.dumps(x) + "\n" for x in first).encode())

    p = write(env, *first, user("two"), reply("a2"))
    res2 = read(p, res.new_cursor)
    assert [(e.idx, e.user_text) for e in res2.exchanges] == [(1, "two")]  # idx stays stable
    assert read(p, res2.new_cursor).exchanges == []


def test_empty_replies_and_interrupted_prompts(env):
    p = write(env, START, user("one"), reply("a1"),
              user("interrupted"), reply(""),            # tool-only message, no text
              user("three"), reply("a3"))
    assert [(e.idx, e.user_text) for e in read(p).exchanges] == [(0, "one"), (2, "three")]


def test_partial_trailing_line_and_garbage_are_ignored(env):
    p = env / "t.jsonl"
    good = json.dumps(user("q")) + "\n" + json.dumps(reply("a")) + "\n"
    p.write_bytes((good + "not json\n" + '{"type": "assistant.mes').encode())
    res = read(p)
    assert len(res.exchanges) == 1 and res.new_cursor < p.stat().st_size


def test_transcript_is_found_by_session_id_when_the_hook_gives_no_path(env):
    d = env / "Code" / "User" / "workspaceStorage" / "abc123" / "GitHub.copilot-chat" / "transcripts"
    d.mkdir(parents=True)
    (d / f"{SID}.jsonl").write_bytes((json.dumps(user("q")) + "\n" + json.dumps(reply("a")) + "\n").encode())
    a = CopilotAdapter()
    for given in ({}, {"transcriptPath": str(env / "stale.jsonl")}):
        s = a.session_from_hook({"sessionId": SID, **given})
        assert s.transcript_path == d / f"{SID}.jsonl" and len(a.read_new(s, 0).exchanges) == 1


def test_snake_case_vs_code_payload_is_accepted(env):
    p = write(env, START, user("q"), reply("a"))
    s = CopilotAdapter().session_from_hook(
        {"hook_event_name": "Stop", "session_id": SID, "transcript_path": str(p), "cwd": "C:/x"})
    assert s.session_id == SID and s.transcript_path == p and s.cwd == "C:/x"


def test_bad_payloads_are_clear_errors(env):
    a = CopilotAdapter()
    for bad in ({}, {"sessionId": "../../etc"}, {"sessionId": 5}):
        with pytest.raises(TranscriptError, match="sessionId"):
            a.session_from_hook(bad)
    s = a.session_from_hook({"sessionId": SID})  # valid id but no transcript anywhere
    with pytest.raises(TranscriptError, match="Transcript not found"):
        a.read_new(s, 0)


def test_sync_redacts_and_uploads_once(env):
    p = write(env, START, user(f"use {LEAK} and {KEY}"), reply(f"ok {KEY}"))
    sent = []

    def handler(req):
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"accepted": len(sent[-1]["exchanges"]), "ids": [e["id"] for e in sent[-1]["exchanges"]]})

    transport = httpx.MockTransport(handler)
    assert sync_cmd.run_sync(payload(p), "copilot", transport) == 1
    blob = json.dumps(sent)
    assert LEAK not in blob and KEY not in blob and "[REDACTED" in blob
    assert sent[0]["exchanges"][0]["session_id"] == SID  # ids survive redaction
    assert sync_cmd.run_sync(payload(p), "copilot", transport) == 0  # cursor advanced


def test_copilot_prints_nothing_on_stdout_for_the_hook(env):
    assert CopilotAdapter().hook_stdout() is None
