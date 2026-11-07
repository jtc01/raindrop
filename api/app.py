"""raindrop API — single Lambda serving REST routes + the MCP endpoint.

Run locally:   uvicorn app:app --port 8080
MCP endpoint:  http://localhost:8080/mcp
"""
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

mcp = FastMCP("raindrop")


# ---------- MCP tools (search/list/etc. get added here by teammates) ----------

@mcp.tool
def ping() -> str:
    """Health check tool. Returns 'pong'."""
    return "pong"


# ---------- REST routes ----------

@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


# TODO next: POST /teams, POST /join, POST /messages


# ---------- ASGI app ----------
# Stateless + JSON responses: required for Lambda (no sticky sessions, no SSE).
app = mcp.http_app(
    path="/mcp",
    stateless_http=True,
    json_response=True,
    # middleware=[Middleware(AuthMiddleware)],  # added in the auth step
)
