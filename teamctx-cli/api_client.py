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
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        with httpx.Client(timeout=TIMEOUT, transport=transport) as client:
            r = client.post(api_url.rstrip("/") + path, json=body, headers=headers)
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


def join(api_url: str, code: str, display_name: str, focus: str, **kw) -> dict:
    """POST /join. Returns team_id, team_name, agent_id, api_key (shown once)."""
    return _post(api_url, "/join", {"code": code, "display_name": display_name, "focus": focus}, **kw)
