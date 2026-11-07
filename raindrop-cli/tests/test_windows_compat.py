"""Windows behaviour, simulated: we can't run on Windows in CI here, so these
force the Windows code paths on any OS."""
import json

import pytest

from raindrop import constants
from raindrop.adapters import _common, claude, kiro
from raindrop.adapters._common import display_cmd, is_our_hook_command, quote_cmd

WIN_EXE = r"C:\Users\Jo Smith\venv\Scripts\raindrop.EXE"


def test_quote_cmd():
    assert quote_cmd(WIN_EXE, windows=True) == '"C:/Users/Jo Smith/venv/Scripts/raindrop.EXE"'
    assert quote_cmd(r"C:\py\Scripts\raindrop.exe", windows=True) == "C:/py/Scripts/raindrop.exe"
    assert quote_cmd("/opt/my dir/raindrop", windows=False) == "'/opt/my dir/raindrop'"
    assert quote_cmd("/usr/bin/raindrop", windows=False) == "/usr/bin/raindrop"


def test_display_cmd():
    assert display_cmd(["claude", "mcp", "add", "x y"], windows=True) == 'claude mcp add "x y"'
    assert display_cmd(["claude", "mcp", "add", "x y"], windows=False) == "claude mcp add 'x y'"


@pytest.mark.parametrize("cmd", [
    "raindrop sync --tool claude-code",
    "/usr/local/bin/raindrop sync --tool kiro",
    "'/opt/my dir/raindrop' sync",
    '"C:/Users/Jo Smith/venv/Scripts/raindrop.EXE" sync --tool claude-code',
    "C:/py/Scripts/raindrop.exe sync",
])
def test_recognises_our_hook(cmd):
    assert is_our_hook_command(cmd)


@pytest.mark.parametrize("cmd", ["other-tool sync", "raindrop proxy", "echo raindrop-sync", "my-raindrop syncer"])
def test_ignores_other_hooks(cmd):
    assert not is_our_hook_command(cmd)


@pytest.fixture
def win(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(_common, "is_windows", lambda: True)
    monkeypatch.setattr(claude, "exe", lambda: WIN_EXE)
    monkeypatch.setattr(kiro, "exe", lambda: WIN_EXE)
    return tmp_path


def test_claude_hook_with_windows_exe_is_idempotent_and_removable(win):
    a = claude.ClaudeCodeAdapter()
    for _ in range(2):
        status = a.apply(a.plan_install(rules=False))
    stop = json.loads(constants.claude_settings_path().read_text())["hooks"]["Stop"]
    assert len(stop) == 1  # re-install replaced it; `raindrop.EXE sync` is still recognised
    assert stop[0]["hooks"][0]["command"] == \
        '"C:/Users/Jo Smith/venv/Scripts/raindrop.EXE" sync --tool claude-code'
    a.uninstall(status.to_tool_record())
    assert "hooks" not in json.loads(constants.claude_settings_path().read_text())


def test_non_ascii_settings_survive_roundtrip(win):
    path = constants.claude_settings_path()
    path.parent.mkdir()
    original = {"note": "naïve 日本語 ✓", "model": "x"}
    path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    a = claude.ClaudeCodeAdapter()
    status = a.apply(a.plan_install(rules=False))
    assert json.loads(path.read_text(encoding="utf-8"))["note"] == "naïve 日本語 ✓"
    assert "日本語" in path.read_text(encoding="utf-8")  # not rewritten as \u escapes
    a.uninstall(status.to_tool_record())
    assert json.loads(path.read_text(encoding="utf-8")) == original


def test_kiro_hook_command_quoted_for_windows(win):
    hooks = json.loads(kiro._hooks_text())["hooks"]
    for h in hooks:
        assert h["action"]["command"] == \
            '"C:/Users/Jo Smith/venv/Scripts/raindrop.EXE" sync --tool kiro --fail-open'
        assert is_our_hook_command(h["action"]["command"])


def test_session_ids_never_put_colons_in_filenames(win):
    sid = kiro.KiroAdapter().session_from_hook({"session_id": "a:b:c"}).session_id
    assert ":" not in sid and ":" not in kiro.spool_path(sid).name
    from raindrop import sync_cmd
    assert ":" not in sync_cmd._cursor_path("kiro", "a:b:c").name
