"""Shared embedding logic. Both ingestion (messages.py) and retrieval
(search.py) must call embed_text() from here rather than invoking Bedrock
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
    EMBED_CHARS_PER_TOKEN,
    EMBED_MAX_INPUT_CHARS,
    EMBED_MODEL_ID,
    EMBED_NORMALIZE,
    EMBED_TEXT_MAX_TOKENS,
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


class EmptyTextError(EmbeddingError):
    """Nothing to embed (empty user_text + assistant_text + files_touched).
    Deterministic: retrying the same input can never succeed."""


_ELLIPSIS = "…"
_FILES_BUDGET_SHARE = 0.2  # at most this share of the budget goes to the file list
_PROMPT_MIN_SHARE = 0.4  # when both are long, the prompt keeps at least this share of what is left


def _clip(text: str, limit: int) -> str:
    """`text` cut to `limit` characters (including the ellipsis that marks the cut)."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(_ELLIPSIS))].rstrip() + _ELLIPSIS


def _files_line(files_touched: Sequence[str], budget: int) -> str:
    """"Files: a, b, +3 more": whole paths only, as many as fit in `budget` characters."""
    paths = [f for f in files_touched if f]
    if not paths or budget <= len("Files: "):
        return ""
    kept: list[str] = []
    used = len("Files: ")
    for i, path in enumerate(paths):
        extra = len(path) + (2 if kept else 0)  # ", " separator
        more = f", +{len(paths) - i - 1} more" if i < len(paths) - 1 else ""
        if used + extra + len(more) > budget and kept:
            return "Files: " + ", ".join(kept) + f", +{len(paths) - len(kept)} more"
        if used + extra > budget:  # even the first path does not fit whole: clip it
            return "Files: " + _clip(path, budget - len("Files: "))
        kept.append(path)
        used += extra
    return "Files: " + ", ".join(kept)


def build_exchange_embedding_text(
    user_text: str, assistant_text: str, files_touched: Sequence[str] = ()
) -> str:
    """Canonical text embedded for one exchange: the prompt, the final reply and the file
    paths touched, capped at about EMBED_TEXT_MAX_TOKENS tokens. Raw tool output and
    tool_summaries are intentionally excluded.

    Within the cap, short parts are kept whole and a long part is cut with an ellipsis. When
    both the prompt and the reply are long, the prompt keeps at least 40% of the room.
    """
    cap = max(1, EMBED_TEXT_MAX_TOKENS * EMBED_CHARS_PER_TOKEN)
    user, reply = user_text.strip(), assistant_text.strip()
    files = _files_line(files_touched, int(cap * _FILES_BUDGET_SHARE))

    separators = 2 * ((1 if user else 0) + (1 if reply else 0) + (1 if files else 0) - 1)
    room = cap - len(files) - max(0, separators)
    if len(user) + len(reply) > room:
        user_share = min(len(user), max(int(room * _PROMPT_MIN_SHARE), room - len(reply)))
        user = _clip(user, user_share)
        reply = _clip(reply, room - len(user))
    return "\n\n".join(p for p in (user, reply, files) if p)


def embed_text(text: str) -> list[float]:
    """Embed text with Bedrock Titan V2. Returns a 512-dim normalized vector."""
    if not text:
        raise EmptyTextError("cannot embed empty text")

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
