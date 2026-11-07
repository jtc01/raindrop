from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import claude_stop_sync as stop_sync  # noqa: E402
from raindrop import config  # noqa: E402

KEY = "rd_supersecretkey_1234567890abcdef"
LEAK = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"
SID = "019a1b2c-3d4e-7f80-9a1b-2c3d4e5f6a7b"


class StopSyncTests(unittest.TestCase):
    def test_syncs_redacts_and_advances_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            old = (config.CONFIG_DIR, config.CONFIG_PATH)
            config.CONFIG_DIR = home / ".raindrop"
            config.CONFIG_PATH = config.CONFIG_DIR / "config.json"
            self.addCleanup(lambda: (setattr(config, "CONFIG_DIR", old[0]),
                                     setattr(config, "CONFIG_PATH", old[1])))
            config.save({"api_url": "https://api.test", "api_key": KEY, "agent_id": "a",
                         "team_id": "t", "display_name": "D", "focus": "", "tools": {},
                         "sharing": {"mode": "all"}})  # default is opt-in per repo

            lines = [
                {"type": "user", "timestamp": "2026-01-01T00:00:00Z",
                 "message": {"content": f"use {LEAK}"}},
                {"type": "assistant",
                 "message": {"content": [{"type": "text", "text": f"done {KEY}"}]}},
            ]
            transcript = home / "t.jsonl"
            transcript.write_bytes("".join(json.dumps(x) + "\n" for x in lines).encode())
            payload = {"session_id": SID, "transcript_path": str(transcript), "cwd": temp}

            sent: list[dict] = []

            def handler(request: httpx.Request) -> httpx.Response:
                sent.append(json.loads(request.content))
                return httpx.Response(200, json={"accepted": len(sent[-1]["exchanges"])})

            transport = httpx.MockTransport(handler)
            self.assertEqual(stop_sync.sync_stop(payload, transport), 1)
            blob = json.dumps(sent)
            self.assertNotIn(LEAK, blob)
            self.assertNotIn(KEY, blob)
            self.assertEqual(sent[0]["exchanges"][0]["session_id"], SID)
            # nothing new on the next run
            self.assertEqual(stop_sync.sync_stop(payload, transport), 0)


if __name__ == "__main__":
    unittest.main()
