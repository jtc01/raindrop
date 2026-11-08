"""files_touched normalization: every adapter uploads paths relative to the
session's git repo root, with `/` separators, and nothing from outside the repo.

The pure function is exercised with Mac, Linux and Windows paths on whatever OS
runs the tests, by forcing the path flavour. The session wrapper is checked
against a real temp repo.
"""
from __future__ import annotations

import json

import pytest

from raindrop.adapters._common import normalize_files_touched, repo_relative_files
from raindrop.adapters.base import SessionRef
from raindrop.adapters.claude import ClaudeCodeAdapter
from raindrop.adapters.codex import CodexAdapter
from raindrop.adapters.cursor import CursorAdapter

MAC = dict(repo_root="/Users/matt/Development/raindrop", home="/Users/matt", windows=False)
LINUX = dict(repo_root="/home/phil/Development/Hackathons/raindrop", home="/home/phil", windows=False)
WIN = dict(repo_root=r"C:\Users\Matteo\Documents\GitHub\raindrop", home=r"C:\Users\Matteo", windows=True)


# ---------------------------------------------------------------------------
# Absolute paths inside the repo become repo-relative POSIX paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("env, raw, expected", [
    (MAC, "/Users/matt/Development/raindrop/api/app.py", "api/app.py"),
    (MAC, "/Users/matt/Development/raindrop/raindrop-cli/raindrop/sync_cmd.py", "raindrop-cli/raindrop/sync_cmd.py"),
    (LINUX, "/home/phil/Development/Hackathons/raindrop/raindrop-cli/tests/test_sync_dry_run.py",
     "raindrop-cli/tests/test_sync_dry_run.py"),
    (WIN, r"C:\Users\Matteo\Documents\GitHub\raindrop\api\app.py", "api/app.py"),
    # Forward slashes on Windows (Git Bash, Claude Code) are fine too.
    (WIN, "C:/Users/Matteo/Documents/GitHub/raindrop/api/app.py", "api/app.py"),
    # Windows paths are case-insensitive, including the drive letter.
    (WIN, r"c:\users\matteo\documents\github\raindrop\API\App.py", "API/App.py"),
    # Rooted but drive-less: lives on the repo's drive.
    (WIN, r"\Users\Matteo\Documents\GitHub\raindrop\README.md", "README.md"),
])
def test_absolute_in_repo(env, raw, expected):
    assert normalize_files_touched([raw], **env) == [expected]


def test_same_file_gives_same_answer_on_every_os():
    """The whole point: a Mac and a Windows teammate editing the same file
    produce identical files_touched."""
    mac = normalize_files_touched(["/Users/matt/Development/raindrop/raindrop-cli/raindrop/cli.py"], **MAC)
    linux = normalize_files_touched(["/home/phil/Development/Hackathons/raindrop/raindrop-cli/raindrop/cli.py"], **LINUX)
    win = normalize_files_touched([r"C:\Users\Matteo\Documents\GitHub\raindrop\raindrop-cli\raindrop\cli.py"], **WIN)
    assert mac == linux == win == ["raindrop-cli/raindrop/cli.py"]


# ---------------------------------------------------------------------------
# Relative paths: resolved against the cwd, which may be a repo subdirectory
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("env, cwd, raw, expected", [
    (MAC, None, "api/app.py", "api/app.py"),
    (MAC, "/Users/matt/Development/raindrop/raindrop-cli", "raindrop/cli.py", "raindrop-cli/raindrop/cli.py"),
    (MAC, "/Users/matt/Development/raindrop/raindrop-cli", "./raindrop/cli.py", "raindrop-cli/raindrop/cli.py"),
    (MAC, "/Users/matt/Development/raindrop/raindrop-cli", "../api/app.py", "api/app.py"),
    (LINUX, "/home/phil/Development/Hackathons/raindrop/api", "search.py", "api/search.py"),
    (WIN, r"C:\Users\Matteo\Documents\GitHub\raindrop\raindrop-cli", r"raindrop\cli.py", "raindrop-cli/raindrop/cli.py"),
    (WIN, r"C:\Users\Matteo\Documents\GitHub\raindrop\raindrop-cli", "tests/test_redact.py", "raindrop-cli/tests/test_redact.py"),
    (WIN, r"C:\Users\Matteo\Documents\GitHub\raindrop\raindrop-cli", r"..\api\app.py", "api/app.py"),
])
def test_relative_paths(env, cwd, raw, expected):
    assert normalize_files_touched([raw], cwd=cwd, **env) == [expected]


# ---------------------------------------------------------------------------
# Outside the repo: dropped
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("env, raw", [
    # Tool internals under the home directory.
    (MAC, "/Users/matt/.claude/projects/-Users-matt-Development-raindrop/abc/tool-results/b8ht82t63.txt"),
    (MAC, "~/.claude/settings.json"),
    (MAC, "/Users/matt/.codex/sessions/rollout.jsonl"),
    (LINUX, "/home/phil/.claude/projects/-home-phil-Development-Hackathons-raindrop/06407b36/tool-results/x.txt"),
    (LINUX, "~/.cursor/hooks.json"),
    (WIN, r"C:\Users\Matteo\.claude\settings.json"),
    (WIN, r"~\.claude\settings.json"),
    (WIN, "~/.gemini/config/rules/raindrop.md"),
    # Sibling repos and system files.
    (MAC, "/Users/matt/Development/other-repo/main.py"),
    (MAC, "/etc/hosts"),
    (LINUX, "/tmp/scratch.txt"),
    (WIN, r"D:\Other\raindrop\api\app.py"),  # other drive, same-looking tree
    (WIN, r"\\server\share\raindrop\api\app.py"),
    # A prefix match is not a containment match.
    (MAC, "/Users/matt/Development/raindrop-old/api/app.py"),
    (WIN, r"C:\Users\Matteo\Documents\GitHub\raindrop2\api\app.py"),
    # `..` that escapes the repo, written relative or absolute.
    (MAC, "../other-repo/x.py"),
    (MAC, "/Users/matt/Development/raindrop/../secrets.env"),
    (WIN, r"C:\Users\Matteo\Documents\GitHub\raindrop\..\secrets.env"),
])
def test_outside_repo_is_dropped(env, raw):
    assert normalize_files_touched([raw], **env) == []


def test_home_expansion_can_land_inside_the_repo():
    assert normalize_files_touched(["~/Development/raindrop/api/app.py"], **MAC) == ["api/app.py"]
    assert normalize_files_touched([r"~\Documents\GitHub\raindrop\api\app.py"], **WIN) == ["api/app.py"]


# ---------------------------------------------------------------------------
# Hygiene: order, dedupe, junk
# ---------------------------------------------------------------------------


def test_dedupes_after_normalizing_and_keeps_first_seen_order():
    raw = [
        r"C:\Users\Matteo\Documents\GitHub\raindrop\api\app.py",
        "C:/Users/Matteo/Documents/GitHub/raindrop/api/search.py",
        "api/app.py",  # same as the first, written relative
        r"C:\Users\Matteo\.claude\settings.json",  # dropped
        r"c:\USERS\matteo\documents\github\raindrop\api\App.py",  # same file, Windows casing
    ]
    assert normalize_files_touched(raw, **WIN) == ["api/app.py", "api/search.py", "api/App.py"]


def test_ignores_non_strings_blanks_and_the_root_itself():
    raw = [None, 42, "", "   ", {"path": "x"}, "/Users/matt/Development/raindrop", "/Users/matt/Development/raindrop/", "."]
    assert normalize_files_touched(raw, **MAC) == []


def test_repo_root_with_trailing_slash_or_mixed_separators():
    assert normalize_files_touched(["/Users/matt/Development/raindrop/api/app.py"],
                                   repo_root="/Users/matt/Development/raindrop/", windows=False, home="/Users/matt") == ["api/app.py"]
    assert normalize_files_touched([r"C:\Users\Matteo\Documents\GitHub\raindrop\api\app.py"],
                                   repo_root="C:/Users/Matteo/Documents/GitHub/raindrop/", windows=True, home=r"C:\Users\Matteo") == ["api/app.py"]


def test_defaults_to_the_current_os(monkeypatch):
    monkeypatch.setattr("raindrop.adapters._common.is_windows", lambda: True)
    assert normalize_files_touched([r"C:\r\a\b.py"], repo_root=r"C:\r", home=r"C:\Users\x") == ["a/b.py"]
    monkeypatch.setattr("raindrop.adapters._common.is_windows", lambda: False)
    assert normalize_files_touched(["/r/a/b.py"], repo_root="/r", home="/home/x") == ["a/b.py"]


# ---------------------------------------------------------------------------
# Session wrapper: finds the git root above the session cwd
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path):
    root = (tmp_path / "proj").resolve()
    (root / ".git").mkdir(parents=True)
    (root / "raindrop-cli" / "raindrop").mkdir(parents=True)
    (root / "api").mkdir()
    return root


def test_repo_relative_files_uses_git_root_above_cwd(repo, tmp_path):
    cwd = repo / "raindrop-cli"  # hook fired from a subdirectory
    session = SessionRef("s", None, str(cwd))
    paths = [
        str(repo / "api" / "app.py"),
        "raindrop/cli.py",  # relative to cwd
        str(tmp_path / "elsewhere.txt"),  # outside
        str(repo / "api" / "app.py"),  # dup
    ]
    assert repo_relative_files(paths, session) == ["api/app.py", "raindrop-cli/raindrop/cli.py"]


def test_repo_relative_files_without_git_treats_cwd_as_root(tmp_path):
    if any((p / ".git").exists() for p in (tmp_path.resolve(), *tmp_path.resolve().parents)):
        pytest.skip("a parent of the temp dir is a git repo (for example a home folder under git)")
    cwd = (tmp_path / "loose").resolve()
    cwd.mkdir()
    session = SessionRef("s", None, str(cwd))
    assert repo_relative_files([str(cwd / "a.py"), str(tmp_path / "b.py")], session) == ["a.py"]


# ---------------------------------------------------------------------------
# Through the adapters
# ---------------------------------------------------------------------------


def _jsonl(path, records):
    path.write_bytes("".join(json.dumps(r) + "\n" for r in records).encode())


def test_claude_adapter_normalizes_and_drops_internals(repo):
    transcript = repo / "s.jsonl"
    _jsonl(transcript, [
        {"type": "user", "timestamp": "2026-01-01T00:00:00Z", "message": {"role": "user", "content": "go"}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Edit", "input": {"file_path": str(repo / "api" / "app.py")}},
            {"type": "tool_use", "name": "Read", "input": {"file_path": str(repo.parent / ".claude" / "settings.json")}},
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "raindrop/cli.py"}},
            {"type": "text", "text": "done"},
        ]}},
    ])
    ref = SessionRef("abc", transcript, str(repo / "raindrop-cli"))
    [ex] = ClaudeCodeAdapter().read_new(ref, 0).exchanges
    assert ex.files_touched == ["api/app.py", "raindrop-cli/raindrop/cli.py"]
    # Summaries are untouched: they are what the agent saw, not a path list.
    assert ex.tool_summaries[0] == f"Edit {repo / 'api' / 'app.py'}"


def test_codex_adapter_normalizes(repo):
    rollout = repo / "rollout.jsonl"
    _jsonl(rollout, [
        {"timestamp": "2026-11-07T18:00:00Z", "type": "response_item",
         "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "fix"}]}},
        {"timestamp": "2026-11-07T18:00:01Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "apply_patch",
                     "arguments": json.dumps({"file_path": str(repo / "api" / "search.py")})}},
        {"timestamp": "2026-11-07T18:00:01Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "apply_patch",
                     "arguments": json.dumps({"path": str(repo.parent / "outside.py")})}},
        {"timestamp": "2026-11-07T18:00:02Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant", "phase": "final",
                     "content": [{"type": "output_text", "text": "ok"}]}},
    ])
    [ex] = CodexAdapter().read_new(SessionRef("c1", rollout, str(repo / "api")), 0).exchanges
    assert ex.files_touched == ["api/search.py"]


def test_cursor_adapter_normalizes(repo, monkeypatch):
    spool = repo.parent / "cursor.jsonl"
    monkeypatch.setattr("raindrop.adapters.cursor._spool_path", lambda _sid: spool)
    adapter = CursorAdapter()
    base = {"conversation_id": "conv", "cwd": str(repo / "raindrop-cli")}
    adapter.capture({**base, "hook_event_name": "beforeSubmitPrompt", "prompt": "fix"})
    adapter.capture({**base, "hook_event_name": "postToolUse", "tool_name": "edit_file",
                     "tool_input": {"file_path": str(repo / "raindrop-cli" / "raindrop" / "cli.py")}})
    adapter.capture({**base, "hook_event_name": "postToolUse", "tool_name": "read_file",
                     "tool_input": {"path": str(repo.parent / ".cursor" / "x.json")}})
    adapter.capture({**base, "hook_event_name": "afterAgentResponse", "text": "done"})
    [ex] = adapter.read_new(SessionRef("conv", None, str(repo / "raindrop-cli")), 0).exchanges
    assert ex.files_touched == ["raindrop-cli/raindrop/cli.py"]
