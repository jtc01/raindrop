"""Ingestion: writes each exchange to S3 and indexes it in S3 Vectors so
search.py can find it later. Called from app.py's POST /messages route.
"""

import json
import logging
import re
import time
from typing import Any

import boto3
from botocore.config import Config

from config import AWS_REGION, EXCHANGES_BUCKET, MAX_RETRY_ATTEMPTS
from embed import EmbeddingError, EmptyTextError, build_exchange_embedding_text, embed_text
from vector_store import VectorStoreError, VectorStoreValidationError, put_vector

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


# Defense in depth behind the client's redaction (raindrop-cli/raindrop/redact.py
# scrubs paths, username and hostname before upload): any home-directory prefix
# that still reaches us -- /Users/<name>/, /home/<name>/, C:\Users\<name>\ with
# either slash, any case -- becomes ~/ so one teammate's username never lands in
# another agent's context. Only the prefix is touched; the rest of the path stays.
# Covers the Git Bash spelling of a Windows home too ("/c/Users/<name>/"). Must not
# match mid-path ("/opt/home/x/", "./home/x/", "~/Users/x/"). The name may contain
# a space or two ("C:\Users\Matt Moyer\").
#
# files_touched is scrubbed as well: the client normalizes it to repo-relative paths
# today, but exchanges uploaded before that carried absolute ones.
_HOME_DIR_PREFIX = re.compile(
    r"""(?<![\w.~\\/-])(?:[A-Za-z]:|[\\/][A-Za-z])?[\\/](?:Users|home)[\\/]"""
    r"""(?:[^\\/\s"'`<>|:*?]+(?: [^\\/\s"'`<>|:*?]+){0,2}(?P<sep>[\\/])"""  # "/Users/x/..." -> "~/..."
    r"""|\w[\w\-]*(?:\.[\w\-]+)*(?![\w\-]))""",  # bare "/Users/x" -> "~" (trailing ";" or "." kept)
    re.IGNORECASE,
)
# Some tools name their own files after the home path with "-" for "/":
# ~/.claude/projects/-Users-matt-Development-raindrop/... The username is the
# segment right after "-Users-" / "-home-" when that starts a word.
_HOME_SLUG_USER = re.compile(r"(?P<prefix>(?<!\w)-(?:Users|home)-)[^\\/\s-]+(?=-)", re.IGNORECASE)
SCRUBBED_TEXT_FIELDS = ("user_text", "assistant_text")
SCRUBBED_LIST_FIELDS = ("tool_summaries", "files_touched")


def scrub_home_paths(text: str) -> str:
    r"""'/Users/matt/Development/x/y.py' -> '~/Development/x/y.py' (also /home/... and C:\Users\...),
    and '-Users-matt-Development-x' -> '-Users-<user>-Development-x'."""
    if not text:
        return text
    text = _HOME_DIR_PREFIX.sub(lambda m: "~/" if m.group("sep") else "~", text)
    return _HOME_SLUG_USER.sub(r"\g<prefix><user>", text)


def scrub_exchange_paths(exchange: dict[str, Any]) -> dict[str, Any]:
    """A copy of `exchange` with scrub_home_paths applied to user_text, assistant_text and
    every tool_summaries and files_touched entry. Shared with scripts/scrub_paths.py so
    stored documents are scrubbed exactly as new ones are."""
    out = dict(exchange)
    for field in SCRUBBED_TEXT_FIELDS:
        if isinstance(out.get(field), str):
            out[field] = scrub_home_paths(out[field])
    for field in SCRUBBED_LIST_FIELDS:
        items = out.get(field)
        if isinstance(items, list):
            out[field] = [scrub_home_paths(s) if isinstance(s, str) else s for s in items]
    return out


def vector_metadata(doc: dict[str, Any], s3_key: str) -> dict[str, Any]:
    """Metadata stored alongside the vector for an exchange document (the same
    dict ingest_exchanges writes to S3). Shared with scripts/backfill_vectors.py
    so backfilled vectors are indistinguishable from ingested ones. Empty values
    (e.g. files_touched: []) are dropped by vector_store before the write.
    """
    return {
        "team_id": doc["team_id"],
        "agent_id": doc["agent_id"],
        "display_name": doc["display_name"],  # shown by search even if the S3 hydration fetch fails
        "session_id": doc["session_id"],
        "idx": doc["idx"],
        "ts": doc["ts"],
        "s3_key": s3_key,
        "files_touched": doc.get("files_touched", []),
        "preview": doc["assistant_text"][:200],
    }


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
      - EXCEPTION: S3 Vectors rejects the input outright (ValidationException,
        raised as VectorStoreValidationError). Resending the same exchange can
        never succeed, so withholding `accepted` would just make the client
        retry the same batch forever and block every exchange behind it. The
        exchange IS durably in S3, so count it accepted, log loudly, and let
        scripts/backfill_vectors.py pick it up once the cause is fixed.
      - Likewise when there is nothing to embed (EmptyTextError: user_text,
        assistant_text and files_touched all empty): no vector can ever exist
        for it, so it is accepted and intentionally never indexed. Other
        EmbeddingErrors (bad Titan response) stay retryable.
    """
    team_id = identity["team_id"]
    agent_id = identity["agent_id"]
    display_name = identity["display_name"]
    received_at = int(time.time())

    accepted_ids: list[str] = []

    for exchange in exchanges[:MAX_EXCHANGES_PER_REQUEST]:
        exchange = scrub_exchange_paths(exchange)
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
            put_vector(vector_id, embedding, metadata=vector_metadata(doc, s3_key))
        except EmptyTextError:
            logger.warning(
                "ingest_exchanges: vector_write_skipped=true reason=empty_text for %s "
                "(nothing to embed; exchange stored in S3 and counted as accepted, not indexed)",
                vector_id,
            )
        except VectorStoreValidationError:
            logger.exception(
                "ingest_exchanges: vector_write_rejected=true for %s "
                "(deterministic ValidationException; exchange stored in S3 and counted as accepted "
                "so the client moves on, but it is NOT searchable until backfilled)",
                vector_id,
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
