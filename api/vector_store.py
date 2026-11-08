"""Thin wrapper around AWS S3 Vectors. This is the ONLY module allowed to call
the s3vectors client directly -- isolating it here means:
  1. The multi-tenancy boundary (every query is scoped to team_id) has exactly
     one enforcement point.
  2. If AWS's exact field/param names ever shift, only this file changes.
"""

import logging
from typing import Any, NamedTuple, Sequence

import boto3
from botocore.config import Config

from config import AWS_REGION, MAX_RETRY_ATTEMPTS, VECTOR_BUCKET_NAME, VECTOR_INDEX_NAME

logger = logging.getLogger(__name__)

_s3vectors = boto3.client(
    "s3vectors",
    region_name=AWS_REGION,
    config=Config(retries={"max_attempts": MAX_RETRY_ATTEMPTS, "mode": "adaptive"}),
)

# S3 Vectors caps a single put_vectors call's batch size; irrelevant at our
# <=50-exchanges-per-sync scale today, but a future bulk-reindex job must chunk.
PUT_VECTORS_BATCH_LIMIT = 500


class VectorMatch(NamedTuple):
    vector_id: str
    similarity: float
    metadata: dict[str, Any]


class VectorStoreError(Exception):
    """S3 Vectors call failed after botocore's own retries (throttling, 5xx,
    bad permissions, ...). Callers treat this as retryable."""


class VectorStoreValidationError(VectorStoreError):
    """S3 Vectors rejected the request itself (ValidationException). The same
    input will fail the same way every time, so retrying is pointless."""


# S3 Vectors rejects empty arrays in metadata ("Empty arrays are not allowed in
# metadata"); None/empty values carry no information for search anyway, which
# reads them with .get(key, default).
def _sanitize_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in metadata.items() if v is not None and v != [] and v != {} and v != ""}


def put_vector(vector_id: str, embedding: Sequence[float], metadata: dict[str, Any]) -> None:
    put_vectors_batch([(vector_id, embedding, metadata)])


def put_vectors_batch(items: Sequence[tuple[str, Sequence[float], dict[str, Any]]]) -> None:
    """Upsert vectors. put_vectors overwrites an existing key in place -- there
    is no separate upsert call, so idempotent re-ingestion falls out of using a
    deterministic vector_id (messages.py uses the exchange's session_id:idx).
    """
    if not items:
        return

    for start in range(0, len(items), PUT_VECTORS_BATCH_LIMIT):
        chunk = items[start : start + PUT_VECTORS_BATCH_LIMIT]
        try:
            _s3vectors.put_vectors(
                vectorBucketName=VECTOR_BUCKET_NAME,
                indexName=VECTOR_INDEX_NAME,
                vectors=[
                    {
                        "key": vector_id,
                        "data": {"float32": list(embedding)},
                        "metadata": _sanitize_metadata(metadata),
                    }
                    for vector_id, embedding, metadata in chunk
                ],
            )
        except _s3vectors.exceptions.ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ValidationException":
                raise VectorStoreValidationError(f"put_vectors rejected input: {exc}") from exc
            raise VectorStoreError(f"put_vectors failed: {exc}") from exc


# GetVectors caps the number of keys per call.
GET_VECTORS_BATCH_LIMIT = 100


def existing_vector_keys(keys: Sequence[str]) -> set[str]:
    """Which of `keys` currently exist in the index (metadata/data not fetched).
    Used by scripts/backfill_vectors.py to find exchanges that are in S3 but
    never made it into the index.
    """
    found: set[str] = set()
    for start in range(0, len(keys), GET_VECTORS_BATCH_LIMIT):
        chunk = list(keys[start : start + GET_VECTORS_BATCH_LIMIT])
        try:
            response = _s3vectors.get_vectors(
                vectorBucketName=VECTOR_BUCKET_NAME,
                indexName=VECTOR_INDEX_NAME,
                keys=chunk,
                returnData=False,
                returnMetadata=False,
            )
        except _s3vectors.exceptions.ClientError as exc:
            raise VectorStoreError(f"get_vectors failed: {exc}") from exc
        found.update(item["key"] for item in response.get("vectors", []))
    return found


def fetch_vectors(keys: Sequence[str]) -> dict[str, tuple[list[float], dict[str, Any]]]:
    """key -> (embedding, metadata) for the keys that exist. Lets a caller rewrite a
    vector's metadata (scripts/scrub_paths.py fixes `preview`) by putting the same
    embedding back, instead of paying for a new Titan call."""
    found: dict[str, tuple[list[float], dict[str, Any]]] = {}
    for start in range(0, len(keys), GET_VECTORS_BATCH_LIMIT):
        chunk = list(keys[start : start + GET_VECTORS_BATCH_LIMIT])
        try:
            response = _s3vectors.get_vectors(
                vectorBucketName=VECTOR_BUCKET_NAME,
                indexName=VECTOR_INDEX_NAME,
                keys=chunk,
                returnData=True,
                returnMetadata=True,
            )
        except _s3vectors.exceptions.ClientError as exc:
            raise VectorStoreError(f"get_vectors failed: {exc}") from exc
        for item in response.get("vectors", []):
            found[item["key"]] = (list(item.get("data", {}).get("float32", [])), item.get("metadata") or {})
    return found


def build_query_filter(
    team_id: str, agent_ids: Sequence[str] | None = None, since_ts: int | None = None
) -> dict[str, Any]:
    """The metadata filter for a query. `team_id` is ALWAYS the first condition: it is the
    multi-tenancy boundary, and the optional narrowing below can only ever shrink the result
    set inside that team, never widen it. Only filterable metadata can be used here (team_id,
    agent_id, session_id, idx, ts, s3_key, display_name); files_touched and preview are not.
    """
    conditions: list[dict[str, Any]] = [{"team_id": team_id}]
    if agent_ids:
        ids = list(agent_ids)
        conditions.append({"agent_id": ids[0]} if len(ids) == 1 else {"agent_id": {"$in": ids}})
    if since_ts is not None:
        conditions.append({"ts": {"$gte": int(since_ts)}})
    return conditions[0] if len(conditions) == 1 else {"$and": conditions}


def query_vectors(
    query_embedding: Sequence[float],
    team_id: str,
    top_k: int,
    *,
    agent_ids: Sequence[str] | None = None,
    since_ts: int | None = None,
) -> list[VectorMatch]:
    """Nearest-neighbor search, always scoped to team_id. No other function in
    this codebase may call s3vectors.query_vectors directly -- that's what
    makes this the single enforcement point of the multi-tenancy boundary.
    `agent_ids` (only these agents) and `since_ts` (epoch seconds, newer than or equal to)
    narrow the search further, inside the team.

    Requesting returnMetadata/returnDistance requires BOTH the
    s3vectors:QueryVectors and s3vectors:GetVectors IAM actions on the caller
    (AccessDeniedException otherwise) -- grant both on the Lambda execution role.
    """
    try:
        response = _s3vectors.query_vectors(
            vectorBucketName=VECTOR_BUCKET_NAME,
            indexName=VECTOR_INDEX_NAME,
            topK=top_k,
            queryVector={"float32": list(query_embedding)},
            filter=build_query_filter(team_id, agent_ids, since_ts),
            returnMetadata=True,
            returnDistance=True,
        )
    except _s3vectors.exceptions.ClientError as exc:
        raise VectorStoreError(f"query_vectors failed: {exc}") from exc

    distance_metric = response.get("distanceMetric", "cosine")
    if distance_metric != "cosine":
        # similarity = 1 - distance only holds for cosine distance on normalized
        # vectors (Titan V2 normalize=true). If the index is ever reconfigured
        # to euclidean, this conversion must change too.
        logger.warning(
            "query_vectors: index uses %s distance; similarity conversion assumes cosine",
            distance_metric,
        )

    matches = []
    for item in response.get("vectors", []):
        distance = item.get("distance")
        similarity = _clip01(1.0 - distance) if distance is not None else 0.0
        matches.append(
            VectorMatch(vector_id=item["key"], similarity=similarity, metadata=item.get("metadata") or {})
        )
    return matches


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, value))
