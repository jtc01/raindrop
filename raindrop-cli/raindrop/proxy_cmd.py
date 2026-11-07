"""`raindrop proxy`: a local stdio MCP server that forwards to the team's /mcp.

The coding tool launches this once per window. It re-exposes the remote
server's tools (search_team_context, list_teammates, ...) as if they were
local, sending `Authorization: Bearer <key>` and `X-Session-Id` on every
request.

Failure handling. FastMCP flattens a 401 from the remote into a generic
error, and a proxy whose backend rejects it lists zero tools. The agent would
see nothing and could never learn why. So we:
  - record 401s at the HTTP layer (response hook),
  - turn them into a readable ToolError on tool calls,
  - always expose a local `raindrop_status` tool, which stays listed when the
    remote tools can't be, and explains what is wrong.

The key is re-read from ~/.raindrop/config.json on every request, so running
`raindrop join` again takes effect without restarting the tool.

stdout belongs to the MCP protocol: never print to it here.
"""
from __future__ import annotations

import os
import uuid

from fastmcp import FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from fastmcp.server import create_proxy
from fastmcp.server.middleware import Middleware
from fastmcp.server.providers.proxy import ProxyClient

try:  # fastmcp 4 builds its HTTP clients on httpx2
    import httpx2 as _httpx
except ImportError:  # pragma: no cover
    import httpx as _httpx

from raindrop import config

REJECTED = "raindrop credentials rejected, ask the developer to run `raindrop join`"
NOT_JOINED = "raindrop isn't set up on this machine, ask the developer to run `raindrop join`"
STATUS_TOOL = "raindrop_status"
STATUS_DOC = (
    "Check that Raindrop team context is working. Call this if the other "
    "raindrop tools are missing or failing."
)


class _State:
    """Set by the HTTP response hook when the remote answers 401."""

    rejected = False


class _ConfigAuth(_httpx.Auth):
    """Adds the current key from config.json to every request."""

    def auth_flow(self, request):
        cfg = config.load()
        if cfg and cfg.get("api_key"):
            request.headers["Authorization"] = f"Bearer {cfg['api_key']}"
        yield request


def _client_factory(state: _State):
    async def on_response(response) -> None:
        if response.status_code == 401:
            state.rejected = True

    def make(**kwargs):
        kwargs.setdefault("follow_redirects", True)
        return _httpx.AsyncClient(event_hooks={"response": [on_response]}, **kwargs)

    return make


def _transport(api_url: str, session_id: str, state: _State) -> StreamableHttpTransport:
    return StreamableHttpTransport(
        url=api_url.rstrip("/") + "/mcp",
        headers={"X-Session-Id": session_id},
        auth=_ConfigAuth(),
        httpx_client_factory=_client_factory(state),
    )


class _TranslateErrors(Middleware):
    """Replace opaque failures with a message the agent can act on."""

    def __init__(self, state: _State):
        self.state = state

    async def on_call_tool(self, context, call_next):
        if config.load() is None:
            raise ToolError(NOT_JOINED)
        self.state.rejected = False
        try:
            return await call_next(context)
        except Exception:
            if self.state.rejected:
                raise ToolError(REJECTED) from None
            raise


def build_proxy(cfg: dict | None, session_id: str) -> FastMCP:
    """The local server. With no config it only offers the status tool."""
    state = _State()

    if cfg is None:
        server = FastMCP("raindrop")

        @server.tool(name=STATUS_TOOL, description=STATUS_DOC)
        async def status_not_joined() -> dict:
            raise ToolError(NOT_JOINED)

        return server

    api_url = cfg["api_url"]
    server = create_proxy(
        ProxyClient(_transport(api_url, session_id, state)),
        name="raindrop",
        middleware=[_TranslateErrors(state)],
    )

    @server.tool(name=STATUS_TOOL, description=STATUS_DOC)
    async def status() -> dict:
        if config.load() is None:
            raise ToolError(NOT_JOINED)
        state.rejected = False
        try:
            async with ProxyClient(_transport(api_url, session_id, state)) as client:
                result = await client.call_tool("whoami")
        except Exception as e:
            if state.rejected:
                raise ToolError(REJECTED) from None
            raise ToolError(f"raindrop could not reach {api_url}: {e}") from None
        return {"ok": True, "identity": result.data, "session_id": session_id}

    return server


def proxy() -> None:
    """Run the local MCP proxy over stdio. Your coding tool launches this; you don't."""
    cfg = config.load()
    session_id = os.environ.get("RAINDROP_SESSION_ID") or str(uuid.uuid4())
    build_proxy(cfg, session_id).run(transport="stdio", show_banner=False)
