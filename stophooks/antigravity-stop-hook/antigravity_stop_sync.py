"""Antigravity Stop hook: sync the finished turn to Raindrop.

Antigravity (App, IDE and `agy` CLI) runs this when the agent stops and passes
the hook event as JSON on stdin (conversationId, transcriptPath,
workspacePaths, ...). The script reuses the CLI's own pipeline
(`raindrop.sync_cmd.run_sync`): it reads only the new part of the transcript,
redacts secrets locally, uploads, and advances its cursor only after the API
confirms.

Stop hooks must answer on stdout with a JSON decision, and "continue" would keep
the agent running. This script therefore ALWAYS prints {"decision": "allow"} and
exits 0, success or not. Failures go to stderr and ~/.raindrop/sync.log.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

CLI_DIR = Path(__file__).resolve().parents[2] / "raindrop-cli"
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from raindrop import api_client  # noqa: E402
from raindrop.errors import RaindropError  # noqa: E402
from raindrop.sync_cmd import _log_failure, run_sync  # noqa: E402

TOOL = "antigravity"
ALLOW = json.dumps({"decision": "allow"})


def sync_stop(payload: dict, transport=None) -> int:
    """Sync one Stop event. Returns the number of exchanges uploaded."""
    return run_sync(payload, TOOL, transport)


def main() -> int:
    count = 0
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("Antigravity hook input must be a JSON object.")
        count = sync_stop(payload)
    except (api_client.ApiError, RaindropError, ValueError, OSError) as exc:
        message = getattr(exc, "message", None) or str(exc)  # ValueError covers bad JSON
        _log_failure(f"{TOOL}: {message}")
        print(f"Raindrop sync failed: {message}", file=sys.stderr)
    else:
        if count:
            print(f"Raindrop synced {count} exchange{'' if count == 1 else 's'}.", file=sys.stderr)
    print(ALLOW)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
