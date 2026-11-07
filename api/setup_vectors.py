"""One-time setup: creates the S3 Vectors bucket + index that messages.py and
search.py assume already exist. template.yaml grants IAM permissions and sets
the VECTOR_BUCKET_NAME/VECTOR_INDEX_NAME env vars, but can't create these
itself (no CloudFormation resource type for S3 Vectors yet) -- run this once
per account/region after `sam deploy`, before the first /messages call.

Usage:  python setup_vectors.py
Idempotent: safe to re-run: a bucket/index that already exists is left as-is.
"""

import boto3

from config import AWS_REGION, EMBED_DIMENSIONS, VECTOR_BUCKET_NAME, VECTOR_INDEX_NAME

_s3vectors = boto3.client("s3vectors", region_name=AWS_REGION)


def _exists(exc: Exception) -> bool:
    return exc.__class__.__name__ in ("ConflictException",)


def main() -> None:
    try:
        _s3vectors.create_vector_bucket(vectorBucketName=VECTOR_BUCKET_NAME)
        print(f"created vector bucket {VECTOR_BUCKET_NAME!r} in {AWS_REGION}")
    except _s3vectors.exceptions.ClientError as exc:
        if not _exists(exc):
            raise
        print(f"vector bucket {VECTOR_BUCKET_NAME!r} already exists, skipping")

    try:
        _s3vectors.create_index(
            vectorBucketName=VECTOR_BUCKET_NAME,
            indexName=VECTOR_INDEX_NAME,
            dataType="float32",
            dimension=EMBED_DIMENSIONS,
            distanceMetric="cosine",  # must match vector_store.py's similarity = 1 - distance assumption
        )
        print(f"created index {VECTOR_INDEX_NAME!r} (dim={EMBED_DIMENSIONS}, cosine) in bucket {VECTOR_BUCKET_NAME!r}")
    except _s3vectors.exceptions.ClientError as exc:
        if not _exists(exc):
            raise
        print(f"index {VECTOR_INDEX_NAME!r} already exists, skipping")


if __name__ == "__main__":
    main()
