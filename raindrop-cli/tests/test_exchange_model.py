"""Each exchange carries the AI model that answered the prompt (shown on the dashboard)."""
import json
from pathlib import Path

from raindrop import config
from raindrop.adapters.base import SessionRef
from raindrop.adapters.claude import ClaudeCodeAdapter
from raindrop.adapters.codex import CodexAdapter
from raindrop.adapters.cursor import CursorAdapter


def write(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def test_claude_code_reads_the_model_from_the_assistant_message(tmp_path):
    t = tmp_path / "s.jsonl"
    write(t, [
        {"type": "user", "timestamp": "2026-11-07T20:00:00Z", "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "message": {"role": "assistant", "model": "claude-sonnet-5-5",
                                          "content": [{"type": "text", "text": "hello"}]}},
        {"type": "user", "timestamp": "2026-11-07T20:01:00Z", "message": {"role": "user", "content": "again"}},
        {"type": "assistant", "message": {"role": "assistant", "model": "<synthetic>",
                                          "content": [{"type": "text", "text": "ok"}]}},
    ])
    ex = ClaudeCodeAdapter().read_new(SessionRef("s", t, None), 0).exchanges
    assert [e.model for e in ex] == ["claude-sonnet-5-5", ""]  # placeholder names are not models


def test_codex_takes_the_model_from_the_turn_context_before_each_prompt(tmp_path):
    def msg(role, text):
        return {"type": "response_item", "timestamp": "2026-11-07T20:00:00Z",
                "payload": {"type": "message", "role": role,
                            "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}]}}

    t = tmp_path / "r.jsonl"
    write(t, [{"type": "turn_context", "payload": {"model": "gpt-a"}}, msg("user", "one"), msg("assistant", "1"),
              {"type": "turn_context", "payload": {"model": "gpt-b"}}, msg("user", "two"), msg("assistant", "2")])
    ex = CodexAdapter().read_new(SessionRef("s", t, None), 0).exchanges
    assert [e.model for e in ex] == ["gpt-a", "gpt-b"]


def test_cursor_records_the_model_from_hook_payloads(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "rd")
    a = CursorAdapter()
    base = {"conversation_id": "c1", "workspace_roots": [str(tmp_path)], "model": "claude-4.5-sonnet"}
    a.capture({**base, "hook_event_name": "beforeSubmitPrompt", "prompt": "hi"})
    a.capture({**base, "hook_event_name": "afterAgentResponse", "text": "hello"})
    a.capture({**base, "hook_event_name": "stop"})
    from raindrop.adapters import cursor
    spool = cursor._spool_path("c1")
    ex = a.read_new(SessionRef("c1", spool, str(tmp_path)), 0).exchanges
    assert [(e.user_text, e.model) for e in ex] == [("hi", "claude-4.5-sonnet")]


def test_a_missing_model_is_an_empty_string(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "rd")
    a = CursorAdapter()
    base = {"conversation_id": "c2", "workspace_roots": [str(tmp_path)]}
    a.capture({**base, "hook_event_name": "beforeSubmitPrompt", "prompt": "hi"})
    a.capture({**base, "hook_event_name": "afterAgentResponse", "text": "hello"})
    a.capture({**base, "hook_event_name": "stop"})
    from raindrop.adapters import cursor
    ex = a.read_new(SessionRef("c2", cursor._spool_path("c2"), str(tmp_path)), 0).exchanges
    assert [e.model for e in ex] == [""]


def test_kiro_takes_the_model_from_its_chat_log_when_the_hook_has_none(tmp_path, monkeypatch):
    from raindrop.adapters import kiro

    monkeypatch.setenv("HOME", str(tmp_path))  # spool and chat log both live under the home folder
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    chat = tmp_path / ".kiro" / "sessions" / "wshash" / "sess1"
    chat.mkdir(parents=True)
    write(chat / "messages.jsonl", [
        {"payload": {"type": "turn_start", "turnId": "t1", "modelId": "claude-sonnet-4.5"}},
        {"payload": {"type": "assistant", "operationType": "Say", "turnId": "t1", "content": "hello"}},
        {"payload": {"type": "turn_end", "turnId": "t1"}},
    ])
    a = kiro.KiroAdapter()
    a.capture({"hook_event_name": "UserPromptSubmit", "session_id": "sess1", "prompt": "hi", "cwd": str(tmp_path)})
    a.capture({"hook_event_name": "Stop", "session_id": "sess1", "cwd": str(tmp_path)})
    ex = a.read_new(SessionRef("sess1", None, str(tmp_path)), 0).exchanges
    assert [(e.assistant_text, e.model) for e in ex] == [("hello", "claude-sonnet-4.5")]
