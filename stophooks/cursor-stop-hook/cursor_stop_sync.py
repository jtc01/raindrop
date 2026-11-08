"""Cursor hook: record each hook event and sync finished turns to Raindrop.

Cursor passes the hook event as JSON on stdin (conversation_id, hook_event_name,
workspace_roots, plus prompt / text / tool_name / tool_input depending on the event).
Four Cursor hooks run this one script (see the README): beforeSubmitPrompt records the
prompt, postToolUse records tool names and file paths, afterAgentResponse records the
reply, and stop uploads the completed turn. The pipeline is the CLI's own
(`raindrop.sync_cmd.run_sync`), so secrets are redacted locally before upload, the
sharing settings are honoured, and the cursor only advances after the API confirms.

Exit code 2 would block the user's action in Cursor, and a hook that crashes can
interrupt a prompt. This script therefore ALWAYS exits 0, prints nothing on stdout, and
logs failures to ~/.raindrop/sync.log and stderr.
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

TOOL = "cursor"


def sync_event(payload: dict, transport=None) -> int:
    """Handle one Cursor hook event. Returns the number of exchanges uploaded."""
    return run_sync(payload, TOOL, transport)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("Cursor hook input must be a JSON object.")
        count = sync_event(payload)
    except (api_client.ApiError, RaindropError, ValueError, OSError) as exc:
        message = getattr(exc, "message", None) or str(exc)  # ValueError covers bad JSON
        _log_failure(f"{TOOL}: {message}")
        print(f"Raindrop sync failed: {message}", file=sys.stderr)
        return 0  # fail open: never block Cursor
    if count:
        print(f"Raindrop synced {count} exchange{'' if count == 1 else 's'}.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
