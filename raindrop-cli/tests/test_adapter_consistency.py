"""Every registered tool adapter is held to the same contract, so a new or changed
adapter can't quietly behave differently from the others."""
import json

import pytest
from typer.testing import CliRunner

from raindrop import cli, config
from raindrop.adapters import registry
from raindrop.adapters._common import is_our_hook_command
from raindrop.errors import TranscriptError

runner = CliRunner()
NAMES = registry.all_names()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    monkeypatch.delenv("COPILOT_HOME", raising=False)
    monkeypatch.delenv("RAINDROP_HOME", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / ".raindrop")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / ".raindrop" / "config.json")
    config.save({"api_url": "https://api.test", "api_key": "rd_key_1234567890abcdef", "agent_id": "a",
                 "team_id": "t", "display_name": "D", "focus": "", "tools": {}, "sharing": {"mode": "all"}})
    return tmp_path


def snapshot(root):
    skip = root / ".raindrop"
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file() and skip not in p.parents}


def written(root, before):
    return {k: v for k, v in snapshot(root).items() if before.get(k) != v}


@pytest.mark.parametrize("name", NAMES)
def test_identity(name):
    a = registry.get_adapter(name)
    assert a.name == name and a.display_name.strip()


@pytest.mark.parametrize("name", NAMES)
def test_planning_never_touches_disk(home, name):
    before = snapshot(home)
    registry.get_adapter(name).plan_install(rules=True)
    assert snapshot(home) == before


@pytest.mark.parametrize("name", NAMES)
def test_install_is_idempotent_and_syncs_this_tool(home, name):
    a = registry.get_adapter(name)
    before = snapshot(home)
    status = a.apply(a.plan_install(rules=True))
    first = snapshot(home)
    status = a.apply(a.plan_install(rules=True))
    assert snapshot(home) == first  # installing again changes nothing
    assert status.hook, f"{name}: no stop hook was installed"
    new_text = "\n".join(v.decode("utf-8", "replace") for v in written(home, before).values())
    assert f"sync --tool {name}" in new_text.replace("\\\\", "/"), f"{name}: hook doesn't sync this tool"
    assert any(is_our_hook_command(line) for line in new_text.splitlines() if "sync" in line)


# Known gap: these two install a hook (the tool can share) but no MCP server (it can't read the
# team's context). strict=True so that fixing it makes this xfail an error, prompting its removal.
NO_MCP_YET = {"codex", "cursor"}


@pytest.mark.parametrize("name", [
    pytest.param(n, marks=pytest.mark.xfail(strict=True, reason="adapter doesn't register the MCP server yet"))
    if n in NO_MCP_YET else n
    for n in NAMES
])
def test_install_registers_the_mcp_server_so_the_tool_can_read_team_context(home, name):
    a = registry.get_adapter(name)
    status = a.apply(a.plan_install(rules=False))
    assert status.mcp, f"{name}: installs a hook but not the MCP server"


@pytest.mark.parametrize("name", NAMES)
def test_uninstall_leaves_no_raindrop_hook_behind(home, name):
    a = registry.get_adapter(name)
    before = snapshot(home)
    status = a.apply(a.plan_install(rules=True))
    a.uninstall(status.to_tool_record())
    leftovers = {k: v for k, v in written(home, before).items()
                 if b"sync --tool" in v.replace(b"\\\\", b"/") or b"raindrop" in v.lower() and k.endswith(".json")}
    assert not leftovers, f"{name}: still has raindrop entries in {sorted(leftovers)}"


@pytest.mark.parametrize("name", NAMES)
def test_bad_hook_payload_is_a_clear_error_not_a_crash(home, name):
    a = registry.get_adapter(name)
    try:
        a.session_from_hook({})
    except TranscriptError:
        pass  # clear, catchable error


@pytest.mark.parametrize("name", NAMES)
def test_sync_command_never_crashes_on_garbage_and_fail_open_exits_zero(home, name):
    r = runner.invoke(cli.app, ["sync", "--tool", name, "--fail-open"], input="not json")
    assert r.exit_code == 0 and "Traceback" not in r.output
    r = runner.invoke(cli.app, ["sync", "--tool", name], input="not json")
    assert r.exit_code == 1  # without --fail-open a failure is reported


@pytest.mark.parametrize("name", NAMES)
def test_hook_stdout_is_none_or_valid_json(home, name):
    out = registry.get_adapter(name).hook_stdout()
    assert out is None or isinstance(json.loads(out), dict)
