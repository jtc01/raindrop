"""Raindrop API — single Lambda serving REST routes + the MCP endpoint.

Run locally:   uvicorn app:app --port 8080
MCP endpoint:  http://localhost:8080/mcp

MCP tools:      ping, whoami, search_team_context, get_recent_activity, list_teammates
Public routes:  GET /health, POST /teams, POST /join
Authed routes:  /mcp, POST /messages, POST /invite, GET /me, GET /teammates,
                POST /rotate-key, POST /leave, POST /remove (lead), GET /codes,
                POST /codes/revoke — require `Authorization: Bearer <key>`
"""
import hashlib
import json
import os
import re
import secrets
import time
import uuid

import anyio
import boto3
from boto3.dynamodb.conditions import Key
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse

import messages  # ingestion: S3 write + Titan embedding + S3 Vectors upsert
import recent  # newest exchanges straight from S3 (no embedding)
import search  # retrieval: embed query, scoped vector search, rerank, hydrate

# ---------- config ----------

TEAMS_TABLE = os.environ.get("TEAMS_TABLE", "raindrop-teams")
JOIN_CODES_TABLE = os.environ.get("JOIN_CODES_TABLE", "raindrop-join-codes")
AGENTS_TABLE = os.environ.get("AGENTS_TABLE", "raindrop-agents")

JOIN_CODE_TTL_SECONDS = 48 * 3600
JOIN_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I
AUTH_CACHE_TTL_SECONDS = 60
PUBLIC_PATHS = {"/health", "/teams", "/join"}

_ddb = boto3.resource("dynamodb")
teams = _ddb.Table(TEAMS_TABLE)
join_codes = _ddb.Table(JOIN_CODES_TABLE)
agents = _ddb.Table(AGENTS_TABLE)


# ---------- helpers ----------

def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_join_code() -> str:
    raw = "".join(secrets.choice(JOIN_CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def normalize_code(code: str) -> str:
    return code.upper().replace("-", "").replace(" ", "")


def create_agent(team_id: str, display_name: str, focus: str) -> dict:
    """Creates an agent and returns its plaintext key. The key is never stored."""
    api_key = secrets.token_urlsafe(32)
    agent_id = f"agt_{uuid.uuid4().hex[:12]}"
    agents.put_item(Item={
        "key_hash": sha256(api_key),
        "agent_id": agent_id,
        "team_id": team_id,
        "display_name": display_name,
        "focus": focus,
        "status": "active",
        "created_at": int(time.time()),
    })
    return {"agent_id": agent_id, "api_key": api_key}


def issue_join_code(team_id: str, created_by: str | None = None) -> tuple[str, int]:
    """Creates a new join code for a team. Only its hash is stored."""
    code = new_join_code()
    expires_at = int(time.time()) + JOIN_CODE_TTL_SECONDS
    item = {
        "code_hash": sha256(normalize_code(code)),
        "team_id": team_id,
        "expires_at": expires_at,
        "uses": 0,
    }
    if created_by:
        item["created_by"] = created_by
    join_codes.put_item(Item=item)
    return code, expires_at


def error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status)


# ---------- auth middleware (pure ASGI) ----------

_auth_cache: dict[str, tuple[float, dict | None]] = {}


def _lookup_agent(key_hash: str) -> dict | None:
    hit = _auth_cache.get(key_hash)
    if hit and hit[0] > time.time():
        return hit[1]
    item = agents.get_item(Key={"key_hash": key_hash}).get("Item")
    agent = item if item and item.get("status") == "active" else None
    _auth_cache[key_hash] = (time.time() + AUTH_CACHE_TTL_SECONDS, agent)
    return agent


class AuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] in PUBLIC_PATHS:
            return await self.app(scope, receive, send)

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        auth = headers.get(b"authorization", b"").decode()
        agent = None
        if auth.lower().startswith("bearer "):
            key = auth[7:].strip()
            if key:
                agent = await anyio.to_thread.run_sync(_lookup_agent, sha256(key))

        if agent is None:
            resp = error(401, "unauthorized",
                         "Raindrop credentials rejected. Ask the developer to run `raindrop join`.")
            return await resp(scope, receive, send)

        scope.setdefault("state", {})["raindrop"] = {
            "team_id": agent["team_id"],
            "agent_id": agent["agent_id"],
            "display_name": agent["display_name"],
            "session_id": headers.get(b"x-session-id", b"").decode() or None,
        }
        # Internal only (rotate/leave need the caller's key record). Never returned to clients.
        scope["state"]["raindrop_key_hash"] = agent["key_hash"]
        return await self.app(scope, receive, send)


def current_identity() -> dict:
    """Use inside MCP tools: returns team_id, agent_id, display_name, session_id."""
    return get_http_request().state.raindrop


# ---------- MCP ----------

mcp = FastMCP("raindrop")


@mcp.tool
def ping() -> str:
    """Health check tool. Returns 'pong'."""
    return "pong"


@mcp.tool
def whoami() -> dict:
    """Returns the team and agent this connection is authenticated as."""
    return current_identity()


SEARCH_TEXT_MAX_CHARS = 1500
SEARCH_TOP_K_MAX = 25


def _trim(text: str | None, limit: int = SEARCH_TEXT_MAX_CHARS) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


@mcp.tool
def search_team_context(query: str, top_k: int = 8) -> dict:
    """Semantic search over what your teammates' agents have been working on.
    Returns the most relevant recent exchanges (prompt + reply + files touched)
    from everyone on your team, including you. Use it before starting work that
    may overlap with a teammate's, or to find out who touched a file or feature."""
    ident = current_identity()
    top_k = max(1, min(int(top_k), SEARCH_TOP_K_MAX))
    results = search.search(ident["team_id"], query, top_k)
    return {
        "query": query,
        "results": [{
            "display_name": r.get("display_name") or r.get("agent_id") or "unknown",
            "agent_id": r.get("agent_id"),
            "ts": r.get("ts"),
            "files_touched": r.get("files_touched", []),
            "user_text": _trim(r.get("user_text")),
            "assistant_text": _trim(r.get("assistant_text")),
            "score": round(float(r.get("score", 0.0)), 4),
        } for r in results],
    }


RECENT_N_MAX = 25


@mcp.tool
def get_recent_activity(n: int = 10, include_self: bool = False) -> dict:
    """The n newest exchanges (prompt + reply + files touched) from your teammates'
    agents, newest first. Use it to catch up on what the team has been doing without
    a specific question; use search_team_context when you are looking for something
    particular. Your own exchanges are left out unless include_self is true."""
    ident = current_identity()
    n = max(1, min(int(n), RECENT_N_MAX))
    results = recent.recent_activity(
        ident["team_id"], n, exclude_agent_id=None if include_self else ident["agent_id"]
    )
    names = {m["agent_id"]: m["display_name"] for m in list_team_members(ident["team_id"])}
    return {
        "results": [{
            "display_name": names.get(r.get("agent_id"), r.get("agent_id") or "unknown"),
            "agent_id": r.get("agent_id"),
            "session_id": r.get("session_id"),
            "ts": r.get("ts"),
            "files_touched": r.get("files_touched", []),
            "user_text": _trim(r.get("user_text")),
            "assistant_text": _trim(r.get("assistant_text")),
        } for r in results],
    }


@mcp.tool
def list_teammates() -> dict:
    """Lists the active members of your team (display_name, focus, agent_id)."""
    ident = current_identity()
    members = list_team_members(ident["team_id"])
    for m in members:
        m["you"] = m["agent_id"] == ident["agent_id"]
    return {"team_id": ident["team_id"], "members": members}


# ---------- REST routes ----------

@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


@mcp.custom_route("/teams", methods=["POST"])
async def create_team(request: Request) -> JSONResponse:
    """Body: {name, display_name?, focus?}
    Returns team_id + join_code. If display_name is given, the lead is auto-joined
    and the response also includes agent_id + api_key (shown once)."""
    try:
        body = await request.json()
    except Exception:
        return error(400, "bad_request", "Body must be JSON.")
    name = (body.get("name") or "").strip()
    if not name or len(name) > 80:
        return error(400, "bad_request", "`name` is required (max 80 chars).")

    def work():
        team_id = f"team_{uuid.uuid4().hex[:12]}"
        teams.put_item(Item={"team_id": team_id, "name": name, "created_at": int(time.time())})
        code, expires_at = issue_join_code(team_id)
        result = {"team_id": team_id, "team_name": name,
                  "join_code": code, "join_code_expires_at": expires_at}
        display_name = (body.get("display_name") or "").strip()
        if display_name:
            agent = create_agent(team_id, display_name, (body.get("focus") or "").strip())
            teams.update_item(Key={"team_id": team_id},
                              UpdateExpression="SET lead_agent_id = :a",
                              ExpressionAttributeValues={":a": agent["agent_id"]})
            join_codes.update_item(Key={"code_hash": sha256(normalize_code(code))},
                                   UpdateExpression="SET created_by = :a",
                                   ExpressionAttributeValues={":a": agent["agent_id"]})
            result.update(agent)
        return result

    return JSONResponse(await anyio.to_thread.run_sync(work), status_code=201)


@mcp.custom_route("/join", methods=["POST"])
async def join(request: Request) -> JSONResponse:
    """Body: {code, display_name, focus?}. Returns team_id, agent_id, api_key (shown once)."""
    try:
        body = await request.json()
    except Exception:
        return error(400, "bad_request", "Body must be JSON.")
    code = normalize_code(body.get("code") or "")
    display_name = (body.get("display_name") or "").strip()
    if len(code) != 8 or not display_name or len(display_name) > 40:
        return error(400, "bad_request", "`code` (8 chars) and `display_name` (max 40) are required.")

    def work():
        item = join_codes.get_item(Key={"code_hash": sha256(code)}).get("Item")
        if not item:
            return None, error(404, "invalid_code", "Join code not found.")
        if int(item["expires_at"]) < time.time():
            return None, error(410, "expired_code", "Join code has expired.")
        join_codes.update_item(Key={"code_hash": sha256(code)},
                               UpdateExpression="ADD uses :one",
                               ExpressionAttributeValues={":one": 1})
        team = teams.get_item(Key={"team_id": item["team_id"]}).get("Item") or {}
        agent = create_agent(item["team_id"], display_name, (body.get("focus") or "").strip())
        return {"team_id": item["team_id"], "team_name": team.get("name"), **agent}, None

    result, err = await anyio.to_thread.run_sync(work)
    return err or JSONResponse(result, status_code=201)


@mcp.custom_route("/invite", methods=["POST"])
async def invite(request: Request) -> JSONResponse:
    """Authed. Issues a fresh join code for the caller's team (any member can invite).
    Older codes keep working until they expire."""
    ident = request.state.raindrop  # set by AuthMiddleware
    code, expires_at = await anyio.to_thread.run_sync(
        lambda: issue_join_code(ident["team_id"], created_by=ident["agent_id"]))
    team = await anyio.to_thread.run_sync(
        lambda: teams.get_item(Key={"team_id": ident["team_id"]}).get("Item") or {})
    return JSONResponse({"team_id": ident["team_id"], "team_name": team.get("name"),
                         "join_code": code, "join_code_expires_at": expires_at},
                        status_code=201)


@mcp.custom_route("/me", methods=["GET"])
async def me(request: Request) -> JSONResponse:
    """Authed. Returns the caller's identity and team name (used by `raindrop doctor`)."""
    ident = request.state.raindrop
    team = await anyio.to_thread.run_sync(
        lambda: teams.get_item(Key={"team_id": ident["team_id"]}).get("Item") or {})
    return JSONResponse({"team_id": ident["team_id"], "team_name": team.get("name"),
                         "agent_id": ident["agent_id"], "display_name": ident["display_name"]})


def list_team_members(team_id: str) -> list[dict]:
    """Active members of a team, oldest first. Shared by GET /teammates and list_teammates."""
    items, kwargs = [], {"IndexName": "by_team",
                         "KeyConditionExpression": Key("team_id").eq(team_id)}
    while True:
        page = agents.query(**kwargs)
        items += page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    members = {}
    for it in items:
        if it.get("status") == "active":
            members[it["agent_id"]] = {
                "agent_id": it["agent_id"],
                "display_name": it.get("display_name", ""),
                "focus": it.get("focus", ""),
                "joined_at": int(it.get("created_at", 0)),
            }
    return sorted(members.values(), key=lambda m: m["joined_at"])


def team_lead(team_id: str, members: list[dict]) -> str | None:
    """The recorded lead if still active; otherwise the earliest-joined active member.
    Covers teams created before leads existed and leads who have left."""
    active = {m["agent_id"] for m in members}
    team = teams.get_item(Key={"team_id": team_id}).get("Item") or {}
    lead = team.get("lead_agent_id")
    if lead in active:
        return lead
    return members[0]["agent_id"] if members else None


@mcp.custom_route("/teammates", methods=["GET"])
async def teammates(request: Request) -> JSONResponse:
    """Authed. Lists active members of the caller's team, flagging you and the lead."""
    ident = request.state.raindrop

    def work():
        members = list_team_members(ident["team_id"])
        lead = team_lead(ident["team_id"], members)
        for m in members:
            m["you"] = m["agent_id"] == ident["agent_id"]
            m["lead"] = m["agent_id"] == lead
        return members

    members = await anyio.to_thread.run_sync(work)
    return JSONResponse({"team_id": ident["team_id"], "members": members})


@mcp.custom_route("/remove", methods=["POST"])
async def remove_member(request: Request) -> JSONResponse:
    """Authed, lead only. Body: {agent_id}. Revokes every active key of that teammate.
    Their already-shared context stays. Use /leave to remove yourself."""
    ident = request.state.raindrop
    try:
        body = await request.json()
    except Exception:
        return error(400, "bad_request", "Body must be JSON.")
    target = body.get("agent_id") if isinstance(body, dict) else None
    if not isinstance(target, str) or not target:
        return error(400, "bad_request", "`agent_id` is required.")
    if target == ident["agent_id"]:
        return error(400, "bad_request", "You can't remove yourself; use `raindrop leave`.")

    def work():
        members = list_team_members(ident["team_id"])
        if team_lead(ident["team_id"], members) != ident["agent_id"]:
            return error(403, "not_lead", "Only the team lead can remove teammates.")
        rows = agents.query(IndexName="by_team",
                            KeyConditionExpression=Key("team_id").eq(ident["team_id"])).get("Items", [])
        hits = [r for r in rows if r["agent_id"] == target and r.get("status") == "active"]
        if not hits:
            return error(404, "not_found", "No active teammate with that agent_id.")
        for r in hits:
            _deactivate(r["key_hash"], "removed")
        return JSONResponse({"removed": target, "display_name": hits[0].get("display_name")})

    return await anyio.to_thread.run_sync(work)


def _code_id(code_hash: str) -> str:
    return code_hash[:10]


@mcp.custom_route("/codes", methods=["GET"])
async def list_codes(request: Request) -> JSONResponse:
    """Authed. Active (unexpired) join codes for the caller's team. Codes are stored
    hashed, so each is identified by a short id rather than shown in full."""
    ident = request.state.raindrop

    def work():
        now = int(time.time())
        rows = join_codes.query(IndexName="by_team",
                                KeyConditionExpression=Key("team_id").eq(ident["team_id"])).get("Items", [])
        names = {m["agent_id"]: m["display_name"] for m in list_team_members(ident["team_id"])}
        codes = [{
            "id": _code_id(r["code_hash"]),
            "created_by": names.get(r.get("created_by"), r.get("created_by") or "team creator"),
            "uses": int(r.get("uses", 0)),
            "expires_at": int(r["expires_at"]),
        } for r in rows if int(r["expires_at"]) > now]
        return sorted(codes, key=lambda c: c["expires_at"], reverse=True)

    return JSONResponse({"team_id": ident["team_id"], "codes": await anyio.to_thread.run_sync(work)})


@mcp.custom_route("/codes/revoke", methods=["POST"])
async def revoke_code(request: Request) -> JSONResponse:
    """Authed. Body: {code} or {id}. The team lead, or whoever created the code, can revoke it."""
    ident = request.state.raindrop
    try:
        body = await request.json()
    except Exception:
        return error(400, "bad_request", "Body must be JSON.")
    body = body if isinstance(body, dict) else {}

    def work():
        if body.get("code"):
            item = join_codes.get_item(
                Key={"code_hash": sha256(normalize_code(str(body["code"])))}).get("Item")
        elif body.get("id"):
            rows = join_codes.query(IndexName="by_team",
                                    KeyConditionExpression=Key("team_id").eq(ident["team_id"])).get("Items", [])
            item = next((r for r in rows if _code_id(r["code_hash"]) == str(body["id"]).lower()), None)
        else:
            return error(400, "bad_request", "Provide `code` or `id`.")
        if not item or item["team_id"] != ident["team_id"]:
            return error(404, "not_found", "No such join code on your team.")
        members = list_team_members(ident["team_id"])
        allowed = ident["agent_id"] in (item.get("created_by"), team_lead(ident["team_id"], members))
        if not allowed:
            return error(403, "forbidden", "Only the team lead or the code's creator can revoke it.")
        join_codes.delete_item(Key={"code_hash": item["code_hash"]})
        return JSONResponse({"revoked": _code_id(item["code_hash"])})

    return await anyio.to_thread.run_sync(work)


def _deactivate(key_hash: str, status: str) -> None:
    agents.update_item(Key={"key_hash": key_hash},
                       UpdateExpression="SET #s = :s, deactivated_at = :t",
                       ExpressionAttributeNames={"#s": "status"},
                       ExpressionAttributeValues={":s": status, ":t": int(time.time())})
    _auth_cache.pop(key_hash, None)  # this container stops accepting it immediately


@mcp.custom_route("/rotate-key", methods=["POST"])
async def rotate_key(request: Request) -> JSONResponse:
    """Authed. Issues a new key for the same agent and revokes the current one.
    Note: other warm Lambda containers may accept the old key for up to the auth cache TTL."""
    old_hash = request.state.raindrop_key_hash

    def work():
        old = agents.get_item(Key={"key_hash": old_hash}).get("Item")
        if not old or old.get("status") != "active":
            return None
        api_key = secrets.token_urlsafe(32)
        agents.put_item(Item={**old, "key_hash": sha256(api_key), "status": "active",
                              "created_at": int(time.time()), "rotated_from_at": int(time.time())})
        _deactivate(old_hash, "revoked")
        return {"agent_id": old["agent_id"], "team_id": old["team_id"], "api_key": api_key}

    result = await anyio.to_thread.run_sync(work)
    if not result:
        return error(401, "unauthorized", "Key is no longer active.")
    return JSONResponse(result, status_code=201)


@mcp.custom_route("/leave", methods=["POST"])
async def leave(request: Request) -> JSONResponse:
    """Authed. Deactivates the caller's key; they disappear from teammates.
    Their already-shared context stays (deleting it is a separate feature)."""
    ident = request.state.raindrop
    await anyio.to_thread.run_sync(lambda: _deactivate(request.state.raindrop_key_hash, "left"))
    return JSONResponse({"left": True, "team_id": ident["team_id"], "agent_id": ident["agent_id"]})


MAX_EXCHANGES_PER_PUSH = 50
MAX_PUSH_BYTES = 5_000_000
_SAFE_SESSION = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _validate_exchange(ex) -> str | None:
    """Returns an error message, or None if the exchange matches ExchangeWire."""
    if not isinstance(ex, dict):
        return "each exchange must be an object"
    sid = ex.get("session_id")
    if not isinstance(sid, str) or not _SAFE_SESSION.match(sid) or ".." in sid:
        return "session_id must be 1-128 chars of [A-Za-z0-9._:-]"
    if not isinstance(ex.get("idx"), int) or isinstance(ex.get("idx"), bool) or ex["idx"] < 0:
        return "idx must be a non-negative int"
    if not isinstance(ex.get("ts"), int) or isinstance(ex.get("ts"), bool):
        return "ts must be an int (epoch seconds)"
    for f in ("user_text", "assistant_text"):
        if not isinstance(ex.get(f), str):
            return f"{f} must be a string"
    for f in ("tool_summaries", "files_touched"):
        v = ex.get(f, [])
        if not isinstance(v, list) or not all(isinstance(s, str) for s in v):
            return f"{f} must be a list of strings"
    return None


@mcp.custom_route("/messages", methods=["POST"])
async def push_messages(request: Request) -> JSONResponse:
    """Sync endpoint for the Stop hook. Body matches PushMessagesRequest:
    {"exchanges": [ExchangeWire, ...]} (max 50). Returns {"accepted", "ids"}.
    messages.ingest_exchanges is the only writer: it stamps each validated
    exchange with the caller's identity, writes the resulting document to S3,
    and embeds it into S3 Vectors. Idempotent: the S3 key and vector id derive
    from session_id + idx, so retries overwrite."""
    raw = await request.body()
    if len(raw) > MAX_PUSH_BYTES:
        return error(413, "too_large", f"Body exceeds {MAX_PUSH_BYTES} bytes; send smaller batches.")
    try:
        body = json.loads(raw)
    except Exception:
        return error(400, "bad_request", "Body must be JSON.")
    exchanges = body.get("exchanges") if isinstance(body, dict) else None
    if not isinstance(exchanges, list) or not exchanges:
        return error(400, "bad_request", "`exchanges` must be a non-empty list.")
    if len(exchanges) > MAX_EXCHANGES_PER_PUSH:
        return error(400, "bad_request", f"Max {MAX_EXCHANGES_PER_PUSH} exchanges per request.")
    for i, ex in enumerate(exchanges):
        msg = _validate_exchange(ex)
        if msg:
            return error(400, "bad_request", f"exchanges[{i}]: {msg}")

    ident = request.state.raindrop  # set by AuthMiddleware; never trust the body for identity

    try:
        result = await anyio.to_thread.run_sync(
            lambda: messages.ingest_exchanges(ident, exchanges))
    except Exception:
        return error(500, "storage_failed", "Could not store exchanges; retry the same batch.")
    if not result.get("accepted"):
        # ingest_exchanges skips exchanges whose S3 write failed; if none made it, tell the client to retry
        return error(500, "storage_failed", "Could not store exchanges; retry the same batch.")
    return JSONResponse(result)


# ---------- ASGI app ----------
# Stateless + JSON responses: required for Lambda (no sticky sessions, no SSE).
app = mcp.http_app(
    path="/mcp",
    stateless_http=True,
    json_response=True,
    middleware=[Middleware(AuthMiddleware)],
)
