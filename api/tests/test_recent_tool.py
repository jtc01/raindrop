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
        return calls.get("results", [{"agent_id": "bob", "session_id": "s", "ts": 5, "files_touched": ["a.py"],
                                      "user_text": "q", "assistant_text": "x" * 5000}])

    monkeypatch.setattr(app.recent, "recent_activity", fake_recent)
    return calls


def app_entries(text):
    """The '- ' entry lines inside the untrusted-data fence."""
    return [l for l in text.splitlines() if l.startswith("- ")]


def call(args):
    async def go():
        async with Client(app.mcp) as c:
            tools = [t.name for t in await c.list_tools()]
            return tools, (await c.call_tool("get_recent_activity", args)).data
    return asyncio.run(go())


def test_tool_is_registered_and_returns_attributed_trimmed_text(wired):
    # The tools return compact labeled lines for an agent to read, not raw dicts.
    tools, text = call({"n": 3})
    assert "get_recent_activity" in tools
    [line] = app_entries(text)  # fenced as untrusted data; see test_format.py
    assert line.startswith('- Bob (1970-01-01 00:00 UTC): "q" -> "'.replace("->", "→"))  # who, when, prompt
    assert line.endswith("[files: a.py]")
    assert len(line) < 2500  # the 5000-character reply is trimmed to a snippet (start + end)
    assert wired["team_id"] == "t1" and wired["n"] == 3
    assert wired["exclude"] == "me"  # the caller is left out by default


def test_empty_result_says_so(wired):
    wired["results"] = []
    _, text = call({})
    assert text == "No recent activity."


def test_include_self_and_n_is_clamped(wired):
    call({"n": 1000, "include_self": True})
    assert wired["n"] == app.RECENT_N_MAX and wired["exclude"] is None
    call({"n": 0})
    assert wired["n"] == 1


def test_each_exchange_is_exactly_one_line_even_when_the_text_has_newlines(wired):
    wired["results"] = [
        {"agent_id": "bob", "ts": 5, "files_touched": [], "user_text": "line one\n\nline two\r\n  indented",
         "assistant_text": "first\nsecond\n\n```\ncode block\n```"},
        {"agent_id": "bob", "ts": 6, "files_touched": ["a.py"], "user_text": "q2", "assistant_text": "a2"},
    ]
    _, text = call({"n": 2})
    lines = app_entries(text)  # the entry lines inside the untrusted-data fence
    assert len(lines) == 2  # two exchanges, two lines
    assert "line one line two indented" in lines[0] and "first second ``` code block ```" in lines[0]
    assert lines[1].endswith("[files: a.py]")
