"""End to end: the real `raindrop proxy` process over stdio, against a local
fake /mcp that enforces the Bearer key like the API's AuthMiddleware."""
import asyncio
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StdioTransport
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from starlette.middleware import Middleware
from starlette.responses import JSONResponse

CLI_DIR = Path(__file__).resolve().parents[1]
GOOD, BAD = "rd_good_key_1234567890abcdef", "rd_stale_key_1234567890abcdef"
seen_sessions: list[str] = []


class Auth:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        h = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])} if scope["type"] == "http" else {}
        if scope["type"] == "http" and h.get("authorization") != f"Bearer {GOOD}":
            return await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
        if scope["type"] == "http":
            seen_sessions.append(h.get("x-session-id", ""))
        return await self.app(scope, receive, send)


@pytest.fixture(scope="module")
def backend_url():
    mcp = FastMCP("backend")

    @mcp.tool
    def search_team_context(query: str) -> str:
        return f"result for {query} (session {get_http_request().headers.get('x-session-id')})"

    @mcp.tool
    def whoami() -> dict:
        return {"agent_id": "a1", "team_id": "t1"}

    app = mcp.http_app(path="/mcp", stateless_http=True, json_response=True,
                       middleware=[Middleware(Auth)])
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True


def write_config(home: Path, url: str | None, key: str = GOOD) -> None:
    d = home / ".raindrop"
    d.mkdir(exist_ok=True)
    cfg = d / "config.json"
    if url is None:
        cfg.unlink(missing_ok=True)
        return
    cfg.write_text(json.dumps({"api_url": url, "api_key": key, "agent_id": "a1", "team_id": "t1",
                               "display_name": "D", "focus": "", "tools": {}}))


def proxy_client(home: Path) -> Client:
    return Client(StdioTransport(
        command=sys.executable,
        args=["-c", "from raindrop.cli import app; app()", "proxy"],
        # Inherit the environment: a bare dict loses SYSTEMROOT, which Python needs on Windows.
        env={**os.environ, "HOME": str(home), "USERPROFILE": str(home),
             "RAINDROP_HOME": str(home / ".raindrop"), "PYTHONPATH": str(CLI_DIR),
             "RAINDROP_SESSION_ID": "sess-test-1"},
        cwd=str(CLI_DIR),
    ))


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 60))


def test_forwards_tools_with_auth_and_session_header(tmp_path, backend_url):
    write_config(tmp_path, backend_url)
    seen_sessions.clear()

    async def go():
        async with proxy_client(tmp_path) as c:
            names = {t.name for t in await c.list_tools()}
            res = await c.call_tool("search_team_context", {"query": "auth"})
            status = await c.call_tool("raindrop_status")
            return names, res.data, status.data

    names, result, status = run(go())
    assert {"search_team_context", "whoami", "raindrop_status"} <= names
    assert result == "result for auth (session sess-test-1)"
    assert status["ok"] and status["identity"] == {"agent_id": "a1", "team_id": "t1"}
    assert set(seen_sessions) == {"sess-test-1"}


def test_rejected_key_gives_readable_error_and_recovers_after_rejoin(tmp_path, backend_url):
    write_config(tmp_path, backend_url, key=BAD)

    async def go():
        out = {}
        async with proxy_client(tmp_path) as c:
            out["names_bad"] = {t.name for t in await c.list_tools()}
            for tool, args in (("search_team_context", {"query": "x"}), ("raindrop_status", {})):
                try:
                    await c.call_tool(tool, args)
                except ToolError as e:
                    out[tool] = str(e)
            write_config(tmp_path, backend_url, key=GOOD)  # developer re-runs `raindrop join`
            out["names_good"] = {t.name for t in await c.list_tools()}
            out["after"] = (await c.call_tool("search_team_context", {"query": "y"})).data
        return out

    out = run(go())
    assert out["names_bad"] == {"raindrop_status"}  # not silently empty: the agent can see why
    for tool in ("search_team_context", "raindrop_status"):
        assert "credentials rejected" in out[tool] and "raindrop join" in out[tool]
    assert "search_team_context" in out["names_good"]
    assert out["after"].startswith("result for y")


def test_not_joined(tmp_path):
    write_config(tmp_path, None)

    async def go():
        async with proxy_client(tmp_path) as c:
            names = {t.name for t in await c.list_tools()}
            try:
                await c.call_tool("raindrop_status")
            except ToolError as e:
                return names, str(e)

    names, err = run(go())
    assert names == {"raindrop_status"} and "raindrop join" in err
