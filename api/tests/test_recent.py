import io
import json
from datetime import datetime, timedelta, timezone

import recent

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FakeS3:
    """Just enough of the S3 client for recent.py: paginated listing + get_object."""

    def __init__(self, page_size=1000):
        self.objects = {}  # key -> (last_modified, body_dict)
        self.page_size = page_size

    def put(self, team, agent, session, idx, ts, minutes, **kw):
        key = f"teams/{team}/agents/{agent}/sessions/{session}/{idx}.json"
        body = {"session_id": session, "idx": idx, "ts": ts, "user_text": f"q{ts}",
                "assistant_text": f"a{ts}", "files_touched": kw.get("files", [])}
        self.objects[key] = (T0 + timedelta(minutes=minutes), body)

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        fake = self

        class P:
            def paginate(self, Bucket, Prefix, Delimiter=None):
                keys = sorted(k for k in fake.objects if k.startswith(Prefix))
                if Delimiter:
                    prefixes = sorted({k[: k.index(Delimiter, len(Prefix)) + 1] for k in keys})
                    yield {"CommonPrefixes": [{"Prefix": p} for p in prefixes]}
                    return
                for i in range(0, max(len(keys), 1), fake.page_size):
                    yield {"Contents": [{"Key": k, "LastModified": fake.objects[k][0]}
                                        for k in keys[i : i + fake.page_size]]}
        return P()

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(json.dumps(self.objects[Key][1]).encode())}


def test_newest_first_across_agents_and_limited_to_n():
    s3 = FakeS3()
    s3.put("t1", "ann", "s1", 0, ts=100, minutes=1)
    s3.put("t1", "bob", "s2", 0, ts=300, minutes=3)
    s3.put("t1", "ann", "s1", 1, ts=200, minutes=2)
    s3.put("t1", "bob", "s2", 1, ts=400, minutes=4)
    got = recent.recent_activity("t1", 3, s3=s3)
    assert [r["ts"] for r in got] == [400, 300, 200]
    assert [r["agent_id"] for r in got] == ["bob", "bob", "ann"]
    assert got[0]["user_text"] == "q400" and got[0]["session_id"] == "s2"


def test_excludes_the_callers_own_exchanges():
    s3 = FakeS3()
    s3.put("t1", "me", "s1", 0, ts=900, minutes=9)
    s3.put("t1", "bob", "s2", 0, ts=100, minutes=1)
    assert [r["agent_id"] for r in recent.recent_activity("t1", 5, exclude_agent_id="me", s3=s3)] == ["bob"]
    assert len(recent.recent_activity("t1", 5, s3=s3)) == 2


def test_other_teams_are_never_returned():
    s3 = FakeS3()
    s3.put("t1", "ann", "s1", 0, ts=100, minutes=1)
    s3.put("t2", "eve", "sx", 0, ts=999, minutes=9)
    s3.put("t10", "eve", "sy", 0, ts=998, minutes=8)  # "t1" must not match as a string prefix
    assert [r["agent_id"] for r in recent.recent_activity("t1", 10, s3=s3)] == ["ann"]


def test_pagination_and_empty_team():
    s3 = FakeS3(page_size=2)
    for i in range(7):
        s3.put("t1", "ann", "s1", i, ts=i, minutes=i)
    assert [r["ts"] for r in recent.recent_activity("t1", 3, s3=s3)] == [6, 5, 4]
    assert recent.recent_activity("nobody", 5, s3=s3) == []


def test_unreadable_object_is_dropped_not_fatal():
    s3 = FakeS3()
    s3.put("t1", "ann", "s1", 0, ts=1, minutes=1)
    s3.put("t1", "ann", "s1", 1, ts=2, minutes=2)
    bad_key = "teams/t1/agents/ann/sessions/s1/1.json"
    real = s3.get_object
    s3.get_object = lambda Bucket, Key: (_ for _ in ()).throw(OSError("boom")) if Key == bad_key else real(Bucket, Key)
    assert [r["ts"] for r in recent.recent_activity("t1", 5, s3=s3)] == [1]


def test_each_result_carries_the_exchange_id():
    s3 = FakeS3()
    s3.put("t1", "ann", "sess-a", 0, ts=100, minutes=1)
    s3.put("t1", "ann", "sess-a", 7, ts=200, minutes=2)
    got = recent.recent_activity("t1", 5, s3=s3)
    assert [r["id"] for r in got] == ["sess-a:7", "sess-a:0"]  # rebuilt as session_id:idx for older documents

    key = "teams/t1/agents/ann/sessions/sess-b/3.json"       # a document that stores its own id keeps it
    s3.objects[key] = (T0 + timedelta(minutes=9), {"id": "sess-b:3", "session_id": "sess-b", "idx": 3, "ts": 300,
                                                   "user_text": "q", "assistant_text": "a", "files_touched": []})
    assert recent.recent_activity("t1", 1, s3=s3)[0]["id"] == "sess-b:3"
