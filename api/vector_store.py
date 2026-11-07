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
    """Non-retryable S3 Vectors failure (bad permissions, validation, ...)."""


def put_vector(vector_id: str, embedding: Sequence[float], metadata: dict[str, Any]) -> None:
    put_vectors_batch([(vector_id, embedding, metadata)])


def put_vectors_batch(items: Sequence[tuple[str, Sequence[float], dict[str, Any]]]) -> None:
    """Upsert vectors. put_vectors overwrites an existing key in place -- there
    is no separate upsert call, so idempotent re-ingestion falls out of using a
    deterministic vector_id (api/messages.py uses the exchange's session_id:idx).
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
                        "metadata": metadata,
                    }
                    for vector_id, embedding, metadata in chunk
                ],
            )
        except _s3vectors.exceptions.ClientError as exc:
            raise VectorStoreError(f"put_vectors failed: {exc}") from exc


def query_vectors(query_embedding: Sequence[float], team_id: str, top_k: int) -> list[VectorMatch]:
    """Nearest-neighbor search, always scoped to team_id. No other function in
    this codebase may call s3vectors.query_vectors directly -- that's what
    makes this the single enforcement point of the multi-tenancy boundary.

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
            filter={"team_id": team_id},
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
