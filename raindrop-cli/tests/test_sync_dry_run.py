"""`raindrop sync --dry-run`: preview what a sync would send, with no side effects."""
import json
import os
from hashlib import sha1

import httpx
import pytest
from typer.testing import CliRunner

from raindrop import api_client, cli, config, constants, sync_cmd
from raindrop.adapters import kiro
from raindrop.adapters.registry import get_adapter

runner = CliRunner()
KEY = "rd_supersecretkey_1234567890abcdef"
LEAK = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"
ENV_SECRET = "envsecretvalue_0987654321"
REAL_POST = api_client._post


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / ".raindrop")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / ".raindrop" / "config.json")

    def no_network(*a, **kw):
        raise AssertionError("dry run must not upload")

    monkeypatch.setattr(api_client, "_post", no_network)
    repo = tmp_path / "work"
    (repo / ".git").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / ".env").write_text(f"API_TOKEN={ENV_SECRET}\n")
    monkeypatch.chdir(repo / "src")  # a subdirectory still counts as "in the repo"
    save_cfg({"mode": "all"})
    return repo


def save_cfg(sharing):
    config.save({"api_url": "https://api.test", "api_key": KEY, "agent_id": "a",
                 "team_id": "t", "display_name": "D", "focus": "", "tools": {},
                 "sharing": sharing})


def turn(prompt, reply, files=(), ts="2026-11-07T18:00:00Z"):
    tools = [{"type": "tool_use", "name": "Edit", "input": {"file_path": f}} for f in files]
    return [
        {"type": "user", "message": {"content": prompt}, "timestamp": ts},
        {"type": "assistant", "message": {"content": [*tools, {"type": "text", "text": reply}]}},
    ]


def claude_transcript(sid, cwd, entries, mtime=None):
    path = constants.claude_dir() / "projects" / "slug" / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    first = {"type": "mode", "sessionId": sid}  # bookkeeping lines come before cwd
    lines = [first] + [{**e, "cwd": str(cwd)} for e in entries]
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def no_side_effects(tool, sid):
    cursor = sync_cmd._cursor_path(tool, sid)
    assert not cursor.exists()
    assert not sync_cmd._state_path(cursor).exists()
    assert not (config.CONFIG_DIR / "sync.log").exists()


# ---- span alignment ----------------------------------------------------------


def test_redacted_spans_finds_each_replaced_span():
    original = f"use {LEAK} and password: hunter2222 ok"
    spans = sync_cmd.redacted_spans(original, original.replace(LEAK, "[REDACTED:anthropic-key]")
                                    .replace("hunter2222", "[REDACTED]"), "user_text")
    assert [(s.marker, s.original) for s in spans] == [
        ("[REDACTED:anthropic-key]", LEAK), ("[REDACTED]", "hunter2222")]
    assert original[spans[0].start:spans[0].end] == LEAK
    assert all(s.field == "user_text" for s in spans)


def test_redacted_spans_ignores_markers_already_in_text():
    original = f"literal [REDACTED] then {LEAK}"
    redacted = original.replace(LEAK, "[REDACTED:anthropic-key]")
    spans = sync_cmd.redacted_spans(original, redacted)
    assert [s.original for s in spans] == [LEAK]
    assert sync_cmd.redacted_spans("same", "same") == []


# ---- finding the latest session --------------------------------------------


def test_claude_latest_session_picks_newest_in_this_repo(env, tmp_path):
    other = tmp_path / "other"
    (other / ".git").mkdir(parents=True)
    claude_transcript("old", env, turn("a", "b"), mtime=1000)
    claude_transcript("new", env / "src", turn("a", "b"), mtime=2000)
    claude_transcript("elsewhere", other, turn("a", "b"), mtime=3000)
    ref = get_adapter("claude-code").latest_session(config.repo_root())
    assert ref.session_id == "new" and ref.transcript_path.name == "new.jsonl"
    assert get_adapter("claude-code").latest_session(config.repo_root(tmp_path / "nowhere")) is None


def test_codex_latest_session_reads_session_meta(env):
    sid = "019a1b2c-3d4e-7f80-9a1b-2c3d4e5f6a7b"
    path = env.parent / ".codex" / "sessions" / "2026" / "11" / "07" / f"rollout-2026-11-07T18-00-00-{sid}.jsonl"
    path.parent.mkdir(parents=True)
    recs = [
        {"type": "session_meta", "payload": {"id": sid, "cwd": str(env)}},
        {"timestamp": "2026-11-07T18:00:00Z", "type": "response_item",
         "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}},
        {"timestamp": "2026-11-07T18:00:01Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "yo"}]}},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in recs))
    report = sync_cmd.run_dry_run(None, "codex")
    assert report.session.session_id == sid and report.session.transcript_path == path
    assert [e.wire["user_text"] for e in report.exchanges] == ["hi"]
    no_side_effects("codex", sid)


def test_cursor_latest_session_reads_spool(env):
    spool = config.CONFIG_DIR / "spools" / "cursor" / "conv-1.jsonl"
    spool.parent.mkdir(parents=True)
    spool.write_text("".join(json.dumps(r) + "\n" for r in [
        {"event": "beforeSubmitPrompt", "ts": 1, "text": "hi", "cwd": str(env)},
        {"event": "afterAgentResponse", "ts": 2, "text": "yo"},
    ]))
    report = sync_cmd.run_dry_run(None, "cursor")
    assert report.session.session_id == "conv-1"
    assert [e.wire["assistant_text"] for e in report.exchanges] == ["yo"]


def test_kiro_spool_records_cwd_and_old_hash_spools_still_match(env):
    a = get_adapter("kiro")
    a.capture({"hook_event_name": "UserPromptSubmit", "session_id": "k1", "cwd": str(env), "prompt": "hi"})
    assert json.loads(kiro.spool_path("k1").read_text())["cwd"] == str(env)
    assert a.latest_session(config.repo_root()).session_id == "k1"

    # spool from before records carried cwd, named by the per-directory fallback hash
    legacy = "kiro-" + sha1(str(env).encode()).hexdigest()[:12]
    kiro.spool_path(legacy).write_text(json.dumps({"event": "prompt", "ts": 1, "text": "x"}) + "\n")
    os.utime(kiro.spool_path("k1"), (1000, 1000))
    assert a.latest_session(config.repo_root()).session_id == legacy


def test_tool_without_lookup_explains_how_to_use_payload(env):
    result = runner.invoke(cli.app, ["sync", "--tool", "copilot", "--dry-run"], input="")
    assert result.exit_code == 1
    assert "pipe a hook payload" in result.output


def test_no_session_in_repo_is_an_error(env):
    result = runner.invoke(cli.app, ["sync", "--dry-run"], input="")
    assert result.exit_code == 1
    assert "No Claude Code session found" in result.output


# ---- the preview itself -----------------------------------------------------


def test_dry_run_prints_redacted_preview_without_side_effects(env):
    path = claude_transcript("s1", env, turn(
        f"deploy with {LEAK}", f"done, used {ENV_SECRET} and {KEY}", files=["src/app.py", "README.md"]))
    before = path.read_bytes()
    result = runner.invoke(cli.app, ["sync", "--dry-run"], input="")
    assert result.exit_code == 0, result.output
    out = result.output
    assert "latest session" in out and "Session:    s1" in out
    assert "A real sync would upload 1 exchange:" in out
    assert "Prompt: deploy with [REDACTED:anthropic-key]" in out
    assert "Reply:  done, used [REDACTED] and [REDACTED]" in out
    assert "Files:  src/app.py, README.md" in out
    assert "user_text chars 12-" in out and "-> [REDACTED:anthropic-key]" in out
    assert out.count("- assistant_text") == 2  # the .env value and the raindrop key
    for secret in (LEAK, ENV_SECRET, KEY):
        assert secret not in out
    assert "nothing was uploaded" in out
    assert path.read_bytes() == before
    no_side_effects("claude-code", "s1")


def test_dry_run_matches_what_a_real_sync_would_send(env, monkeypatch):
    path = claude_transcript("s1", env, turn(f"use {LEAK}", f"ok {ENV_SECRET} {KEY}", files=["a.py"]))
    preview = [e.wire for e in sync_cmd.run_dry_run(None, "claude-code").exchanges]

    sent = []

    def handler(req):
        sent.extend(json.loads(req.content)["exchanges"])
        return httpx.Response(200, json={"accepted": len(sent), "ids": [e["id"] for e in sent]})

    monkeypatch.setattr(api_client, "_post", REAL_POST)
    payload = {"session_id": "s1", "transcript_path": str(path), "cwd": str(env)}
    assert sync_cmd.run_sync(payload, "claude-code", httpx.MockTransport(handler)) == 1
    assert sent == preview


def test_dry_run_only_shows_exchanges_after_the_cursor(env):
    path = claude_transcript("s1", env, turn("first", "one") + turn("second", "two"))
    cursor = sync_cmd._cursor_path("claude-code", "s1")
    report = sync_cmd.run_dry_run(None, "claude-code")
    assert [e.wire["user_text"] for e in report.exchanges] == ["first", "second"]
    # put the cursor right after the first exchange, as a real sync would have
    first_len = len(b"".join(path.read_bytes().splitlines(keepends=True)[:3]))
    sync_cmd._save_cursor(cursor, first_len)
    report = sync_cmd.run_dry_run(None, "claude-code")
    assert [e.wire["user_text"] for e in report.exchanges] == ["second"]
    assert report.cursor == first_len and report.new_cursor == len(path.read_bytes())
    assert int(cursor.read_text()) == first_len  # unchanged


def test_unshared_repo_says_so_and_lists_what_would_be_skipped(env):
    save_cfg({"mode": "opt_in"})
    claude_transcript("s1", env, turn("private prompt", "private reply"))
    result = runner.invoke(cli.app, ["sync", "--dry-run"], input="")
    assert result.exit_code == 0, result.output
    assert "This repo isn't shared" in result.output and "raindrop share --repo" in result.output
    assert "would skip this exchange for good" in result.output
    assert "Prompt: private prompt" in result.output
    no_side_effects("claude-code", "s1")


def test_paused_repo_gives_pause_reason(env):
    save_cfg({"mode": "all", "paused_repos": [str(env)]})
    claude_transcript("s1", env, turn("p", "r"))
    report = sync_cmd.run_dry_run(None, "claude-code")
    assert not report.shared and "paused for this repo" in report.skip_reason


def test_previously_abandoned_exchanges_are_flagged(env):
    claude_transcript("s1", env, turn("bad", "x") + turn("good", "y"))
    cursor = sync_cmd._cursor_path("claude-code", "s1")
    sync_cmd._save_state(cursor, {}, {"s1:0"})
    out = sync_cmd.format_dry_run(sync_cmd.run_dry_run(None, "claude-code"))
    assert "would upload 1 exchange:" in out
    assert "[s1:0]" in out and "previously abandoned" in out


def test_hook_payload_on_stdin_is_used_and_not_captured(env, tmp_path):
    claude_transcript("newest", env, turn("not me", "x"), mtime=2000)
    path = claude_transcript("chosen", env, turn("me", "y"), mtime=1000)
    payload = {"session_id": "chosen", "transcript_path": str(path), "cwd": str(env)}
    result = runner.invoke(cli.app, ["sync", "--dry-run"], input=json.dumps(payload))
    assert result.exit_code == 0, result.output
    assert "hook payload" in result.output and "Prompt: me" in result.output

    # event-capture tools: the dry run must not spool the event it was given
    kiro_payload = {"hook_event_name": "UserPromptSubmit", "session_id": "k9", "cwd": str(env), "prompt": "hi"}
    result = runner.invoke(cli.app, ["sync", "--tool", "kiro", "--dry-run"], input=json.dumps(kiro_payload))
    assert result.exit_code == 0, result.output
    assert not kiro.spool_path("k9").exists()


def test_not_joined(env):
    config.CONFIG_PATH.unlink()
    result = runner.invoke(cli.app, ["sync", "--dry-run"], input="")
    assert result.exit_code == 1 and "Not joined" in result.output
