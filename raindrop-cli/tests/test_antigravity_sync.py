"""Antigravity transcript parsing and the sync path (record shapes taken from a real
~/.gemini/antigravity/brain/<id>/.system_generated/logs/transcript.jsonl)."""
import json

import httpx
import pytest
from typer.testing import CliRunner

from raindrop import cli, config, sync_cmd
from raindrop.adapters.antigravity import AntigravityAdapter

runner = CliRunner()
KEY = "rd_supersecretkey_1234567890abcdef"
LEAK = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"
CID = "dbf33081-21f1-4738-8699-4f9bc63aae79"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / ".raindrop")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / ".raindrop" / "config.json")
    config.save({"api_url": "https://api.test", "api_key": KEY, "agent_id": "a", "team_id": "t",
                 "display_name": "D", "focus": "", "tools": {}, "sharing": {"mode": "all"}})
    return tmp_path


def step(i, type_, source, content="", **kw):
    return {"step_index": i, "source": source, "type": type_, "status": "DONE",
            "created_at": "2026-07-21T09:18:28Z", "content": content, **kw}


def user(i, text):
    return step(i, "USER_INPUT", "USER_EXPLICIT",
                f"<USER_REQUEST>\n{text}\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\nlocal time\n</ADDITIONAL_METADATA>")


def reply(i, text, thinking="secret reasoning"):
    return step(i, "PLANNER_RESPONSE", "MODEL", text, thinking=thinking)


def write(tmp_path, *steps):
    p = tmp_path / "transcript.jsonl"
    p.write_bytes("".join(json.dumps(s) + "\n" for s in steps).encode())
    return p


def payload(path, **kw):
    return {"conversationId": CID, "transcriptPath": str(path), "workspacePaths": [str(path.parent)], **kw}


def read(path, cursor=0):
    a = AntigravityAdapter()
    return a.read_new(a.session_from_hook(payload(path)), cursor)


def test_turn_becomes_one_exchange_with_unwrapped_prompt_and_final_reply(env):
    p = write(env,
              user(0, "fix the bug"),
              reply(1, "Let me look first.", thinking="hidden"),
              step(2, "VIEW_FILE", "MODEL", "File Path: `file:///C:/proj/src/app.py`"),
              step(3, "CODE_ACTION", "MODEL", "Created file file:///C:/proj/src/fix%20it.py"),
              step(4, "RUN_COMMAND", "MODEL", "Output: ok"),
              step(5, "CHECKPOINT", "SYSTEM", "internal"),
              reply(6, "Fixed it."))
    [e] = read(p).exchanges
    assert e.user_text == "fix the bug" and e.idx == 0 and e.session_id == CID
    assert e.assistant_text == "Fixed it."  # text after the last tool step; thinking never shared
    assert "hidden" not in json.dumps(e.__dict__) and "internal" not in json.dumps(e.__dict__)
    assert e.files_touched == ["C:/proj/src/app.py", "C:/proj/src/fix it.py"]
    assert e.tool_summaries[0] == "view_file C:/proj/src/app.py" and e.tool_summaries[2] == "run_command"
    assert e.ts == 1784625508


def test_unanswered_prompt_is_held_back_and_picked_up_later(env):
    first = [user(0, "one"), reply(1, "a1")]
    p = write(env, *first, user(2, "two"))
    res = read(p)
    assert [e.user_text for e in res.exchanges] == ["one"]
    assert res.new_cursor == len(("".join(json.dumps(s) + "\n" for s in first)).encode())  # start of "two"

    p = write(env, *first, user(2, "two"), reply(3, "a2"))
    res2 = read(p, res.new_cursor)
    assert [(e.idx, e.user_text) for e in res2.exchanges] == [(1, "two")]  # idx stays stable
    assert read(p, res2.new_cursor).exchanges == []


def test_prompt_without_reply_in_the_middle_is_skipped_but_keeps_numbering(env):
    p = write(env, user(0, "one"), reply(1, "a1"), user(2, "interrupted"), user(3, "three"), reply(4, "a3"))
    assert [(e.idx, e.user_text) for e in read(p).exchanges] == [(0, "one"), (2, "three")]


def test_partial_trailing_line_is_left_alone_and_garbage_is_ignored(env):
    p = env / "t.jsonl"
    good = json.dumps(user(0, "q")) + "\n" + json.dumps(reply(1, "a")) + "\n"
    p.write_bytes((good + "not json\n" + '{"step_index": 9, "ty').encode())
    res = read(p)
    assert len(res.exchanges) == 1 and res.new_cursor < p.stat().st_size


def test_antigravity_own_files_are_not_reported_as_touched(env):
    p = write(env, user(0, "q"),
              step(1, "CODE_ACTION", "MODEL", "Created file file:///C:/Users/me/.gemini/antigravity/brain/x/plan.md"),
              reply(2, "done"))
    assert read(p).exchanges[0].files_touched == []


def test_transcript_path_may_be_the_conversation_folder_or_missing(env):
    brain = env / ".gemini" / "antigravity" / "brain" / CID
    logs = brain / ".system_generated" / "logs"
    logs.mkdir(parents=True)
    (logs / "transcript.jsonl").write_bytes((json.dumps(user(0, "q")) + "\n" + json.dumps(reply(1, "a")) + "\n").encode())
    a = AntigravityAdapter()
    for given in (str(brain), None, str(env / "wrong")):  # folder, absent, stale -> same file found
        s = a.session_from_hook({"conversationId": CID, "transcriptPath": given})
        assert s.transcript_path == logs / "transcript.jsonl"
        assert len(a.read_new(s, 0).exchanges) == 1


def test_bad_payload_is_a_clear_error(env):
    from raindrop.errors import TranscriptError
    with pytest.raises(TranscriptError, match="conversationId"):
        AntigravityAdapter().session_from_hook({"transcriptPath": "x"})
    with pytest.raises(TranscriptError, match="conversationId"):
        AntigravityAdapter().session_from_hook({"conversationId": "../../etc"})


def test_sync_redacts_and_uploads_once(env):
    p = write(env, user(0, f"use {LEAK} and {KEY}"), reply(1, f"ok {KEY}"))
    sent = []

    def handler(req):
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"accepted": len(sent[-1]["exchanges"])})

    transport = httpx.MockTransport(handler)
    assert sync_cmd.run_sync(payload(p), "antigravity", transport) == 1
    blob = json.dumps(sent)
    assert LEAK not in blob and KEY not in blob and "[REDACTED" in blob
    assert sent[0]["exchanges"][0]["session_id"] == CID  # ids survive redaction
    assert sync_cmd.run_sync(payload(p), "antigravity", transport) == 0  # cursor advanced


def test_stop_hook_always_answers_allow_on_stdout_even_when_sync_fails(env):
    ok = runner.invoke(cli.app, ["sync", "--tool", "antigravity", "--fail-open"],
                       input=json.dumps(payload(write(env, user(0, "q")))))  # nothing to upload
    assert ok.exit_code == 0 and json.loads(ok.stdout.strip().splitlines()[-1]) == {"decision": "allow"}

    bad = runner.invoke(cli.app, ["sync", "--tool", "antigravity", "--fail-open"], input="not json")
    assert bad.exit_code == 0
    assert json.loads(bad.stdout.strip().splitlines()[-1]) == {"decision": "allow"}
    assert "antigravity:" in (env / ".raindrop" / "sync.log").read_text()


def test_other_tools_print_nothing_on_stdout(env):
    r = runner.invoke(cli.app, ["sync", "--tool", "claude-code", "--fail-open"], input="not json")
    assert r.exit_code == 0 and r.stdout.strip() == ""
