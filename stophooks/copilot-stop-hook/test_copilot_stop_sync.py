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
import copilot_stop_sync as hook  # noqa: E402
from raindrop import config  # noqa: E402

KEY = "rd_supersecretkey_1234567890abcdef"
LEAK = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"
SID = "bc2fe3cb-216e-4c85-b203-75064a38a1d2"


def event(type_: str, **data) -> dict:
    return {"type": type_, "data": data, "id": "x", "timestamp": "2026-11-07T18:00:00.000Z"}


class CopilotStopSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        old = (config.CONFIG_DIR, config.CONFIG_PATH)
        config.CONFIG_DIR = self.home / ".raindrop"
        config.CONFIG_PATH = config.CONFIG_DIR / "config.json"
        self.addCleanup(lambda: (setattr(config, "CONFIG_DIR", old[0]),
                                 setattr(config, "CONFIG_PATH", old[1])))
        config.save({"api_url": "https://api.test", "api_key": KEY, "agent_id": "a",
                     "team_id": "t", "display_name": "D", "focus": "", "tools": {},
                     "sharing": {"mode": "all"}})  # default is opt-in per repo

    def transcript(self) -> Path:
        events = [
            event("session.start", sessionId=SID),
            event("user.message", content=f"use {LEAK} and {KEY}", attachments=[]),
            event("assistant.message", content="Done.", toolRequests=[], reasoningText="x"),
        ]
        path = self.home / f"{SID}.jsonl"
        path.write_bytes("".join(json.dumps(e) + "\n" for e in events).encode())
        return path

    def test_syncs_redacts_and_advances_cursor(self) -> None:
        sent: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"accepted": len(sent[-1]["exchanges"]), "ids": [e["id"] for e in sent[-1]["exchanges"]]})

        transport = httpx.MockTransport(handler)
        payload = {"sessionId": SID, "transcriptPath": str(self.transcript()), "cwd": str(self.home)}
        self.assertEqual(hook.sync_stop(payload, transport), 1)
        blob = json.dumps(sent)
        self.assertNotIn(LEAK, blob)
        self.assertNotIn(KEY, blob)
        self.assertEqual(sent[0]["exchanges"][0]["session_id"], SID)
        self.assertEqual(hook.sync_stop(payload, transport), 0)  # nothing new

    def run_main(self, stdin: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(stdin)), \
                mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys, "stderr", io.StringIO()):
            return hook.main(), out.getvalue()

    def test_never_fails_the_agent_and_keeps_stdout_empty(self) -> None:
        for stdin in ("not json", "[]", json.dumps({"sessionId": "../x"})):
            with self.subTest(stdin=stdin):
                code, stdout = self.run_main(stdin)
                self.assertEqual((code, stdout), (0, ""))  # Copilot parses stdout as JSON
        self.assertIn("copilot:", (config.CONFIG_DIR / "sync.log").read_text(encoding="utf-8"))

    def test_upload_error_still_exits_zero(self) -> None:
        def boom(*args, **kwargs):
            raise hook.api_client.ApiError(500, "server_error", "boom")

        with mock.patch.object(hook, "sync_stop", boom):
            code, stdout = self.run_main(json.dumps({"sessionId": SID}))
        self.assertEqual((code, stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
