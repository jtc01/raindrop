"""Ingestion: writes each exchange to S3 and indexes it in S3 Vectors so
search.py can find it later. Called from app.py's POST /messages route.
"""

import json
import logging
import time
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


def ingest_exchanges(identity: dict[str, Any], exchanges: list[dict[str, Any]]) -> dict[str, Any]:
    """Core logic (no Lambda/event shapes). The only writer of exchange
    documents: takes the caller's whole identity (team_id, agent_id,
    display_name -- as set by app.py's AuthMiddleware) plus the validated,
    still-bare exchanges from the request body, stamps each with that
    identity, and writes the resulting document to S3 and S3 Vectors. app.py
    must not build its own copy of this document -- two writers of the same
    S3 key meant whichever ran last won, silently dropping fields like
    display_name depending on write order.

    Returns a PushMessagesResponse-shaped dict: {"accepted": int, "ids": [...]}.

    An exchange only counts as accepted once it's fully searchable (S3 write
    AND embed+vector write both succeeded) -- NOT merely once it's in S3.
    raindrop-cli's sync_cmd.py compares `accepted` to the batch size and only
    advances its cursor on a full match (sync_cmd.py:89-90); if a vector
    write failed but we still reported it accepted, the client would advance
    past it anyway and the exchange would never be resent, leaving its text
    durably stored but permanently unsearchable with no way to notice. Under-
    reporting `accepted` is what makes the client's own retry-on-mismatch
    logic actually retry.

      - S3 write failure -> skip the exchange entirely (not accepted; nothing
        to search for text that was never durably stored).
      - S3 write succeeds but embedding/vector write fails -> also not
        accepted. The S3 object stays (redundant rewrite on retry is
        harmless, same key), and the vector write is retried under the same
        idempotent vector_id next time the client resends this batch.
    """
    team_id = identity["team_id"]
    agent_id = identity["agent_id"]
    display_name = identity["display_name"]
    received_at = int(time.time())

    accepted_ids: list[str] = []

    for exchange in exchanges[:MAX_EXCHANGES_PER_REQUEST]:
        session_id = exchange["session_id"]
        idx = exchange["idx"]
        vector_id = _vector_id(session_id, idx)
        s3_key = _exchange_key(team_id, agent_id, session_id, idx)

        doc = {
            "id": vector_id,  # recomputed server-side, not trusted from the body
            "session_id": session_id,
            "idx": idx,
            "ts": exchange["ts"],
            "user_text": exchange["user_text"],
            "assistant_text": exchange["assistant_text"],
            "tool_summaries": exchange.get("tool_summaries", []),
            "files_touched": exchange.get("files_touched", []),
            "team_id": team_id,
            "agent_id": agent_id,
            "display_name": display_name,
            "received_at": received_at,
        }

        try:
            _s3.put_object(
                Bucket=EXCHANGES_BUCKET,
                Key=s3_key,
                Body=json.dumps(doc).encode("utf-8"),
                ContentType="application/json",
            )
        except Exception:
            logger.exception("ingest_exchanges: S3 write failed for %s, skipping", vector_id)
            continue

        try:
            text = build_exchange_embedding_text(
                doc["user_text"], doc["assistant_text"], doc["files_touched"]
            )
            embedding = embed_text(text)
            put_vector(
                vector_id,
                embedding,
                metadata={
                    "team_id": team_id,
                    "agent_id": agent_id,
                    "display_name": display_name,  # shown by search even if the S3 hydration fetch fails
                    "session_id": session_id,
                    "idx": idx,
                    "ts": doc["ts"],
                    "s3_key": s3_key,
                    "files_touched": doc["files_touched"],
                    "preview": doc["assistant_text"][:200],
                },
            )
        except (EmbeddingError, VectorStoreError):
            logger.exception(
                "ingest_exchanges: vector_write_failed=true for %s "
                "(exchange stored in S3 but NOT counted as accepted, so the client retries it)",
                vector_id,
            )
            continue

        accepted_ids.append(doc["id"])

    return {"accepted": len(accepted_ids), "ids": accepted_ids}
