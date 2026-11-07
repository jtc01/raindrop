from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kiro_stop_sync  # noqa: E402
from raindrop import config  # noqa: E402

KEY = "rd_supersecretkey_1234567890abcdef"
LEAK = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"


def event(name: str, **kw) -> dict:
    return {"hook_event_name": name, "session_id": "kiro-s1", "cwd": "/proj", **kw}


class KiroStopSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        home = Path(self.temp.name)
        patcher = mock.patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        old = (config.CONFIG_DIR, config.CONFIG_PATH)
        config.CONFIG_DIR = home / ".raindrop"
        config.CONFIG_PATH = config.CONFIG_DIR / "config.json"
        self.addCleanup(lambda: (setattr(config, "CONFIG_DIR", old[0]),
                                 setattr(config, "CONFIG_PATH", old[1])))
        config.save({"api_url": "https://api.test", "api_key": KEY, "agent_id": "a",
                     "team_id": "t", "display_name": "D", "focus": "", "tools": {},
                     "sharing": {"mode": "all"}})  # default is opt-in per repo
        self.sent: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            self.sent.append(body)
            return httpx.Response(200, json={"accepted": len(body["exchanges"])})

        self.transport = httpx.MockTransport(handler)

    def test_turn_is_redacted_and_uploaded_only_at_stop(self) -> None:
        sync = lambda p: kiro_stop_sync.sync_event(p, self.transport)  # noqa: E731
        self.assertEqual(sync(event("UserPromptSubmit", prompt=f"use {LEAK} and {KEY}")), 0)
        self.assertEqual(sync(event("PreToolUse", tool_name="execute_bash",
                                    tool_input={"command": "pytest -q"})), 0)
        self.assertEqual(self.sent, [])  # nothing leaves the machine mid-turn

        self.assertEqual(sync(event("Stop", assistant_response="Fixed it.")), 1)
        blob = json.dumps(self.sent)
        self.assertNotIn(LEAK, blob)
        self.assertNotIn(KEY, blob)
        exchange = self.sent[0]["exchanges"][0]
        self.assertEqual(exchange["session_id"], "kiro-s1")  # ids survive redaction
        self.assertEqual(exchange["assistant_text"], "Fixed it.")

    def test_main_fails_open_and_logs(self) -> None:
        for stdin in ("not json", "[]"):
            with self.subTest(stdin=stdin), \
                    mock.patch.object(sys, "stdin", io.StringIO(stdin)), \
                    mock.patch.object(sys, "stderr", io.StringIO()):
                self.assertEqual(kiro_stop_sync.main(), 0)  # never blocks Kiro
        log = (config.CONFIG_DIR / "sync.log").read_text(encoding="utf-8")
        self.assertIn("kiro:", log)

    def test_main_fails_open_on_upload_error(self) -> None:
        def boom(*args, **kwargs):
            raise kiro_stop_sync.api_client.ApiError(500, "server_error", "boom")

        payload = json.dumps(event("Stop", assistant_response="x"))
        with mock.patch.object(kiro_stop_sync, "sync_event", boom), \
                mock.patch.object(sys, "stdin", io.StringIO(payload)), \
                mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(kiro_stop_sync.main(), 0)


if __name__ == "__main__":
    unittest.main()
