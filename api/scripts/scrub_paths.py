"""Scrub home-directory paths out of exchanges already stored in S3.

Exchanges uploaded before the client redacted local paths carry things like
/Users/<name>/... in user_text, assistant_text, tool_summaries and (before the client normalized it)
files_touched. This applies the same scrub ingestion now applies to new exchanges
(messages.scrub_exchange_paths: /Users/<name>/, /home/<name>/, C:\\Users\\<name>\\,
/c/Users/<name>/ -> ~/) to every object under teams/ in the exchanges bucket, rewrites
the ones that changed, and, where the vector's metadata (`preview` = first 200 chars of
assistant_text, `files_touched`) changed, rewrites that metadata by putting the vector
back with its existing embedding (no re-embedding: the scrub only touches path
prefixes, which carry no meaning the embedding needs).

Runs locally with your AWS credentials against the production buckets:

    cd api
    python scripts/scrub_paths.py --dry-run   # counts + a few redacted examples, writes nothing
    python scripts/scrub_paths.py             # rewrite changed objects and vector previews

Idempotent: a second run finds nothing to change.
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("EXCHANGES_BUCKET", "raindrop-exchanges-677859735674")
os.environ.setdefault("VECTOR_BUCKET_NAME", "raindrop-vectors-677859735674")
os.environ.setdefault("VECTOR_INDEX_NAME", "raindrop-exchanges")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # api/ on the path for config/messages/...

import boto3  # noqa: E402

from config import AWS_REGION, EXCHANGES_BUCKET, VECTOR_BUCKET_NAME, VECTOR_INDEX_NAME  # noqa: E402
from messages import _HOME_DIR_PREFIX, _vector_id, scrub_exchange_paths, scrub_home_paths, vector_metadata  # noqa: E402
from vector_store import VectorStoreError, fetch_vectors, put_vectors_batch  # noqa: E402

# Mirrors messages._exchange_key: teams/{team_id}/agents/{agent_id}/sessions/{session_id}/{idx}.json
_KEY_RE = re.compile(r"^teams/[^/]+/agents/[^/]+/sessions/(?P<session_id>[^/]+)/(?P<idx>\d+)\.json$")

TEXT_FIELDS = ("user_text", "assistant_text", "tool_summaries", "files_touched")
MAX_EXAMPLES = 8
EXAMPLE_CONTEXT = 50  # characters of context shown either side of a redacted path


def list_exchange_keys(s3) -> list[str]:
    keys: list[str] = []
    skipped = 0
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=EXCHANGES_BUCKET, Prefix="teams/"):
        for obj in page.get("Contents", []):
            if _KEY_RE.match(obj["Key"]):
                keys.append(obj["Key"])
            else:
                skipped += 1
    if skipped:
        print(f"note: skipped {skipped} object(s) under teams/ that don't look like exchange documents")
    return sorted(keys)


def examples_from(field: str, before: str, after: str) -> list[tuple[str, str]]:
    """(before snippet, after snippet) around each scrubbed prefix in one field."""
    out = []
    for m in _HOME_DIR_PREFIX.finditer(before):
        lo, hi = max(0, m.start() - EXAMPLE_CONTEXT), min(len(before), m.end() + EXAMPLE_CONTEXT)
        snippet = " ".join(before[lo:hi].split())
        out.append((f"{field}: …{snippet}…", f"{field}: …{' '.join(scrub_home_paths(before[lo:hi]).split())}…"))
    return out


def changed_fields(old: dict, new: dict) -> list[str]:
    return [f for f in TEXT_FIELDS if old.get(f) != new.get(f)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="count and show examples; write nothing")
    parser.add_argument("--examples", type=int, default=MAX_EXAMPLES, help="how many redacted examples to print")
    args = parser.parse_args()

    print(f"exchanges bucket: s3://{EXCHANGES_BUCKET}/teams/")
    print(f"vector index:     {VECTOR_BUCKET_NAME}/{VECTOR_INDEX_NAME} ({AWS_REGION})")
    s3 = boto3.client("s3", region_name=AWS_REGION)
    t0 = time.time()

    keys = list_exchange_keys(s3)
    print(f"exchange objects:   {len(keys)}")

    scanned = changed = objects_written = previews_changed = vectors_rewritten = 0
    vectors_missing: list[str] = []
    failures: list[tuple[str, str]] = []
    field_counts = {f: 0 for f in TEXT_FIELDS}
    examples: list[tuple[str, str]] = []
    preview_updates: list[tuple[str, str, dict]] = []  # (vector_id, s3_key, new doc)

    for key in keys:
        try:
            old = json.loads(s3.get_object(Bucket=EXCHANGES_BUCKET, Key=key)["Body"].read())
        except Exception as exc:  # noqa: BLE001 -- report and keep going
            failures.append((key, f"read: {type(exc).__name__}: {exc}"))
            continue
        scanned += 1
        new = scrub_exchange_paths(old)
        fields = changed_fields(old, new)
        if not fields:
            continue
        changed += 1
        for f in fields:
            field_counts[f] += 1
            if len(examples) < args.examples:
                if f in ("tool_summaries", "files_touched"):
                    for b, a in zip(old[f], new[f]):
                        if b != a:
                            examples.extend(examples_from(f, b, a))
                else:
                    examples.extend(examples_from(f, old[f], new[f]))
        if vector_metadata(old, key) != vector_metadata(new, key):  # preview and/or files_touched
            previews_changed += 1
            m = _KEY_RE.match(key)
            preview_updates.append((_vector_id(m["session_id"], int(m["idx"])), key, new))
        if args.dry_run:
            continue
        try:
            s3.put_object(Bucket=EXCHANGES_BUCKET, Key=key, Body=json.dumps(new).encode("utf-8"),
                          ContentType="application/json")
            objects_written += 1
        except Exception as exc:  # noqa: BLE001
            failures.append((key, f"write: {type(exc).__name__}: {exc}"))

    # Vector previews: fetch the existing embedding + metadata, put back with the new metadata.
    if preview_updates:
        try:
            existing = fetch_vectors([vid for vid, _, _ in preview_updates])
        except VectorStoreError as exc:
            existing = {}
            failures.append(("get_vectors", str(exc)))
        batch = []
        for vid, key, doc in preview_updates:
            if vid not in existing:
                vectors_missing.append(vid)  # never indexed; backfill_vectors.py will embed the scrubbed doc
                continue
            embedding, _old_meta = existing[vid]
            batch.append((vid, embedding, vector_metadata(doc, key)))
        if batch and not args.dry_run:
            try:
                put_vectors_batch(batch)
                vectors_rewritten = len(batch)
            except VectorStoreError as exc:
                failures.append(("put_vectors", str(exc)))
        elif args.dry_run:
            vectors_rewritten = len(batch)

    mode = "--dry-run: nothing written" if args.dry_run else "applied"
    print(f"\nsummary ({mode}, {time.time() - t0:.1f}s)")
    print(f"  objects scanned:            {scanned}")
    print(f"  objects with local paths:   {changed}")
    for f in TEXT_FIELDS:
        print(f"    in {f + ':':<17}{field_counts[f]}")
    print(f"  objects {'to rewrite' if args.dry_run else 'rewritten'}:        {changed if args.dry_run else objects_written}")
    print(f"  vector metadata affected:   {previews_changed}")
    print(f"  vectors {'to rewrite' if args.dry_run else 'rewritten'}:        {vectors_rewritten}")
    if vectors_missing:
        print(f"  vectors not in index:       {len(vectors_missing)} (run backfill_vectors.py)")
        for vid in vectors_missing:
            print(f"    {vid}")
    if examples:
        print(f"\nexamples ({min(len(examples), args.examples)} of {len(examples)} found in the first changed objects)")
        for before, after in examples[: args.examples]:
            print(f"  - {before}\n    {after}")
    if failures:
        print(f"\nfailed: {len(failures)}")
        for what, err in failures:
            print(f"  {what}: {err}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
