import app
from starlette.testclient import TestClient


def client(monkeypatch):
    monkeypatch.setattr(app, "_lookup_agent", lambda h: {
        "key_hash": h, "team_id": "t1", "agent_id": "me", "display_name": "Me"})
    seen = {}

    def fake_recent(team_id, n, exclude_agent_id=None, s3=None):
        seen.update(team_id=team_id, n=n, exclude=exclude_agent_id)
        return [{"agent_id": "bob", "ts": 5, "user_text": "q", "assistant_text": "a", "files_touched": []}]

    monkeypatch.setattr(app.recent, "recent_activity", fake_recent)
    monkeypatch.setattr(app, "list_team_members", lambda t: [
        {"agent_id": "me", "display_name": "Me"}, {"agent_id": "bob", "display_name": "Bob"}])
    return TestClient(app.app), seen


def test_recent_requires_auth(monkeypatch):
    c, _ = client(monkeypatch)
    assert c.get("/recent").status_code == 401


def test_recent_returns_exchanges_and_members_and_clamps_n(monkeypatch):
    c, seen = client(monkeypatch)
    r = c.get("/recent?n=999", headers={"Authorization": "Bearer k"})
    assert r.status_code == 200
    body = r.json()
    assert seen == {"team_id": "t1", "n": app.RECENT_N_MAX, "exclude": None}
    assert body["exchanges"][0]["user_text"] == "q"
    assert [m["you"] for m in body["members"]] == [True, False]
    assert c.get("/recent?n=x", headers={"Authorization": "Bearer k"}).status_code == 400


def test_cors_preflight_is_answered_without_auth(monkeypatch):
    c, _ = client(monkeypatch)
    r = c.options("/recent", headers={"Origin": "http://localhost:5173",
                                      "Access-Control-Request-Method": "GET",
                                      "Access-Control-Request-Headers": "authorization"})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"
