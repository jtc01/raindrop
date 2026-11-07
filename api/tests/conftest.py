import os
import sys
from pathlib import Path

# api/config.py reads these at import time; no AWS calls are made in these tests.
os.environ.setdefault("EXCHANGES_BUCKET", "test-exchanges")
os.environ.setdefault("VECTOR_BUCKET_NAME", "test-vectors")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
