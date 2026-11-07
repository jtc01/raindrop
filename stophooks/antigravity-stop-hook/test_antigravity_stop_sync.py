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
import antigravity_stop_sync as hook  # noqa: E402
from raindrop import config  # noqa: E402

KEY = "rd_supersecretkey_1234567890abcdef"
LEAK = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"
CID = "dbf33081-21f1-4738-8699-4f9bc63aae79"


def step(i, type_, source, content, **kw):
    return {"step_index": i, "source": source, "type": type_, "status": "DONE",
            "created_at": "2026-07-21T09:18:28Z", "content": content, **kw}


class AntigravityStopSyncTests(unittest.TestCase):
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
        steps = [
            step(0, "USER_INPUT", "USER_EXPLICIT", f"<USER_REQUEST>\nuse {LEAK} and {KEY}\n</USER_REQUEST>"),
            step(1, "PLANNER_RESPONSE", "MODEL", "Done.", thinking="x"),
        ]
        path = self.home / "transcript.jsonl"
        path.write_bytes("".join(json.dumps(s) + "\n" for s in steps).encode())
        return path

    def test_syncs_redacts_and_advances_cursor(self) -> None:
        sent: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"accepted": len(sent[-1]["exchanges"]), "ids": [e["id"] for e in sent[-1]["exchanges"]]})

        transport = httpx.MockTransport(handler)
        payload = {"conversationId": CID, "transcriptPath": str(self.transcript()),
                   "workspacePaths": [str(self.home)]}
        self.assertEqual(hook.sync_stop(payload, transport), 1)
        blob = json.dumps(sent)
        self.assertNotIn(LEAK, blob)
        self.assertNotIn(KEY, blob)
        self.assertEqual(sent[0]["exchanges"][0]["session_id"], CID)
        self.assertEqual(hook.sync_stop(payload, transport), 0)  # nothing new

    def run_main(self, stdin: str) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(stdin)), \
                mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys, "stderr", io.StringIO()):
            return hook.main(), out.getvalue()

    def test_always_answers_allow_and_exits_zero_even_on_failure(self) -> None:
        for stdin in ("not json", "[]", json.dumps({"conversationId": "../x"})):
            with self.subTest(stdin=stdin):
                code, stdout = self.run_main(stdin)
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(stdout), {"decision": "allow"})  # never "continue"
        self.assertIn("antigravity:", (config.CONFIG_DIR / "sync.log").read_text(encoding="utf-8"))

    def test_upload_error_still_allows(self) -> None:
        def boom(*args, **kwargs):
            raise hook.api_client.ApiError(500, "server_error", "boom")

        with mock.patch.object(hook, "sync_stop", boom):
            code, stdout = self.run_main(json.dumps({"conversationId": CID}))
        self.assertEqual((code, json.loads(stdout)), (0, {"decision": "allow"}))


if __name__ == "__main__":
    unittest.main()
