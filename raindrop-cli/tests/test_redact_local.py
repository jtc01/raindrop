"""Local identifiers (absolute paths, OS username, hostname) never leave the machine.

Pure-function tests feed Mac, Linux and Windows identifiers on whatever OS runs them.
The sync tests check the layer is in the one redaction path both the real sync and
`sync --dry-run` use, and that the dry run reports the rewrites.
"""
from __future__ import annotations

import json

import httpx
import pytest

from raindrop import api_client, config, constants, sync_cmd
from raindrop.redact import LocalIdentifiers, redact, scrub_local_identifiers, scrub_obj

MAC = LocalIdentifiers(repo_root="/Users/matt/Development/raindrop", home="/Users/matt",
                       username="matt", hostname="Matts-MacBook-Pro.local")
LINUX = LocalIdentifiers(repo_root="/home/phil/Development/Hackathons/raindrop", home="/home/phil",
                         username="phil", hostname="ubuntu")
WIN = LocalIdentifiers(repo_root=r"C:\Users\Matteo\Documents\GitHub\raindrop", home=r"C:\Users\Matteo",
                       username="Matteo", hostname="DESKTOP-4F2K9", windows=True)


def scrub(text, ids):
    return scrub_local_identifiers(text, ids)


# ---------------------------------------------------------------------------
# Paths inside the repo become repo-relative
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ids, text, expected", [
    (MAC, "Edit /Users/matt/Development/raindrop/api/app.py", "Edit api/app.py"),
    (MAC, "Read ~/Development/raindrop/api/app.py", "Read api/app.py"),
    (MAC, "/Users/matt/Development/raindrop/api/app.py:12: error", "api/app.py:12: error"),
    (MAC, 'open "/Users/matt/Development/raindrop/api/app.py" (line 3)', 'open "api/app.py" (line 3)'),
    (LINUX, "/home/phil/Development/Hackathons/raindrop/raindrop-cli/tests/test_x.py",
     "raindrop-cli/tests/test_x.py"),
    (WIN, r"Edit C:\Users\Matteo\Documents\GitHub\raindrop\api\app.py", "Edit api/app.py"),
    (WIN, "C:/Users/Matteo/Documents/GitHub/raindrop/api/search.py", "api/search.py"),
    (WIN, r"c:\users\matteo\documents\github\raindrop\API\App.py", "API/App.py"),
    (WIN, r"\Users\Matteo\Documents\GitHub\raindrop\README.md", "README.md"),  # drive-less, same drive
    (WIN, r"~\Documents\GitHub\raindrop\api\app.py", "api/app.py"),
    (WIN, "cd /c/Users/Matteo/Documents/GitHub/raindrop/api && pytest", "cd api && pytest"),  # Git Bash
])
def test_repo_paths_become_relative(ids, text, expected):
    assert scrub(text, ids) == expected


def test_same_file_reads_the_same_from_every_os():
    assert (scrub("/Users/matt/Development/raindrop/raindrop-cli/raindrop/cli.py", MAC)
            == scrub("/home/phil/Development/Hackathons/raindrop/raindrop-cli/raindrop/cli.py", LINUX)
            == scrub(r"C:\Users\Matteo\Documents\GitHub\raindrop\raindrop-cli\raindrop\cli.py", WIN)
            == "raindrop-cli/raindrop/cli.py")


def test_bare_repo_root_is_a_placeholder():
    assert scrub("cd /Users/matt/Development/raindrop && pytest", MAC) == "cd <repo> && pytest"
    assert scrub("ls /Users/matt/Development/raindrop/", MAC) == "ls <repo>"
    assert scrub("C:/Users/Matteo/Documents/GitHub/raindrop", WIN) == "<repo>"


def test_prefix_match_is_not_containment():
    assert scrub("/Users/matt/Development/raindrop-old/x.py", MAC) == "~/Development/raindrop-old/x.py"
    assert scrub(r"D:\Other\raindrop\api\app.py", WIN) == r"D:\Other\raindrop\api\app.py"


# ---------------------------------------------------------------------------
# Other paths under the home directory become ~/...
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ids, text, expected", [
    (MAC, "/Users/matt/.claude/settings.json", "~/.claude/settings.json"),
    (MAC, "see /Users/matt and /Users/matt/", "see ~ and ~/"),
    (MAC, "[/Users/matt/x]", "[~/x]"),
    (LINUX, "/home/phil/.cursor/hooks.json", "~/.cursor/hooks.json"),
    (WIN, r"C:\Users\Matteo\.claude\settings.json", r"~\.claude\settings.json"),
    (WIN, r"c:/users/matteo/.codex/x", "~/.codex/x"),
    (WIN, r"\Users\Matteo\x", r"~\x"),
    (WIN, "/c/Users/Matteo/AppData/Local/Temp/x", "~/AppData/Local/Temp/x"),  # Git Bash
])
def test_home_paths(ids, text, expected):
    assert scrub(text, ids) == expected


def test_tool_internal_slug_loses_the_username():
    text = "/Users/matt/.claude/projects/-Users-matt-Development-raindrop/abc/tool-results/x.txt"
    assert scrub(text, MAC) == "~/.claude/projects/-Users-<user>-Development-raindrop/abc/tool-results/x.txt"


def test_other_users_and_lookalikes_are_left_alone():
    assert scrub("/Users/matthew/x /Volumes/matt/x", MAC) == "/Users/matthew/x /Volumes/matt/x"
    assert scrub("/opt/home/phil/x ./home/phil/x", LINUX) == "/opt/home/phil/x ./home/phil/x"


# ---------------------------------------------------------------------------
# Username: only in path or user@host contexts, never as a word in prose
# ---------------------------------------------------------------------------


def test_username_in_paths_written_differently():
    assert scrub("python3 /users/matt/x.py", MAC) == "python3 /users/<user>/x.py"  # case differs from home
    assert scrub(r"D:\Users\Matteo\y", WIN) == r"D:\Users\<user>\y"  # other drive
    assert scrub("/d/Users/Matteo/y", WIN) == "/d/Users/<user>/y"  # other drive, Git Bash
    assert scrub("/home/phil/x", LocalIdentifiers(username="phil")) == "/home/<user>/x"  # no home known


def test_username_before_at():
    assert scrub("matt@Matts-MacBook-Pro raindrop % ls", MAC) == "<user>@<host> raindrop % ls"
    assert scrub("ssh phil@10.0.0.5", LINUX) == "ssh <user>@10.0.0.5"
    assert scrub("phil@ubuntu:~$ make", LINUX) == "<user>@<host>:~$ make"


def test_username_as_a_word_is_kept():
    assert scrub("matt is a nice name and Matteo wrote this", MAC) == "matt is a nice name and Matteo wrote this"
    assert scrub("Matteo wrote this, format it", WIN) == "Matteo wrote this, format it"
    assert scrub("the mattress", MAC) == "the mattress"


# ---------------------------------------------------------------------------
# Hostname
# ---------------------------------------------------------------------------


def test_machine_like_hostnames_are_replaced_everywhere():
    assert scrub("host is Matts-MacBook-Pro.local or Matts-MacBook-Pro", MAC) == "host is <host> or <host>"
    assert scrub("on DESKTOP-4F2K9 now", WIN) == "on <host> now"
    assert scrub("desktop-4f2k9", WIN) == "<host>"  # hostnames are case-insensitive


def test_plain_word_hostnames_only_after_at():
    assert scrub("ubuntu is an os; phil@ubuntu is a prompt", LINUX) == "ubuntu is an os; <user>@<host> is a prompt"


def test_localhost_is_never_a_hostname():
    ids = LocalIdentifiers(hostname="localhost")
    assert scrub("curl http://localhost:8080 root@localhost", ids) == "curl http://localhost:8080 root@localhost"


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def test_missing_identifiers_disable_rules_and_empty_text_passes():
    assert scrub("/Users/matt/x", LocalIdentifiers()) == "/Users/matt/x"
    assert scrub("", MAC) == ""


def test_detect_never_raises_and_uses_this_machine(monkeypatch, tmp_path):
    monkeypatch.setenv("USER", "someone")
    monkeypatch.setattr("raindrop.redact.socket.gethostname", lambda: "box-1")
    ids = LocalIdentifiers.detect(repo_root=tmp_path / "proj")
    assert ids.repo_root == str(tmp_path / "proj")
    assert ids.username == "someone" and ids.hostname == "box-1"
    assert ids.home and ids.windows == (__import__("os").name == "nt")


def test_scrub_obj_covers_every_text_field_and_reports_rewrites():
    wire = {
        "id": "s:0", "session_id": "s", "idx": 0, "ts": 1,
        "user_text": "fix /Users/matt/Development/raindrop/api/app.py on Matts-MacBook-Pro",
        "assistant_text": "Edited /Users/matt/Development/raindrop/api/app.py; logs in /Users/matt/.claude/x",
        "tool_summaries": ["Edit /Users/matt/Development/raindrop/api/app.py", "Bash: pytest -q"],
        "files_touched": ["api/app.py"],
    }
    out, rewrites = scrub_obj(wire, MAC)
    assert out["user_text"] == "fix api/app.py on <host>"
    assert out["assistant_text"] == "Edited api/app.py; logs in ~/.claude/x"
    assert out["tool_summaries"] == ["Edit api/app.py", "Bash: pytest -q"]
    assert wire["user_text"].startswith("fix /Users")  # input not mutated
    assert "matt" not in json.dumps(out) and "MacBook" not in json.dumps(out)
    assert {(r.field, r.original, r.replacement) for r in rewrites} == {
        ("user_text", "/Users/matt/Development/raindrop/api/app.py", "api/app.py"),
        ("user_text", "Matts-MacBook-Pro", "<host>"),
        ("assistant_text", "/Users/matt/Development/raindrop/api/app.py", "api/app.py"),
        ("assistant_text", "/Users/matt", "~"),
        ("tool_summaries[0]", "/Users/matt/Development/raindrop/api/app.py", "api/app.py"),
    }


def test_placeholders_survive_secret_redaction():
    assert redact("user=<user> host=<host> path=~/.ssh/config") == "user=<user> host=<host> path=~/.ssh/config"


# ---------------------------------------------------------------------------
# Through sync and sync --dry-run (shared redaction path)
# ---------------------------------------------------------------------------

KEY = "rd_supersecretkey_1234567890abcdef"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("USER", "uzer")
    monkeypatch.setattr("raindrop.redact.socket.gethostname", lambda: "uzers-laptop.local")
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / ".raindrop")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / ".raindrop" / "config.json")
    config.save({"api_url": "https://api.test", "api_key": KEY, "agent_id": "a", "team_id": "t",
                 "display_name": "D", "focus": "", "tools": {}, "sharing": {"mode": "all"}})
    repo = tmp_path / "work"
    (repo / ".git").mkdir(parents=True)
    (repo / "src").mkdir()
    return repo


def transcript(repo, prompt, reply, edited):
    sid = "sess1"
    path = constants.claude_dir() / "projects" / "slug" / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "user", "message": {"content": prompt}, "timestamp": "2026-11-07T18:00:00Z", "cwd": str(repo)},
        {"type": "assistant", "cwd": str(repo), "message": {"content": [
            {"type": "tool_use", "name": "Edit", "input": {"file_path": edited}},
            {"type": "text", "text": reply},
        ]}},
    ]
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))
    return {"session_id": sid, "transcript_path": str(path), "cwd": str(repo)}


def test_real_sync_uploads_scrubbed_text(env, monkeypatch):
    repo = env
    edited = str(repo / "src" / "app.py")
    payload = transcript(repo, f"fix {edited} as uzer@uzers-laptop",
                         f"Edited {edited}, notes in {repo.parent}/.claude/n.txt", edited)
    sent = []

    def handler(req):
        body = json.loads(req.content)
        sent.append(body)
        return httpx.Response(200, json={"accepted": 1, "ids": [e["id"] for e in body["exchanges"]]})

    assert sync_cmd.run_sync(payload, "claude-code", httpx.MockTransport(handler)) == 1
    [ex] = sent[0]["exchanges"]
    assert ex["user_text"] == "fix src/app.py as <user>@<host>"
    assert ex["assistant_text"] == "Edited src/app.py, notes in ~/.claude/n.txt"
    assert ex["tool_summaries"] == ["Edit src/app.py"]
    assert ex["files_touched"] == ["src/app.py"]
    assert str(repo.parent) not in json.dumps(sent) and "uzer" not in json.dumps(sent).replace("<user>", "")


def test_dry_run_shows_the_same_text_and_lists_rewrites(env, monkeypatch):
    repo = env
    monkeypatch.setattr(api_client, "_post", lambda *a, **k: pytest.fail("dry run must not upload"))
    edited = str(repo / "src" / "app.py")
    payload = transcript(repo, f"fix {edited}", f"Edited {edited} twice: {edited}", edited)
    report = sync_cmd.run_dry_run(payload, "claude-code")
    [ex] = report.exchanges
    assert ex.wire["user_text"] == "fix src/app.py"
    assert ex.wire["tool_summaries"] == ["Edit src/app.py"]
    local = {(r.field, r.original, r.marker, r.count) for r in ex.redactions if r.kind == "local"}
    assert local == {
        ("user_text", edited, "src/app.py", 1),
        ("assistant_text", edited, "src/app.py", 2),
        ("tool_summaries[0]", edited, "src/app.py", 1),
    }
    text = sync_cmd.format_dry_run(report)
    assert f"- assistant_text: {edited} -> src/app.py (x2)" in text
    assert "Prompt: fix src/app.py" in text


def test_dry_run_still_locates_secrets_after_path_rewrites(env, monkeypatch):
    repo = env
    monkeypatch.setattr(api_client, "_post", lambda *a, **k: pytest.fail("dry run must not upload"))
    leak = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"
    payload = transcript(repo, f"in {repo}/src/app.py use {leak}", "ok", str(repo / "src" / "app.py"))
    [ex] = sync_cmd.run_dry_run(payload, "claude-code").exchanges
    assert ex.wire["user_text"] == "in src/app.py use [REDACTED:anthropic-key]"
    [secret] = [r for r in ex.redactions if r.kind == "secret"]
    # Offsets refer to the text after path rewriting, i.e. the text the marker sits in.
    assert secret.original == leak and ex.wire["user_text"][: secret.start] == "in src/app.py use "
