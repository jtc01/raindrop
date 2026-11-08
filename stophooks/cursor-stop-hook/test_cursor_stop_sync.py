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
import cursor_stop_sync as hook  # noqa: E402
from raindrop import config  # noqa: E402

KEY = "rd_supersecretkey_1234567890abcdef"
LEAK = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"
CID = "a1b2c3d4-0000-4000-8000-000000000001"


class CursorStopSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / ".git").mkdir()  # the temp folder is the repo root, wherever it lives
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
        self.sent: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            self.sent.append(body)
            return httpx.Response(200, json={"accepted": len(body["exchanges"]),
                                             "ids": [e["id"] for e in body["exchanges"]]})

        self.transport = httpx.MockTransport(handler)

    def event(self, name: str, **kw) -> dict:
        return {"hook_event_name": name, "conversation_id": CID,
                "workspace_roots": [str(self.home)], **kw}

    def test_turn_is_redacted_and_uploaded_at_stop_with_the_last_reply(self) -> None:
        sync = lambda p: hook.sync_event(p, self.transport)  # noqa: E731
        self.assertEqual(sync(self.event("beforeSubmitPrompt", prompt=f"use {LEAK} and {KEY}")), 0)
        # Cursor may reply before each tool call, so a reply does not end the turn.
        self.assertEqual(sync(self.event("afterAgentResponse", text="Let me look at the code first.")), 0)
        self.assertEqual(sync(self.event("postToolUse", tool_name="Edit",
                                         tool_input={"file_path": str(self.home / "src" / "a.py")})), 0)
        self.assertEqual(sync(self.event("afterAgentResponse", text="Fixed it.")), 0)
        self.assertEqual(self.sent, [])  # nothing leaves the machine until the turn is over

        self.assertEqual(sync(self.event("stop", status="completed", loop_count=0)), 1)
        blob = json.dumps(self.sent)
        self.assertNotIn(LEAK, blob)
        self.assertNotIn(KEY, blob)
        exchange = self.sent[0]["exchanges"][0]
        self.assertEqual(exchange["session_id"], CID)  # ids survive redaction
        self.assertEqual(exchange["assistant_text"], "Fixed it.")  # the last reply, not the first
        self.assertEqual(exchange["files_touched"], ["src/a.py"])
        self.assertEqual(sync(self.event("stop", status="completed", loop_count=1)), 0)  # nothing is sent twice

    def test_a_turn_whose_stop_never_fired_is_uploaded_when_the_next_prompt_starts(self) -> None:
        sync = lambda p: hook.sync_event(p, self.transport)  # noqa: E731
        sync(self.event("beforeSubmitPrompt", prompt="first question"))
        sync(self.event("afterAgentResponse", text="first answer"))
        self.assertEqual(self.sent, [])
        self.assertEqual(sync(self.event("beforeSubmitPrompt", prompt="second question")), 1)  # first turn, now closed
        self.assertEqual(self.sent[0]["exchanges"][0]["user_text"], "first question")
        self.assertEqual(sync(self.event("afterAgentResponse", text="second answer")), 0)  # the second is still open
        self.assertEqual(sync(self.event("stop", status="completed", loop_count=0)), 1)
        self.assertEqual(self.sent[1]["exchanges"][0]["user_text"], "second question")

    def run_main(self, stdin: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(stdin)), \
                mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys, "stderr", io.StringIO()):
            return hook.main(), out.getvalue()

    def test_never_blocks_cursor_and_keeps_stdout_empty(self) -> None:
        for stdin in ("not json", "[]", json.dumps({"hook_event_name": "stop"})):
            with self.subTest(stdin=stdin):
                code, stdout = self.run_main(stdin)
                self.assertEqual((code, stdout), (0, ""))  # exit 2 would block Cursor
        self.assertIn("cursor:", (config.CONFIG_DIR / "sync.log").read_text(encoding="utf-8"))

    def test_upload_error_still_exits_zero(self) -> None:
        def boom(*args, **kwargs):
            raise hook.api_client.ApiError(500, "server_error", "boom")

        with mock.patch.object(hook, "sync_event", boom):
            code, stdout = self.run_main(json.dumps(self.event("stop")))
        self.assertEqual((code, stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
