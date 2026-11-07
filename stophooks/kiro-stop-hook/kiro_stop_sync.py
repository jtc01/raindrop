"""Kiro hook: record each hook event and sync finished turns to Raindrop.

Kiro keeps chat history in an undocumented database, so Raindrop builds
exchanges from hook events instead of tailing a transcript. Three Kiro hooks run
this one script (see the README): UserPromptSubmit records the prompt,
PreToolUse records tool names and file paths, and Stop records the final reply
and uploads the completed turn. The pipeline is the CLI's own
(`raindrop.sync_cmd.run_sync`), so secrets are redacted locally before upload and
the cursor only advances after the API confirms.

This script ALWAYS exits 0. Two of the three hooks run before Kiro acts, and a
non-zero exit there would block the user's prompt or tool call. Failures are
appended to ~/.raindrop/sync.log and printed to stderr instead.
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

TOOL = "kiro"


def sync_event(payload: dict, transport=None) -> int:
    """Handle one Kiro hook event. Returns the number of exchanges uploaded."""
    return run_sync(payload, TOOL, transport)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("Kiro hook input must be a JSON object.")
        count = sync_event(payload)
    except (api_client.ApiError, RaindropError, ValueError, OSError) as exc:
        message = getattr(exc, "message", None) or str(exc)  # ValueError covers bad JSON
        _log_failure(f"{TOOL}: {message}")
        print(f"Raindrop sync failed: {message}", file=sys.stderr)
        return 0  # fail open: never block Kiro
    if count:
        print(f"Raindrop synced {count} exchange{'' if count == 1 else 's'}.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
