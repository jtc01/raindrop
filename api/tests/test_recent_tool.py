import asyncio

import pytest
from fastmcp import Client

import app


@pytest.fixture
def wired(monkeypatch):
    calls = {}
    monkeypatch.setattr(app, "current_identity",
                        lambda: {"team_id": "t1", "agent_id": "me", "display_name": "Me", "session_id": None})
    monkeypatch.setattr(app, "list_team_members",
                        lambda team: [{"agent_id": "bob", "display_name": "Bob"}, {"agent_id": "me", "display_name": "Me"}])

    def fake_recent(team_id, n, exclude_agent_id=None, s3=None):
        calls.update(team_id=team_id, n=n, exclude=exclude_agent_id)
        return [{"agent_id": "bob", "session_id": "s", "ts": 5, "files_touched": ["a.py"],
                 "user_text": "q", "assistant_text": "x" * 5000}]

    monkeypatch.setattr(app.recent, "recent_activity", fake_recent)
    return calls


def call(args):
    async def go():
        async with Client(app.mcp) as c:
            tools = [t.name for t in await c.list_tools()]
            return tools, (await c.call_tool("get_recent_activity", args)).data
    return asyncio.run(go())


def test_tool_is_registered_and_returns_attributed_trimmed_results(wired):
    tools, data = call({"n": 3})
    assert "get_recent_activity" in tools
    [r] = data["results"]
    assert r["display_name"] == "Bob" and r["agent_id"] == "bob" and r["ts"] == 5
    assert r["files_touched"] == ["a.py"] and len(r["assistant_text"]) < 1600  # trimmed
    assert wired == {"team_id": "t1", "n": 3, "exclude": "me"}  # caller excluded by default


def test_include_self_and_n_is_clamped(wired):
    call({"n": 1000, "include_self": True})
    assert wired["n"] == app.RECENT_N_MAX and wired["exclude"] is None
    call({"n": 0})
    assert wired["n"] == 1
