"""HTTP client for the Raindrop API.

create_team / join are unauthenticated. Authed calls (push_messages, added by
the sync owner) send `Authorization: Bearer <api_key>` from config.
"""
from __future__ import annotations

import httpx

TIMEOUT = httpx.Timeout(20.0)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _post(api_url: str, path: str, body: dict, api_key: str | None = None,
          transport: httpx.BaseTransport | None = None) -> dict:
    return _request("POST", api_url, path, body, api_key, transport)


def _get(api_url: str, path: str, api_key: str | None = None,
         transport: httpx.BaseTransport | None = None) -> dict:
    return _request("GET", api_url, path, None, api_key, transport)


def _request(method: str, api_url: str, path: str, body: dict | None,
             api_key: str | None, transport: httpx.BaseTransport | None) -> dict:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        with httpx.Client(timeout=TIMEOUT, transport=transport) as client:
            r = client.request(method, api_url.rstrip("/") + path, json=body, headers=headers)
    except httpx.HTTPError as e:
        raise ApiError(0, "network_error", f"Could not reach {api_url}: {e}") from e
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code >= 400:
        raise ApiError(r.status_code, data.get("error", "http_error"),
                       data.get("message", f"HTTP {r.status_code}"))
    return data


def create_team(api_url: str, name: str, display_name: str, focus: str, **kw) -> dict:
    """POST /teams. The lead is auto-joined, so the response includes api_key."""
    return _post(api_url, "/teams", {"name": name, "display_name": display_name, "focus": focus}, **kw)


def invite(api_url: str, api_key: str, **kw) -> dict:
    """POST /invite (authed). Returns a fresh join_code for the caller's team."""
    return _post(api_url, "/invite", {}, api_key=api_key, **kw)


def health(api_url: str, **kw) -> dict:
    """GET /health (no auth)."""
    return _get(api_url, "/health", **kw)


def me(api_url: str, api_key: str, **kw) -> dict:
    """GET /me (authed). Identity the server sees for this key."""
    return _get(api_url, "/me", api_key=api_key, **kw)


def teammates(api_url: str, api_key: str, **kw) -> dict:
    """GET /teammates (authed). {"team_id", "members": [{agent_id, display_name, focus, joined_at, you}]}"""
    return _get(api_url, "/teammates", api_key=api_key, **kw)


def rotate_key(api_url: str, api_key: str, **kw) -> dict:
    """POST /rotate-key (authed). Returns the new api_key; the old one is revoked."""
    return _post(api_url, "/rotate-key", {}, api_key=api_key, **kw)


def leave(api_url: str, api_key: str, **kw) -> dict:
    """POST /leave (authed). Deactivates this key."""
    return _post(api_url, "/leave", {}, api_key=api_key, **kw)


def remove(api_url: str, api_key: str, agent_id: str, **kw) -> dict:
    """POST /remove (authed, lead only). Revokes a teammate's keys."""
    return _post(api_url, "/remove", {"agent_id": agent_id}, api_key=api_key, **kw)


def list_codes(api_url: str, api_key: str, **kw) -> dict:
    """GET /codes (authed). Active join codes for your team."""
    return _get(api_url, "/codes", api_key=api_key, **kw)


def revoke_code(api_url: str, api_key: str, code: str | None = None, code_id: str | None = None, **kw) -> dict:
    """POST /codes/revoke (authed). Pass the full code or its short id."""
    body = {"code": code} if code else {"id": code_id}
    return _post(api_url, "/codes/revoke", body, api_key=api_key, **kw)


def join(api_url: str, code: str, display_name: str, focus: str, **kw) -> dict:
    """POST /join. Returns team_id, team_name, agent_id, api_key (shown once)."""
    return _post(api_url, "/join", {"code": code, "display_name": display_name, "focus": focus}, **kw)


def disband(api_url: str, api_key: str, confirm: str, **kw) -> dict:
    """POST /disband (authed, lead only). Deletes the team's shared context and revokes every key."""
    return _post(api_url, "/disband", {"confirm": confirm}, api_key=api_key, **kw)
