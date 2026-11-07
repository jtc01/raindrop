"""Backfill S3 Vectors for exchanges that are in the exchanges bucket but
missing from the index (e.g. the ones whose put_vectors was rejected with
"Empty arrays are not allowed in metadata" before vector_store sanitized
metadata, or any exchange counted accepted while its vector write failed).

Runs locally with your AWS credentials against the production buckets:

    cd api
    python scripts/backfill_vectors.py --dry-run   # count + list missing, write nothing
    python scripts/backfill_vectors.py             # embed + write the missing vectors

Uses the same embed.py / vector_store.py / messages.vector_metadata code
ingestion uses, so a backfilled vector is identical to an ingested one.
Idempotent: put_vectors overwrites by key, and only missing keys are written.
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

# Target the production stack unless the caller overrides via the environment.
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("EXCHANGES_BUCKET", "raindrop-exchanges-677859735674")
os.environ.setdefault("VECTOR_BUCKET_NAME", "raindrop-vectors-677859735674")
os.environ.setdefault("VECTOR_INDEX_NAME", "raindrop-exchanges")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # api/ on the path for config/embed/...

import boto3  # noqa: E402

from config import AWS_REGION, EXCHANGES_BUCKET, VECTOR_BUCKET_NAME, VECTOR_INDEX_NAME  # noqa: E402
from embed import EmbeddingError, build_exchange_embedding_text, embed_text  # noqa: E402
from messages import _vector_id, vector_metadata  # noqa: E402
from vector_store import VectorStoreError, existing_vector_keys, put_vectors_batch  # noqa: E402

# Mirrors messages._exchange_key: teams/{team_id}/agents/{agent_id}/sessions/{session_id}/{idx}.json
_KEY_RE = re.compile(r"^teams/[^/]+/agents/[^/]+/sessions/(?P<session_id>[^/]+)/(?P<idx>\d+)\.json$")

WRITE_CHUNK = 50  # exchanges embedded + written per put_vectors call


def list_exchange_keys(s3) -> dict[str, str]:
    """vector_id -> s3_key for every exchange object under teams/."""
    out: dict[str, str] = {}
    skipped = 0
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=EXCHANGES_BUCKET, Prefix="teams/"):
        for obj in page.get("Contents", []):
            m = _KEY_RE.match(obj["Key"])
            if not m:
                skipped += 1
                continue
            out[_vector_id(m["session_id"], int(m["idx"]))] = obj["Key"]
    if skipped:
        print(f"note: skipped {skipped} object(s) under teams/ that don't look like exchange documents")
    return out


def find_missing(all_ids: dict[str, str]) -> dict[str, str]:
    ids = sorted(all_ids)
    present = existing_vector_keys(ids)
    return {vid: all_ids[vid] for vid in ids if vid not in present}


def backfill(s3, missing: dict[str, str]) -> tuple[int, list[tuple[str, str]]]:
    """Embed + write every missing exchange. Returns (written, [(vector_id, error)])."""
    written = 0
    failures: list[tuple[str, str]] = []
    items = sorted(missing.items())
    for start in range(0, len(items), WRITE_CHUNK):
        chunk = items[start : start + WRITE_CHUNK]
        batch = []
        for vector_id, s3_key in chunk:
            try:
                doc = json.loads(s3.get_object(Bucket=EXCHANGES_BUCKET, Key=s3_key)["Body"].read())
                text = build_exchange_embedding_text(
                    doc["user_text"], doc["assistant_text"], doc.get("files_touched", [])
                )
                embedding = embed_text(text)
                batch.append((vector_id, embedding, vector_metadata(doc, s3_key)))
            except (EmbeddingError, KeyError, ValueError, Exception) as exc:  # noqa: BLE001 -- report and keep going
                failures.append((vector_id, f"{type(exc).__name__}: {exc}"))
        if not batch:
            continue
        try:
            put_vectors_batch(batch)
            written += len(batch)
            print(f"  wrote {written}/{len(items)}", flush=True)
        except VectorStoreError as exc:
            failures.extend((vid, f"put_vectors: {exc}") for vid, _, _ in batch)
    return written, failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="only count and list missing vectors; write nothing")
    args = parser.parse_args()

    print(f"exchanges bucket: s3://{EXCHANGES_BUCKET}/teams/")
    print(f"vector index:     {VECTOR_BUCKET_NAME}/{VECTOR_INDEX_NAME} ({AWS_REGION})")

    s3 = boto3.client("s3", region_name=AWS_REGION)
    t0 = time.time()
    all_ids = list_exchange_keys(s3)
    print(f"exchange objects in S3: {len(all_ids)}")

    missing = find_missing(all_ids)
    print(f"missing from index:     {len(missing)}")
    for vid, key in sorted(missing.items()):
        print(f"  {vid}  <- s3://{EXCHANGES_BUCKET}/{key}")

    if args.dry_run:
        print(f"\n--dry-run: nothing written ({time.time() - t0:.1f}s)")
        return 0
    if not missing:
        print("\nnothing to backfill")
        return 0

    print(f"\nbackfilling {len(missing)} vector(s)...")
    written, failures = backfill(s3, missing)

    print("\nsummary")
    print(f"  exchange objects in S3: {len(all_ids)}")
    print(f"  missing before:         {len(missing)}")
    print(f"  written:                {written}")
    print(f"  failed:                 {len(failures)}")
    for vid, err in failures:
        print(f"    {vid}: {err}")
    print(f"  elapsed:                {time.time() - t0:.1f}s")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
