"""The tool picker shown after `raindrop join` / `raindrop create-team`."""
import json

import pytest
import typer
from typer.testing import CliRunner

from raindrop import api_client, cli, config, tools_cmd
from raindrop.adapters import registry
from raindrop.errors import InstallError

runner = CliRunner()
RESP = {"team_id": "t1", "team_name": "Demo", "agent_id": "agt_1", "api_key": "rd_key_1234567890abcdef",
        "join_code": "K7QX-M9TF"}


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A machine with Claude Code and Kiro installed (and nothing else), not yet joined."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    monkeypatch.delenv("COPILOT_HOME", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / ".raindrop")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / ".raindrop" / "config.json")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".kiro").mkdir()
    calls = {"join": 0, "create": 0}

    def join(*a, **k):
        calls["join"] += 1
        return dict(RESP)

    def create(*a, **k):
        calls["create"] += 1
        return dict(RESP)

    monkeypatch.setattr(api_client, "join", join)
    monkeypatch.setattr(api_client, "create_team", create)
    monkeypatch.setattr(tools_cmd, "_interactive", lambda: False)  # tests are not a terminal unless they say so
    return calls


def do_join(*extra, input=None):
    return runner.invoke(cli.app, ["join", "K7QX-M9TF", "--name", "Matteo", "--api-url", "https://api.test", *extra],
                         input=input)


def installed():
    cfg = config.load() or {}
    return sorted(cfg.get("tools", {}))


# ---- scripts and pipes never get a prompt ----------------------------------------------------

def test_without_a_terminal_it_only_prints_a_hint_naming_the_tools_it_found(machine):
    r = do_join()
    assert r.exit_code == 0, r.output
    assert "Joined team 'Demo'" in r.output
    assert "raindrop tools add <tool>" in r.output and "claude-code, kiro" in r.output
    assert installed() == []  # nothing is changed on the machine without being asked


# ---- --tools ------------------------------------------------------------------------------------

def test_tools_option_installs_exactly_the_named_tool(machine, tmp_path):
    r = do_join("--tools", "claude-code")
    assert r.exit_code == 0, r.output
    assert installed() == ["claude-code"] and "Claude Code [ok]" in r.output and "Restart those tools" in r.output
    assert "raindrop sync --tool claude-code" in (tmp_path / ".claude" / "settings.json").read_text()
    assert not (tmp_path / ".kiro" / "hooks").exists()  # Kiro was not touched


def test_tools_all_means_every_tool_found_here_and_no_others(machine, tmp_path):
    r = do_join("--tools", "all")
    assert r.exit_code == 0, r.output
    assert installed() == ["claude-code", "kiro"]  # copilot, cursor, codex, antigravity are not installed here
    assert not (tmp_path / ".cursor").exists() and not (tmp_path / ".codex").exists()


def test_tools_none_skips_without_asking(machine):
    r = do_join("--tools", "none")
    assert r.exit_code == 0 and "Skipped tool setup" in r.output and installed() == []


def test_a_typo_in_tools_fails_before_anything_is_joined(machine):
    r = do_join("--tools", "claudecode")
    assert r.exit_code == 2
    assert "'claudecode' is not a supported tool" in r.output and "claude-code" in r.output
    assert machine["join"] == 0 and not config.CONFIG_PATH.exists()  # no API call, no config written


def test_a_tool_named_but_not_installed_here_is_reported_and_the_join_still_stands(machine):
    r = do_join("--tools", "cursor,kiro")
    assert r.exit_code == 0, r.output
    assert "Could not set up cursor: Cursor doesn't appear to be installed." in r.output
    assert installed() == ["kiro"] and config.CONFIG_PATH.exists()  # the one that could be installed was


def test_one_tool_failing_does_not_stop_the_next(machine, monkeypatch):
    real = registry.get_adapter

    class Failing:
        name, display_name = "claude-code", "Claude Code"
        detect = lambda self: True
        plan_install = lambda self, rules: []
        apply = lambda self, changes: (_ for _ in ()).throw(InstallError("mcp", "disk full"))

    monkeypatch.setattr(registry, "get_adapter", lambda n: Failing() if n == "claude-code" else real(n))
    r = do_join("--tools", "claude-code,kiro")
    assert r.exit_code == 0, r.output
    assert "Could not set up claude-code" in r.output and installed() == ["kiro"]


# ---- the interactive prompt -----------------------------------------------------------------

@pytest.fixture
def terminal(machine, monkeypatch):
    monkeypatch.setattr(tools_cmd, "_interactive", lambda: True)
    return machine


def test_the_prompt_lists_the_tools_and_installs_the_numbers_you_type(terminal):
    r = do_join(input="2\n")
    assert r.exit_code == 0, r.output
    assert "1) Claude Code" in r.output and "2) Kiro CLI" in r.output
    assert installed() == ["kiro"]


def test_numbers_names_all_and_mixed_answers(terminal):
    r = do_join(input="1, kiro\n")
    assert installed() == ["claude-code", "kiro"], r.output


def test_all_at_the_prompt(terminal):
    do_join(input="all\n")
    assert installed() == ["claude-code", "kiro"]


def test_enter_alone_skips_and_installs_nothing(terminal):
    r = do_join(input="\n")
    assert r.exit_code == 0 and "Skipped" in r.output and installed() == []


def test_a_bad_answer_asks_again_then_accepts_a_good_one(terminal):
    r = do_join(input="9\nfoo\n1\n")
    assert "'9' is not on the list (choose 1-2)" in r.output and "'foo' is not a supported tool" in r.output
    assert installed() == ["claude-code"]


def test_ctrl_d_or_closed_input_skips_instead_of_failing(terminal):
    r = do_join(input="")
    assert r.exit_code == 0 and "Skipped" in r.output and installed() == []


def test_the_list_marks_tools_that_are_already_set_up(terminal):
    do_join("--tools", "kiro")
    r = runner.invoke(cli.app, ["join", "K7QX-M9TF", "--name", "Matteo", "--api-url", "https://api.test", "--force"],
                      input="\n")
    assert "2) Kiro CLI   (already installed)" in r.output and "1) Claude Code\n" in r.output


# ---- nothing to pick from ----------------------------------------------------------------------

def test_no_supported_tool_on_the_machine_says_so_and_lists_what_is_supported(machine, tmp_path, monkeypatch):
    (tmp_path / ".claude").rmdir()
    (tmp_path / ".kiro").rmdir()
    r = do_join()
    assert r.exit_code == 0
    assert "No supported coding tools were found on this machine" in r.output and "claude-code" in r.output


# ---- create-team offers it too ---------------------------------------------------------------------

def test_create_team_offers_the_same_picker(machine):
    r = runner.invoke(cli.app, ["create-team", "Demo", "--name", "Matteo", "--api-url", "https://api.test",
                                "--tools", "kiro"])
    assert r.exit_code == 0, r.output
    assert "Join code: K7QX-M9TF" in r.output and installed() == ["kiro"]
    bad = runner.invoke(cli.app, ["create-team", "Demo", "--name", "M", "--api-url", "https://api.test",
                                  "--force", "--tools", "nope"])
    assert bad.exit_code == 2 and machine["create"] == 1  # the typo never reached the API


# ---- parsing --------------------------------------------------------------------------------------

DETECTED = ["claude-code", "kiro", "copilot"]


def test_parse_selection():
    p = tools_cmd.parse_tool_selection
    assert p("2", DETECTED) == ["kiro"]
    assert p("1,3", DETECTED) == ["claude-code", "copilot"]
    assert p("3 1", DETECTED) == ["copilot", "claude-code"]            # order as typed
    assert p("kiro KIRO 2", DETECTED) == ["kiro"]                       # duplicates collapse
    assert p("all", DETECTED) == DETECTED
    assert p("", DETECTED) == p("none", DETECTED) == p("skip", DETECTED) == p("n", DETECTED) == []
    for bad in ("0", "4", "-1", "vscode"):
        with pytest.raises(ValueError):
            p(bad, DETECTED)


def test_validate_tools_option():
    v = tools_cmd.validate_tools_option
    assert v(None) is None and v("none") == [] and v("") == [] and v("all") == ["all"]
    assert v("claude-code,Kiro, kiro") == ["claude-code", "kiro"]
    with pytest.raises(typer.BadParameter):
        v("claude-code,bogus")
    with pytest.raises(typer.BadParameter):
        v("2")  # numbers only make sense at the prompt, where the list is visible


def test_tools_add_still_works_exactly_as_before(machine):
    do_join()
    r = runner.invoke(cli.app, ["tools", "add", "kiro", "--dry-run"])
    assert r.exit_code == 0 and "dry run" in r.output and "raindrop sync" in r.output  # diffs are still shown
    r = runner.invoke(cli.app, ["tools", "add", "kiro", "--yes"])
    assert r.exit_code == 0 and installed() == ["kiro"]
