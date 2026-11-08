"""Codex and Cursor register the Raindrop MCP server, so they can read team context
(not only share their own sessions)."""
import json
import tomllib

import pytest

from raindrop import constants
from raindrop.adapters import codex, cursor
from raindrop.adapters.codex import CodexAdapter
from raindrop.adapters.cursor import CursorAdapter


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("RAINDROP_HOME", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    return tmp_path


def install(adapter):
    return adapter.apply(adapter.plan_install(rules=True))


# ---- Cursor: ~/.cursor/mcp.json ---------------------------------------------


def test_cursor_mcp_is_added_beside_other_servers_and_removed_cleanly(home):
    path = home / ".cursor" / "mcp.json"
    path.parent.mkdir()
    other = {"command": "npx", "args": ["@playwright/mcp"]}
    path.write_text(json.dumps({"mcpServers": {"playwright": other}, "keep": 1}))

    status = install(CursorAdapter())
    install(CursorAdapter())  # idempotent
    data = json.loads(path.read_text())
    assert status.mcp and status.hook
    assert set(data["mcpServers"]) == {"playwright", constants.MCP_SERVER_NAME}
    assert data["mcpServers"][constants.MCP_SERVER_NAME]["args"] == ["proxy"]
    assert data["mcpServers"]["playwright"] == other and data["keep"] == 1

    CursorAdapter().uninstall(status.to_tool_record())
    data = json.loads(path.read_text())
    assert data["mcpServers"] == {"playwright": other} and data["keep"] == 1
    assert "sync --tool cursor" not in (home / ".cursor" / "hooks.json").read_text()


def test_cursor_mcp_is_created_from_scratch(home):
    (home / ".cursor").mkdir()
    install(CursorAdapter())
    assert constants.MCP_SERVER_NAME in json.loads((home / ".cursor" / "mcp.json").read_text())["mcpServers"]


def test_cursor_planning_does_not_write(home):
    CursorAdapter().plan_install(rules=True)
    assert not (home / ".cursor").exists()


# ---- Codex: [mcp_servers.raindrop] in ~/.codex/config.toml ------------------

ORIGINAL = (
    'model = "gpt-5"\n'
    "\n"
    "[mcp_servers.playwright]\n"
    'command = "npx"\n'
    'args = ["@playwright/mcp"]\n'
    "\n"
    "[projects.'C:\\Users\\me\\proj']\n"
    'trust_level = "trusted"\n'
)


def test_codex_mcp_edits_only_our_table_and_the_file_still_parses(home):
    path = home / ".codex" / "config.toml"
    path.parent.mkdir()
    path.write_text(ORIGINAL, encoding="utf-8")

    status = install(CodexAdapter())
    first = path.read_text(encoding="utf-8")
    install(CodexAdapter())
    assert path.read_text(encoding="utf-8") == first  # idempotent
    assert status.mcp and status.hook

    data = tomllib.loads(first)
    ours = data["mcp_servers"][constants.MCP_SERVER_NAME]
    assert ours["args"] == ["proxy"] and ours["command"].lower().endswith(("raindrop", "raindrop.exe"))
    assert ours["env"] == {"RAINDROP_HOME": str(home / ".raindrop")}  # MCP servers may get a bare environment
    assert data["model"] == "gpt-5" and data["mcp_servers"]["playwright"]["args"] == ["@playwright/mcp"]
    assert list(data["projects"]) == ["C:\\Users\\me\\proj"]
    assert first.startswith(ORIGINAL.rstrip())  # everything the user wrote is untouched

    CodexAdapter().uninstall(status.to_tool_record())
    assert path.read_text(encoding="utf-8").strip() == ORIGINAL.strip()
    assert "sync --tool codex" not in (home / ".codex" / "hooks.json").read_text()


def test_codex_replaces_a_stale_table_wherever_it_sits(home):
    path = home / ".codex" / "config.toml"
    path.parent.mkdir()
    path.write_text(
        'a = 1\n\n[mcp_servers.raindrop] # old\ncommand = "OLDCMD"\n\n'
        '[mcp_servers.raindrop.env]\nK = "v"\n\n[other]\nb = 2\n',
        encoding="utf-8",
    )
    install(CodexAdapter())
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data["a"] == 1 and data["other"] == {"b": 2}
    assert data["mcp_servers"][constants.MCP_SERVER_NAME]["args"] == ["proxy"]
    assert "OLDCMD" not in path.read_text(encoding="utf-8") and "K =" not in path.read_text(encoding="utf-8")


def test_codex_path_with_a_quote_still_produces_valid_toml(home, monkeypatch):
    monkeypatch.setattr(codex, "exe", lambda: "C:\\o'brien\\raindrop.exe")
    install(CodexAdapter())
    data = tomllib.loads((home / ".codex" / "config.toml").read_text(encoding="utf-8"))
    assert data["mcp_servers"][constants.MCP_SERVER_NAME]["command"] == "C:\\o'brien\\raindrop.exe"


def test_codex_created_from_scratch_and_removed_to_empty(home):
    (home / ".codex").mkdir()
    status = install(CodexAdapter())
    assert constants.MCP_SERVER_NAME in tomllib.loads((home / ".codex" / "config.toml").read_text())["mcp_servers"]
    CodexAdapter().uninstall(status.to_tool_record())
    assert (home / ".codex" / "config.toml").read_text() == ""


def test_codex_planning_does_not_write(home):
    CodexAdapter().plan_install(rules=True)
    assert not (home / ".codex").exists()
