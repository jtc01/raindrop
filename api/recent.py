"""Recent activity: the n newest exchanges on a team, newest first.
Called from app.py's get_recent_activity MCP tool.

No vector search is involved, so there is no embedding call. Exchanges are
listed straight from S3 (teams/{team}/agents/{agent}/sessions/{session}/{idx}.json),
ordered by the object's LastModified time, then hydrated and ordered by the
exchange's own `ts`.

Cost note: S3 cannot sort or filter a listing, so each call lists every object
under the team's agent prefixes (1000 keys per request, capped by
MAX_KEYS_SCANNED_PER_AGENT). That is fine for a team-sized demo. If a team
accumulates many tens of thousands of exchanges, replace this with a
DynamoDB (team_id, ts) index written at ingest time.
"""

import heapq
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

import boto3
from botocore.config import Config

from config import AWS_REGION, EXCHANGES_BUCKET, MAX_RETRY_ATTEMPTS

logger = logging.getLogger(__name__)

_s3 = boto3.client(
    "s3",
    region_name=AWS_REGION,
    config=Config(retries={"max_attempts": MAX_RETRY_ATTEMPTS, "mode": "adaptive"}),
)

MAX_KEYS_SCANNED_PER_AGENT = 20_000
_MAX_HYDRATION_WORKERS = 10


def _agent_prefixes(s3, team_id: str) -> list[str]:
    """['teams/T/agents/A1/', ...] -- one per agent that has ever pushed."""
    prefixes: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(
        Bucket=EXCHANGES_BUCKET, Prefix=f"teams/{team_id}/agents/", Delimiter="/"
    ):
        prefixes.extend(p["Prefix"] for p in page.get("CommonPrefixes", []))
    return prefixes


def _newest_keys(s3, prefix: str, n: int) -> list[tuple[Any, str]]:
    """The n most recently written (LastModified, Key) pairs under `prefix`."""
    scanned = 0
    best: list[tuple[Any, str]] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=EXCHANGES_BUCKET, Prefix=prefix):
        objects = [
            (o["LastModified"], o["Key"])
            for o in page.get("Contents", [])
            if o["Key"].endswith(".json")
        ]
        best = heapq.nlargest(n, best + objects)
        scanned += len(page.get("Contents", []))
        if scanned >= MAX_KEYS_SCANNED_PER_AGENT:
            logger.warning("recent: stopped scanning %s after %d keys", prefix, scanned)
            break
    return best


def _hydrate(s3, key: str) -> Optional[dict[str, Any]]:
    try:
        exchange = json.loads(s3.get_object(Bucket=EXCHANGES_BUCKET, Key=key)["Body"].read())
    except Exception:
        logger.exception("recent: failed to read s3://%s/%s, dropping", EXCHANGES_BUCKET, key)
        return None
    # teams/{team}/agents/{agent}/sessions/{session}/{idx}.json
    parts = key.split("/")
    session_id, idx = exchange.get("session_id"), exchange.get("idx")
    return {
        # The exchange id (session_id:idx), as stored at ingest; rebuilt for older documents.
        "id": exchange.get("id") or (f"{session_id}:{idx}" if session_id is not None and idx is not None else None),
        "agent_id": parts[3] if len(parts) > 3 else None,
        "session_id": exchange.get("session_id"),
        "idx": exchange.get("idx"),
        "ts": exchange.get("ts"),
        "files_touched": exchange.get("files_touched", []),
        "user_text": exchange.get("user_text"),
        "assistant_text": exchange.get("assistant_text"),
    }


def recent_activity(
    team_id: str, n: int, exclude_agent_id: Optional[str] = None, s3=None
) -> list[dict[str, Any]]:
    """The n newest exchanges for team_id, newest first (by exchange `ts`).
    `exclude_agent_id` leaves out one agent (the caller, to show only teammates).
    The team prefix is part of every listing, so other teams' data is never read."""
    s3 = s3 or _s3
    candidates: list[tuple[Any, str]] = []
    for prefix in _agent_prefixes(s3, team_id):
        if exclude_agent_id and prefix.rstrip("/").endswith("/" + exclude_agent_id):
            continue
        candidates.extend(_newest_keys(s3, prefix, n))

    newest = heapq.nlargest(n, candidates)
    if not newest:
        return []
    with ThreadPoolExecutor(max_workers=min(len(newest), _MAX_HYDRATION_WORKERS)) as pool:
        hydrated = list(pool.map(lambda c: _hydrate(s3, c[1]), newest))
    results = [h for h in hydrated if h is not None]
    results.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    return results
