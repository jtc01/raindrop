import os

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Bedrock embeddings
EMBED_MODEL_ID = os.environ.get("EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0")
EMBED_DIMENSIONS = int(os.environ.get("EMBED_DIMENSIONS", "512"))
EMBED_NORMALIZE = True  # must match between ingest and search embeddings, not meant to be tuned via env
EMBED_MAX_INPUT_CHARS = int(os.environ.get("EMBED_MAX_INPUT_CHARS", "24000"))  # conservative guard under Titan V2's ~8192 token ceiling

# S3 (raw exchange storage)
EXCHANGES_BUCKET = os.environ["EXCHANGES_BUCKET"]

# S3 Vectors
VECTOR_BUCKET_NAME = os.environ["VECTOR_BUCKET_NAME"]
VECTOR_INDEX_NAME = os.environ.get("VECTOR_INDEX_NAME", "raindrop-exchanges")

# Search tuning
SEARCH_TOP_K_DEFAULT = int(os.environ.get("SEARCH_TOP_K_DEFAULT", "8"))
SEARCH_OVERFETCH_MULTIPLIER = int(os.environ.get("SEARCH_OVERFETCH_MULTIPLIER", "4"))
RECENCY_HALF_LIFE_HOURS = float(os.environ.get("RECENCY_HALF_LIFE_HOURS", "72"))
RERANK_SIMILARITY_WEIGHT = float(os.environ.get("RERANK_SIMILARITY_WEIGHT", "0.7"))  # recency weight = 1 - this

# Retry/backoff shared by the bedrock-runtime and s3vectors clients
MAX_RETRY_ATTEMPTS = int(os.environ.get("MAX_RETRY_ATTEMPTS", "5"))
