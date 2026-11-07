"""Claude Code Stop hook: sync the finished turn to Raindrop.

Claude Code runs this after every turn and passes the hook event as JSON on
stdin (session_id, transcript_path, cwd, ...). The script reuses the Raindrop
CLI's own pipeline, so it reads only the new part of the transcript, redacts
secrets locally, uploads in batches, and advances its cursor only after the API
confirms the write.

Output goes to stderr only; Claude Code may add a hook's stdout to the agent's
context.
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
from raindrop.sync_cmd import run_sync  # noqa: E402

TOOL = "claude-code"


def sync_stop(payload: dict, transport=None) -> int:
    """Sync one Stop event. Returns the number of exchanges uploaded."""
    return run_sync(payload, TOOL, transport)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("Claude Code hook input must be a JSON object.")
        count = sync_stop(payload)
    except api_client.ApiError as exc:
        print(f"Raindrop sync failed: {exc.message}", file=sys.stderr)
        return 1
    except (RaindropError, ValueError, OSError) as exc:  # includes JSONDecodeError
        print(f"Raindrop sync failed: {exc}", file=sys.stderr)
        return 1
    print(f"Raindrop synced {count} exchange{'' if count == 1 else 's'}.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
