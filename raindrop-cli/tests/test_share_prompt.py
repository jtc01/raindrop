"""The sharing question asked after the tool picker in `raindrop join` / `raindrop create-team`."""
import pytest
from typer.testing import CliRunner

from raindrop import api_client, cli, config, share_prompt, tools_cmd

runner = CliRunner()
RESP = {"team_id": "t1", "team_name": "Demo", "agent_id": "agt_1", "api_key": "rd_key_1234567890abcdef",
        "join_code": "K7QX-M9TF"}


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """Not yet joined; the current folder is a git repo; no coding tools installed."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / ".raindrop")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / ".raindrop" / "config.json")
    repo = tmp_path / "proj"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(api_client, "join", lambda *a, **k: dict(RESP))
    monkeypatch.setattr(api_client, "create_team", lambda *a, **k: dict(RESP))
    monkeypatch.setattr(tools_cmd, "_interactive", lambda: False)
    monkeypatch.setattr(share_prompt, "_interactive", lambda: False)
    return repo.resolve()


def join(*extra, input=None):
    return runner.invoke(cli.app, ["join", "K7QX-M9TF", "--name", "M", "--api-url", "https://api.test",
                                   "--tools", "none", *extra], input=input)


def sharing():
    return (config.load() or {}).get("sharing", {})


def test_without_a_terminal_nothing_is_shared_and_a_hint_is_printed(machine):
    r = join()
    assert r.exit_code == 0 and "Sharing is off until you turn it on" in r.output
    assert not sharing().get("shared_repos") and sharing().get("mode") == "opt_in"


def test_share_repo_shares_the_repo_you_are_in(machine):
    r = join("--share", "repo")
    assert r.exit_code == 0, r.output
    assert sharing()["shared_repos"] == [str(machine)] and sharing()["mode"] == "opt_in"


def test_share_all_switches_to_all_mode(machine):
    assert join("--share", "all").exit_code == 0
    assert sharing()["mode"] == "all"


def test_share_none_shares_nothing(machine):
    r = join("--share", "none")
    assert r.exit_code == 0 and not sharing().get("shared_repos") and sharing()["mode"] == "opt_in"


def test_a_typo_fails_before_anything_is_joined(machine):
    r = join("--share", "maybe")
    assert r.exit_code != 0 and config.load() is None


def test_share_repo_outside_a_git_repo_shares_nothing_and_says_so(machine, monkeypatch):
    monkeypatch.setattr(share_prompt, "_current_repo", lambda: None)  # (the temp dir may sit inside a git home)
    r = join("--share", "repo")
    assert r.exit_code == 0 and "not inside a git repo" in r.output
    assert not sharing().get("shared_repos")


@pytest.fixture
def terminal(machine, monkeypatch):
    monkeypatch.setattr(share_prompt, "_interactive", lambda: True)
    return machine


def test_the_prompt_explains_and_option_1_shares_this_repo(terminal):
    r = join(input="1\n")
    assert "Share your coding sessions with the team?" in r.output and str(terminal) in r.output
    assert sharing()["shared_repos"] == [str(terminal)]


def test_option_2_shares_everything(terminal):
    join(input="2\n")
    assert sharing()["mode"] == "all"


def test_enter_alone_means_no(terminal):
    r = join(input="\n")
    assert "Not sharing" in r.output and not sharing().get("shared_repos") and sharing()["mode"] == "opt_in"


def test_a_bad_answer_asks_again(terminal):
    r = join(input="9\n1\n")
    assert "Choose 1-2" in r.output and sharing()["shared_repos"] == [str(terminal)]


def test_outside_a_repo_the_only_options_are_all_or_no(terminal, monkeypatch):
    monkeypatch.setattr(share_prompt, "_current_repo", lambda: None)
    r = join(input="1\n")
    assert "This repo only" not in r.output and "1) Every repo" in r.output
    assert sharing()["mode"] == "all"


def test_the_question_comes_after_the_tool_picker(machine):
    r = runner.invoke(cli.app, ["join", "K7QX-M9TF", "--name", "M", "--api-url", "https://api.test"])
    assert r.exit_code == 0
    assert r.output.index("raindrop tools add") < r.output.index("Sharing is off until")


def test_create_team_asks_too(machine):
    r = runner.invoke(cli.app, ["create-team", "Demo", "--name", "M", "--api-url", "https://api.test",
                                "--tools", "none", "--share", "repo"])
    assert r.exit_code == 0, r.output
    assert sharing()["shared_repos"] == [str(machine)]
