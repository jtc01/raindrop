"""Retrieval: embeds the query, finds nearest exchanges scoped to the
caller's team, reranks by similarity + recency, and hydrates full text for
the final result set. Called from app.py's search_team_context MCP tool.
"""

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import boto3
from botocore.config import Config

from config import (
    AWS_REGION,
    EXCHANGES_BUCKET,
    MAX_RETRY_ATTEMPTS,
    RECENCY_HALF_LIFE_HOURS,
    RERANK_SIMILARITY_WEIGHT,
    SEARCH_OVERFETCH_MULTIPLIER,
    SEARCH_TOP_K_DEFAULT,
)
from embed import embed_text
from vector_store import VectorMatch, query_vectors

logger = logging.getLogger(__name__)

_s3 = boto3.client(
    "s3",
    region_name=AWS_REGION,
    config=Config(retries={"max_attempts": MAX_RETRY_ATTEMPTS, "mode": "adaptive"}),
)

_MAX_HYDRATION_WORKERS = 10


def _rerank_score(similarity: float, ts: int, now: int) -> float:
    similarity = max(0.0, min(1.0, similarity))
    age_hours = max(0.0, (now - ts) / 3600)
    recency = 0.5 ** (age_hours / RECENCY_HALF_LIFE_HOURS)
    return RERANK_SIMILARITY_WEIGHT * similarity + (1 - RERANK_SIMILARITY_WEIGHT) * recency


def _hydrate(match: VectorMatch) -> dict[str, Any]:
    """Full text lives in S3; vector metadata carries just enough (including
    display_name) to still return something useful if that fetch fails,
    instead of dropping the result entirely.
    """
    base = {
        "agent_id": match.metadata.get("agent_id"),
        "display_name": match.metadata.get("display_name"),
        "ts": match.metadata.get("ts"),
        "files_touched": match.metadata.get("files_touched", []),
    }
    s3_key = match.metadata.get("s3_key")
    if not s3_key:
        logger.error("search: vector %s has no s3_key in metadata, falling back to vector metadata", match.vector_id)
        return {**base, "user_text": None, "assistant_text": match.metadata.get("preview")}

    try:
        obj = _s3.get_object(Bucket=EXCHANGES_BUCKET, Key=s3_key)
        exchange = json.loads(obj["Body"].read())
    except Exception:
        logger.exception(
            "search: failed to hydrate %s from s3://%s/%s, falling back to vector metadata",
            match.vector_id, EXCHANGES_BUCKET, s3_key,
        )
        return {**base, "user_text": None, "assistant_text": match.metadata.get("preview")}

    return {
        **base,
        "display_name": exchange.get("display_name", base["display_name"]),
        "user_text": exchange.get("user_text"),
        "assistant_text": exchange.get("assistant_text"),
    }


def search(team_id: str, query_text: str, top_k: int = SEARCH_TOP_K_DEFAULT) -> list[dict[str, Any]]:
    """Embed the query, over-fetch nearest vectors scoped to team_id, rerank by
    similarity + recency, keep only the final top_k, then hydrate full text
    for just those from S3 (concurrently -- S3 GetObject latency dominates
    wall time, so parallelizing a handful of small fetches matters).
    """
    embedding = embed_text(query_text)
    candidates = query_vectors(embedding, team_id, top_k * SEARCH_OVERFETCH_MULTIPLIER)
    if not candidates:
        return []

    now = int(time.time())
    scored = sorted(
        candidates,
        key=lambda c: _rerank_score(c.similarity, c.metadata.get("ts", 0), now),
        reverse=True,
    )
    top = scored[:top_k]

    hydrated_by_vector_id: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(len(top), _MAX_HYDRATION_WORKERS)) as pool:
        future_to_match = {pool.submit(_hydrate, match): match for match in top}
        for future in as_completed(future_to_match):
            match = future_to_match[future]
            hydrated = future.result()
            hydrated["score"] = _rerank_score(match.similarity, match.metadata.get("ts", 0), now)
            hydrated_by_vector_id[match.vector_id] = hydrated

    # Hydration completes out of order (concurrent futures); reassemble in rerank order.
    return [hydrated_by_vector_id[m.vector_id] for m in top]
