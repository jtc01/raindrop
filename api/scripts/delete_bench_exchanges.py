"""Delete exchanges that came from bench/run.sh out of team context.

Benchmark sessions synced while sharing was on, so teammates' searches return
dozens of copies of the benchmark questions. An exchange is deleted only if its
user_text contains BOTH a benchmark question (from questions.txt) AND one of the
suffixes run.sh appends to every prompt, so ordinary work is never touched.

    cd api
    python scripts/delete_bench_exchanges.py            # dry run: lists what would go
    python scripts/delete_bench_exchanges.py --apply    # delete vectors, then S3 objects

Options: --questions PATH (default ~/raindrop-bench/questions.txt), --team TEAM_ID.
Idempotent: a second run finds nothing.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("EXCHANGES_BUCKET", "raindrop-exchanges-677859735674")
os.environ.setdefault("VECTOR_BUCKET_NAME", "raindrop-vectors-677859735674")
os.environ.setdefault("VECTOR_INDEX_NAME", "raindrop-exchanges")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3  # noqa: E402

from config import AWS_REGION, EXCHANGES_BUCKET  # noqa: E402
from vector_store import delete_vectors  # noqa: E402

# Every prompt suffix bench/run.sh has ever used (see `git log -p bench/run.sh`).
BENCH_SUFFIXES = ("Answer only this one question, concisely.", "Answer concisely.")
_KEY_RE = re.compile(r"^teams/(?P<team>[^/]+)/agents/[^/]+/sessions/(?P<session>[^/]+)/(?P<idx>\d+)\.json$")


def is_bench(user_text: str, questions: list[str]) -> bool:
    text = " ".join((user_text or "").split())
    return any(s in text for s in BENCH_SUFFIXES) and any(q in text for q in questions)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Actually delete (default: dry run).")
    ap.add_argument("--questions", default=str(Path.home() / "raindrop-bench" / "questions.txt"))
    ap.add_argument("--team", help="Only this team_id.")
    args = ap.parse_args()

    questions = [" ".join(l.split()) for l in Path(args.questions).read_text().splitlines() if l.strip()]
    if not questions:
        sys.exit(f"No questions in {args.questions}")

    s3 = boto3.client("s3", region_name=AWS_REGION)
    prefix = f"teams/{args.team}/" if args.team else "teams/"
    hits, scanned = [], 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=EXCHANGES_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            m = _KEY_RE.match(obj["Key"])
            if not m:
                continue
            scanned += 1
            ex = json.loads(s3.get_object(Bucket=EXCHANGES_BUCKET, Key=obj["Key"])["Body"].read())
            if is_bench(ex.get("user_text", ""), questions):
                hits.append((obj["Key"], f"{m['session']}:{m['idx']}", ex.get("user_text", "")))

    print(f"Scanned {scanned} exchanges; {len(hits)} are benchmark runs.")
    for key, vid, text in hits[:10]:
        print(f"  {vid:48} {' '.join(text.split())[:90]}")
    if len(hits) > 10:
        print(f"  ... and {len(hits) - 10} more")
    if not args.apply:
        print("Dry run. Re-run with --apply to delete." if hits else "Nothing to delete.")
        return

    delete_vectors([vid for _, vid, _ in hits])  # vectors first, so a retry can still find them
    keys = [k for k, _, _ in hits]
    for i in range(0, len(keys), 1000):
        s3.delete_objects(Bucket=EXCHANGES_BUCKET,
                          Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]], "Quiet": True})
    print(f"Deleted {len(hits)} benchmark exchanges (vectors + S3 objects).")


if __name__ == "__main__":
    main()
