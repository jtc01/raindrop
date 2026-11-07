"""GitHub Copilot agentStop hook: sync the finished turn to Raindrop.

Copilot (VS Code, the Copilot app and the CLI share one hooks format) runs this
when the agent stops and passes the event as JSON on stdin (sessionId, cwd,
transcriptPath, stopReason, ...). The script reuses the CLI's own pipeline
(`raindrop.sync_cmd.run_sync`): it reads only the new part of the chat
transcript, redacts secrets locally, uploads, and advances its cursor only after
the API confirms.

Copilot parses a hook's stdout as JSON, so stdout stays empty; messages go to
stderr. The script always exits 0 so a sync problem can never disturb the agent.
Failures are appended to ~/.raindrop/sync.log.
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

TOOL = "copilot"


def sync_stop(payload: dict, transport=None) -> int:
    """Sync one agentStop event. Returns the number of exchanges uploaded."""
    return run_sync(payload, TOOL, transport)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("Copilot hook input must be a JSON object.")
        count = sync_stop(payload)
    except (api_client.ApiError, RaindropError, ValueError, OSError) as exc:
        message = getattr(exc, "message", None) or str(exc)  # ValueError covers bad JSON
        _log_failure(f"{TOOL}: {message}")
        print(f"Raindrop sync failed: {message}", file=sys.stderr)
        return 0
    if count:
        print(f"Raindrop synced {count} exchange{'' if count == 1 else 's'}.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
