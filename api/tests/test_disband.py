"""POST /disband against in-memory fakes of DynamoDB, S3 and S3 Vectors."""
import pytest
from starlette.testclient import TestClient

import app as api
import vector_store


class FakeTable:
    def __init__(self, pk, rows=()):
        self.pk, self.rows = pk, {r[pk]: dict(r) for r in rows}

    def get_item(self, Key):
        r = self.rows.get(Key[self.pk])
        return {"Item": dict(r)} if r else {}

    def query(self, IndexName, KeyConditionExpression, **_):
        team = KeyConditionExpression.get_expression()["values"][1]
        return {"Items": [dict(r) for r in self.rows.values() if r.get("team_id") == team]}

    def update_item(self, Key, ExpressionAttributeValues, **_):
        r = self.rows[Key[self.pk]]
        r["status"] = ExpressionAttributeValues[":s"]

    def delete_item(self, Key):
        self.rows.pop(Key[self.pk], None)


class FakeS3:
    def __init__(self, keys):
        self.keys = set(keys)

    def get_paginator(self, _):
        s3 = self

        class P:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k} for k in sorted(s3.keys) if k.startswith(Prefix)]}
        return P()

    def delete_objects(self, Bucket, Delete):
        for o in Delete["Objects"]:
            self.keys.discard(o["Key"])


@pytest.fixture
def world(monkeypatch):
    agents = FakeTable("key_hash", [
        {"key_hash": api.sha256("lead-key"), "agent_id": "a_lead", "team_id": "T", "display_name": "Lead", "status": "active", "created_at": 1},
        {"key_hash": api.sha256("mate-key"), "agent_id": "a_mate", "team_id": "T", "display_name": "Mate", "status": "active", "created_at": 2},
        {"key_hash": api.sha256("other-key"), "agent_id": "a_x", "team_id": "OTHER", "display_name": "X", "status": "active", "created_at": 1},
    ])
    teams = FakeTable("team_id", [{"team_id": "T", "name": "Rain", "lead_agent_id": "a_lead"},
                                  {"team_id": "OTHER", "name": "Other"}])
    codes = FakeTable("code_hash", [{"code_hash": "c1", "team_id": "T"}, {"code_hash": "c2", "team_id": "OTHER"}])
    s3 = FakeS3(["teams/T/agents/a_lead/sessions/s1/0.json", "teams/T/agents/a_mate/sessions/s2/3.json",
                 "teams/OTHER/agents/a_x/sessions/s9/0.json"])
    deleted = []
    for name, val in [("agents", agents), ("teams", teams), ("join_codes", codes), ("_s3", s3)]:
        monkeypatch.setattr(api, name, val)
    monkeypatch.setattr(vector_store, "delete_vectors", lambda keys: deleted.extend(keys))
    api._auth_cache.clear()
    return dict(agents=agents, teams=teams, codes=codes, s3=s3, vectors=deleted,
                client=TestClient(api.app))


def call(w, key, confirm):
    return w["client"].post("/disband", json={"confirm": confirm}, headers={"Authorization": f"Bearer {key}"})


def test_lead_disbands_and_only_their_team_is_touched(world):
    r = call(world, "lead-key", "Rain")
    assert r.status_code == 200, r.text
    assert r.json()["keys_revoked"] == 2 and r.json()["exchanges_deleted"] == 2
    assert sorted(world["vectors"]) == ["s1:0", "s2:3"]
    assert world["s3"].keys == {"teams/OTHER/agents/a_x/sessions/s9/0.json"}
    assert set(world["codes"].rows) == {"c2"}
    assert world["teams"].rows["T"]["status"] == "disbanded"
    status = {r["agent_id"]: r["status"] for r in world["agents"].rows.values()}
    assert status == {"a_lead": "disbanded", "a_mate": "disbanded", "a_x": "active"}
    api._auth_cache.clear()
    assert call(world, "mate-key", "Rain").status_code == 401  # keys really dead


def test_non_lead_is_refused(world):
    assert call(world, "mate-key", "Rain").status_code == 403
    assert len(world["s3"].keys) == 3 and not world["vectors"]


def test_wrong_confirm_changes_nothing(world):
    r = call(world, "lead-key", "rain")
    assert r.status_code == 400 and r.json()["error"] == "confirm_mismatch"
    assert len(world["s3"].keys) == 3 and set(world["codes"].rows) == {"c1", "c2"}
