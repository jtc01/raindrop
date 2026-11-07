import json

import pytest
from typer.testing import CliRunner

from raindrop import cli, config, constants
from raindrop.adapters import antigravity, copilot, registry

runner = CliRunner()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("COPILOT_HOME", raising=False)
    monkeypatch.delenv("RAINDROP_HOME", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / ".raindrop")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / ".raindrop" / "config.json")
    config.save({"api_url": "https://api.test", "api_key": "rd_key_1234567890abcdef", "agent_id": "a",
                 "team_id": "t", "display_name": "D", "focus": "", "tools": {}})
    return tmp_path


def add(tool, *flags):
    return runner.invoke(cli.app, ["tools", "add", tool, "--yes", *flags])


def test_registered():
    assert {"antigravity", "copilot"} <= set(registry.all_names())


# ---- Antigravity ----------------------------------------------------------


def test_antigravity_detect(env):
    a = antigravity.AntigravityAdapter()
    (env / ".gemini").mkdir()
    assert not a.detect()  # Gemini CLI also uses ~/.gemini
    (env / ".gemini" / "antigravity").mkdir()
    assert a.detect()


def test_antigravity_install_remove_roundtrip(env):
    mcp = antigravity.gemini_dir() / "config" / "mcp_config.json"
    mcp.parent.mkdir(parents=True)
    mcp.write_text(json.dumps({"mcpServers": {"sqlite": {"command": "node", "args": ["s.js"]}}}))

    r = runner.invoke(cli.app, ["tools", "add", "antigravity", "--dry-run", "--rules"])
    assert r.exit_code == 0 and "dry run" in r.output
    rules = antigravity.AntigravityAdapter().rules_path()
    assert not rules.exists()

    for _ in range(2):  # idempotent
        r = add("antigravity", "--rules")
        assert r.exit_code == 0, r.output
    assert "Google Antigravity [ok] MCP + auto-sync hook + rules" in r.output
    hooks = antigravity.AntigravityAdapter().hooks_path()
    entry = json.loads(hooks.read_text())["raindrop"]["Stop"]
    assert len(entry) == 1 and entry[0]["type"] == "command"  # re-install replaced it, no duplicate
    assert "sync --tool antigravity --fail-open" in entry[0]["command"]

    servers = json.loads(mcp.read_text())["mcpServers"]
    assert set(servers) == {"sqlite", constants.MCP_SERVER_NAME}
    assert servers[constants.MCP_SERVER_NAME]["args"] == ["proxy"]
    # modular rules are silently dropped without frontmatter declaring a trigger
    text = rules.read_text()
    assert text.startswith("---\ntrigger: always_on\n") and "search_team_context" in text
    assert config.load()["tools"]["antigravity"] == {
        **config.load()["tools"]["antigravity"], "mcp": True, "hook": True, "rules": True}

    r = runner.invoke(cli.app, ["tools", "remove", "antigravity"])
    assert r.exit_code == 0
    assert set(json.loads(mcp.read_text())["mcpServers"]) == {"sqlite"}
    assert not rules.exists()
    assert "raindrop" not in json.loads(hooks.read_text())


def test_antigravity_hook_keeps_other_hooks(env):
    hooks = antigravity.AntigravityAdapter().hooks_path()
    hooks.parent.mkdir(parents=True)
    other = {"mine": {"Stop": [{"type": "command", "command": "echo hi"}]}}
    hooks.write_text(json.dumps(other))
    assert add("antigravity").exit_code == 0
    assert set(json.loads(hooks.read_text())) == {"mine", "raindrop"}
    assert runner.invoke(cli.app, ["tools", "remove", "antigravity"]).exit_code == 0
    assert json.loads(hooks.read_text()) == other


# ---- Copilot --------------------------------------------------------------


def test_copilot_detect_and_copilot_home(env, monkeypatch):
    a = copilot.CopilotAdapter()
    assert not a.detect()
    (env / ".copilot").mkdir()
    assert a.detect() and a.mcp_path() == env / ".copilot" / "mcp-config.json"
    monkeypatch.setenv("COPILOT_HOME", str(env / "elsewhere"))
    assert a.mcp_path() == env / "elsewhere" / "mcp-config.json"


def test_copilot_install_remove_keeps_user_content(env):
    d = env / ".copilot"
    d.mkdir()
    (d / "mcp-config.json").write_text(json.dumps(
        {"mcpServers": {"playwright": {"type": "local", "command": "npx", "args": ["@playwright/mcp"], "tools": ["*"]}}}))
    (d / "copilot-instructions.md").write_text("# My rules\nAlways use tabs.\n")

    for _ in range(2):
        r = add("copilot", "--rules")
        assert r.exit_code == 0, r.output
    assert "GitHub Copilot [ok] MCP + auto-sync hook + rules" in r.output
    hook = json.loads((d / "hooks" / "raindrop.json").read_text())
    [entry] = hook["hooks"]["agentStop"]  # re-install replaced it, no duplicate
    assert hook["version"] == 1 and entry["type"] == "command" and entry["timeoutSec"] == 60
    assert "sync --tool copilot --fail-open" in entry["command"]

    servers = json.loads((d / "mcp-config.json").read_text())["mcpServers"]
    entry = servers[constants.MCP_SERVER_NAME]
    assert set(servers) == {"playwright", constants.MCP_SERVER_NAME}
    assert entry["type"] == "local" and entry["tools"] == ["*"] and entry["args"] == ["proxy"]
    # Copilot gives MCP servers only PATH, so the proxy must be told where config.json is
    assert entry["env"] == {"RAINDROP_HOME": str(env / ".raindrop")}
    instr = (d / "copilot-instructions.md").read_text()
    assert instr.startswith("# My rules\nAlways use tabs.\n") and instr.count(constants.RULES_START) == 1

    r = runner.invoke(cli.app, ["tools", "remove", "copilot"])
    assert r.exit_code == 0
    assert not (d / "hooks" / "raindrop.json").exists()  # the hook file was ours, so it is deleted
    assert set(json.loads((d / "mcp-config.json").read_text())["mcpServers"]) == {"playwright"}
    assert (d / "copilot-instructions.md").read_text() == "# My rules\nAlways use tabs.\n"


def test_copilot_creates_files_from_scratch_and_cleans_up(env):
    d = env / ".copilot"
    d.mkdir()  # Copilot is installed (its folder exists) but has no config files yet
    r = add("copilot", "--rules")
    assert r.exit_code == 0, r.output
    assert (d / "copilot-instructions.md").read_text().startswith(constants.RULES_START)
    runner.invoke(cli.app, ["tools", "remove", "copilot"])
    assert json.loads((d / "mcp-config.json").read_text()) == {"mcpServers": {}}
    assert (d / "copilot-instructions.md").read_text().strip() == ""


def test_not_joined_is_refused(env):
    config.CONFIG_PATH.unlink()
    r = add("copilot")
    assert r.exit_code == 1
