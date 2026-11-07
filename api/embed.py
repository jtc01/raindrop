"""Shared embedding logic. Both ingestion (api/messages.py) and retrieval
(api/search.py) must call embed_text() from here rather than invoking Bedrock
directly, so document and query embeddings always live in the same vector
space.
"""

import json
import logging
from typing import Sequence

import boto3
from botocore.config import Config

from config import (
    AWS_REGION,
    EMBED_DIMENSIONS,
    EMBED_MAX_INPUT_CHARS,
    EMBED_MODEL_ID,
    EMBED_NORMALIZE,
    MAX_RETRY_ATTEMPTS,
)

logger = logging.getLogger(__name__)

_bedrock = boto3.client(
    "bedrock-runtime",
    region_name=AWS_REGION,
    config=Config(retries={"max_attempts": MAX_RETRY_ATTEMPTS, "mode": "adaptive"}),
)


class EmbeddingError(Exception):
    """Raised when Bedrock returns a response we can't use as an embedding."""


def build_exchange_embedding_text(
    user_text: str, assistant_text: str, files_touched: Sequence[str] = ()
) -> str:
    """Canonical text embedded for one exchange: the prompt, the final reply,
    and the file paths touched. Raw tool output / tool_summaries are
    intentionally excluded.
    """
    parts = [user_text.strip(), assistant_text.strip()]
    if files_touched:
        parts.append("Files: " + ", ".join(files_touched))
    return "\n\n".join(p for p in parts if p)


def embed_text(text: str) -> list[float]:
    """Embed text with Bedrock Titan V2. Returns a 512-dim normalized vector."""
    if not text:
        raise EmbeddingError("cannot embed empty text")

    if len(text) > EMBED_MAX_INPUT_CHARS:
        logger.warning(
            "embed_text: truncating input from %d to %d chars", len(text), EMBED_MAX_INPUT_CHARS
        )
        text = text[:EMBED_MAX_INPUT_CHARS]

    body = {
        "inputText": text,
        "dimensions": EMBED_DIMENSIONS,
        "normalize": EMBED_NORMALIZE,
    }

    response = _bedrock.invoke_model(
        modelId=EMBED_MODEL_ID,
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )

    payload = json.loads(response["body"].read())
    embedding = payload.get("embedding")
    if not isinstance(embedding, list) or len(embedding) != EMBED_DIMENSIONS:
        raise EmbeddingError(
            f"unexpected Titan response shape: expected {EMBED_DIMENSIONS}-dim embedding, got {payload!r}"
        )
    return embedding
