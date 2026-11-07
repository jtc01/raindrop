from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import capture


class CursorHookCaptureTests(unittest.TestCase):
    def test_capture_records_hook_and_transcript_shapes_without_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            transcript = root / "transcript.jsonl"
            transcript.write_text(
                json.dumps({
                    "message": {
                        "role": "assistant",
                        "content": "private assistant response",
                    },
                    "user_message": "private prompt",
                }) + "\n",
                encoding="utf-8",
            )
            report_path = capture.capture(
                {
                    "conversation_id": "private-conversation-id",
                    "transcript_path": str(transcript),
                    "status": "completed",
                    "private_prompt": "do not retain this text",
                },
                root / "diagnostics",
            )

            raw = report_path.read_text(encoding="utf-8")
            report = json.loads(raw)
            self.assertNotIn("private assistant response", raw)
            self.assertNotIn("private prompt", raw)
            self.assertNotIn("private-conversation-id", raw)
            self.assertNotIn("do not retain this text", raw)
            self.assertEqual(report["hook"], "stop")
            self.assertTrue(report["conversation_id_present"])
            self.assertTrue(report["transcript"]["available"])
            self.assertEqual(report["transcript"]["records_inspected"], 1)
            self.assertEqual(
                report["payload_shape"]["status"]["category"],
                "completed",
            )

    def test_missing_transcript_is_reported_without_failing_capture(self) -> None:
        report = capture.build_report({"conversation_id": "id"})
        self.assertEqual(
            report["transcript"],
            {"available": False, "reason": "transcript_path_missing"},
        )

    def test_shape_does_not_expose_unrecognized_string_values(self) -> None:
        shape = capture._shape({"tool_name": "secret-command", "type": "assistant"})
        self.assertEqual(shape["tool_name"]["shape"]["length"], len("secret-command"))
        self.assertEqual(shape["type"]["category"], "assistant")


if __name__ == "__main__":
    unittest.main()
