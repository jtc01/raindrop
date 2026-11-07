"""Ingestion: POST /messages (push_messages). Writes each exchange to S3 and
indexes it in S3 Vectors so api/search.py can find it later.
"""

import json
import logging
from typing import Any

import boto3
from botocore.config import Config

from config import AWS_REGION, EXCHANGES_BUCKET, MAX_RETRY_ATTEMPTS
from embed import EmbeddingError, build_exchange_embedding_text, embed_text
from vector_store import VectorStoreError, put_vector

logger = logging.getLogger(__name__)

_s3 = boto3.client(
    "s3",
    region_name=AWS_REGION,
    config=Config(retries={"max_attempts": MAX_RETRY_ATTEMPTS, "mode": "adaptive"}),
)

# Matches PushMessagesRequest in raindrop-cli/raindrop/models.py.
MAX_EXCHANGES_PER_REQUEST = 50


def _exchange_key(team_id: str, agent_id: str, session_id: str, idx: int) -> str:
    return f"teams/{team_id}/agents/{agent_id}/sessions/{session_id}/{idx}.json"


def _vector_id(session_id: str, idx: int) -> str:
    # Matches Exchange.id in raindrop-cli/raindrop/models.py -- keep the S3 key, the
    # vector key, and the CLI's own notion of an exchange's identity in sync.
    return f"{session_id}:{idx}"


def ingest_exchanges(team_id: str, agent_id: str, exchanges: list[dict[str, Any]]) -> dict[str, Any]:
    """Core logic (no Lambda/event shapes). `exchanges` is a list of
    ExchangeWire-shaped dicts. Returns a PushMessagesResponse-shaped dict:
    {"accepted": int, "ids": [...]}.

    One exchange failing doesn't abort the batch:
      - S3 write failure -> skip the exchange entirely (nothing to search for
        text that was never durably stored).
      - S3 write succeeds but embedding/vector write fails -> still counted as
        accepted (the data isn't lost; a retried sync of the same batch will
        retry the vector write under the same idempotent vector_id).
    """
    accepted_ids: list[str] = []

    for exchange in exchanges[:MAX_EXCHANGES_PER_REQUEST]:
        session_id = exchange["session_id"]
        idx = exchange["idx"]
        vector_id = _vector_id(session_id, idx)
        s3_key = _exchange_key(team_id, agent_id, session_id, idx)

        try:
            _s3.put_object(
                Bucket=EXCHANGES_BUCKET,
                Key=s3_key,
                Body=json.dumps(exchange).encode("utf-8"),
                ContentType="application/json",
            )
        except Exception:
            logger.exception("ingest_exchanges: S3 write failed for %s, skipping", vector_id)
            continue

        accepted_ids.append(exchange.get("id", vector_id))

        try:
            text = build_exchange_embedding_text(
                exchange["user_text"], exchange["assistant_text"], exchange.get("files_touched", [])
            )
            embedding = embed_text(text)
            put_vector(
                vector_id,
                embedding,
                metadata={
                    "team_id": team_id,
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "idx": idx,
                    "ts": exchange["ts"],
                    "s3_key": s3_key,
                    "files_touched": exchange.get("files_touched", []),
                    "preview": exchange["assistant_text"][:200],
                },
            )
        except (EmbeddingError, VectorStoreError):
            logger.exception(
                "ingest_exchanges: vector_write_failed=true for %s (exchange stored in S3, not yet searchable)",
                vector_id,
            )

    return {"accepted": len(accepted_ids), "ids": accepted_ids}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """API Gateway shim. team_id/agent_id are expected to already be attached
    by an upstream authorizer (event["requestContext"]["authorizer"]) -- this
    Lambda trusts them as pre-validated, per the task's auth boundary (the
    local proxy redacts secrets and authenticates before API Gateway is ever
    called).
    """
    authorizer = event.get("requestContext", {}).get("authorizer", {})
    team_id = authorizer["team_id"]
    agent_id = authorizer["agent_id"]

    body = json.loads(event.get("body") or "{}")
    exchanges = body.get("exchanges", [])

    result = ingest_exchanges(team_id, agent_id, exchanges)
    return {"statusCode": 200, "body": json.dumps(result)}
