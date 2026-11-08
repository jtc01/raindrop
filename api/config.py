import os

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Bedrock embeddings
EMBED_MODEL_ID = os.environ.get("EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0")
EMBED_DIMENSIONS = int(os.environ.get("EMBED_DIMENSIONS", "512"))
EMBED_NORMALIZE = True  # must match between ingest and search embeddings, not meant to be tuned via env
EMBED_MAX_INPUT_CHARS = int(os.environ.get("EMBED_MAX_INPUT_CHARS", "24000"))  # conservative guard under Titan V2's ~8192 token ceiling

# The text embedded for one exchange is capped to a token budget (prompt + final reply + file
# paths). A short, focused text embeds better than a long one, and tool output is never included.
# Tokens are estimated from characters (English text and code average roughly 4 characters/token).
EMBED_TEXT_MAX_TOKENS = int(os.environ.get("EMBED_TEXT_MAX_TOKENS", "500"))
EMBED_CHARS_PER_TOKEN = int(os.environ.get("EMBED_CHARS_PER_TOKEN", "4"))

# Exchanges in one /messages request are embedded concurrently (each Bedrock call mostly waits on
# the network). Keep it small: Bedrock throttles per account and the client already retries.
EMBED_MAX_WORKERS = int(os.environ.get("EMBED_MAX_WORKERS", "4"))

# S3 (raw exchange storage)
EXCHANGES_BUCKET = os.environ["EXCHANGES_BUCKET"]

# S3 Vectors
VECTOR_BUCKET_NAME = os.environ["VECTOR_BUCKET_NAME"]
VECTOR_INDEX_NAME = os.environ.get("VECTOR_INDEX_NAME", "raindrop-exchanges")

# Search tuning
SEARCH_TOP_K_DEFAULT = int(os.environ.get("SEARCH_TOP_K_DEFAULT", "8"))
SEARCH_OVERFETCH_MULTIPLIER = int(os.environ.get("SEARCH_OVERFETCH_MULTIPLIER", "4"))
# With a file filter the vector index cannot help (files_touched is non-filterable), so this many of the
# nearest vectors are fetched and filtered afterwards. 100 is the most S3 Vectors returns for one query.
SEARCH_FILE_FILTER_FETCH = int(os.environ.get("SEARCH_FILE_FILTER_FETCH", "100"))
RECENCY_HALF_LIFE_HOURS = float(os.environ.get("RECENCY_HALF_LIFE_HOURS", "72"))
RERANK_SIMILARITY_WEIGHT = float(os.environ.get("RERANK_SIMILARITY_WEIGHT", "0.7"))  # recency weight = 1 - this

# Retry/backoff shared by the bedrock-runtime and s3vectors clients
MAX_RETRY_ATTEMPTS = int(os.environ.get("MAX_RETRY_ATTEMPTS", "5"))
