from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import copilot_capture as capture  # noqa: E402

SECRET_PROMPT = "please use the password hunter2 on the staging db"
SECRET_REPLY = "sure, connecting to staging with hunter2"


class CopilotCaptureTests(unittest.TestCase):
    def make_transcript(self, temp: str) -> Path:
        path = Path(temp) / "events.jsonl"
        records = [
            {"type": "user.message", "data": {"content": SECRET_PROMPT}, "timestamp": "2026-11-07T00:00:00Z"},
            {"type": "assistant.message", "data": {"content": SECRET_REPLY, "toolRequests": []}},
        ]
        path.write_text("".join(json.dumps(r) + "\n" for r in records) + "not json\n", encoding="utf-8")
        return path

    def test_report_has_structure_but_no_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            payload = {"sessionId": "abc", "cwd": "C:/secret/project", "stopReason": "end_turn",
                       "transcriptPath": str(self.make_transcript(temp)), "timestamp": 1}
            report = capture.build_report(payload)
        blob = json.dumps(report)
        for leaked in (SECRET_PROMPT, SECRET_REPLY, "hunter2", "C:/secret/project", "abc"):
            self.assertNotIn(leaked, blob)
        self.assertTrue(report["session_id_present"])
        self.assertEqual(report["payload_shape"]["stopReason"]["category"], "end_turn")
        shapes = report["transcript"]["record_shapes"]
        self.assertEqual(report["transcript"]["records_inspected"], 3)
        self.assertEqual(shapes[0]["type"]["category"], "user.message")  # record type is the useful part
        self.assertEqual(shapes[0]["data"]["shape"]["content"]["shape"]["length"], len(SECRET_PROMPT))
        self.assertIn("invalid_json_record", shapes[2])  # unparseable line is flagged, its text is not kept

    def test_accepts_the_vs_code_snake_case_variant(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            report = capture.build_report({"hook_event_name": "Stop", "session_id": "s",
                                           "transcript_path": str(self.make_transcript(temp))})
        self.assertTrue(report["session_id_present"] and report["transcript"]["available"])

    def test_missing_transcript_is_reported_not_fatal(self) -> None:
        for payload in ({}, {"sessionId": "s"}, {"sessionId": "s", "transcriptPath": "/nope"}):
            with self.subTest(payload=payload):
                self.assertFalse(capture.build_report(payload)["transcript"]["available"])

    def test_main_writes_a_report_and_never_fails_the_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.dict("os.environ", {"RAINDROP_HOME": temp}):
            out = io.StringIO()
            with mock.patch.object(sys, "stdin", io.StringIO(json.dumps({"sessionId": "s"}))), \
                    mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", io.StringIO()):
                self.assertEqual(capture.main(), 0)
            self.assertEqual(out.getvalue(), "")  # stdout stays empty: Copilot parses it as JSON
            self.assertEqual(len(list((Path(temp) / "copilot-hook-diagnostics").glob("*.json"))), 1)
            for bad in ("not json", "[]"):
                with mock.patch.object(sys, "stdin", io.StringIO(bad)), \
                        mock.patch.object(sys, "stderr", io.StringIO()):
                    self.assertEqual(capture.main(), 0)


if __name__ == "__main__":
    unittest.main()
