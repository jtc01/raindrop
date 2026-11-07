from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

import stop_sync


class CodexStopSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sessions = self.root / ".codex" / "sessions"
        self.sessions.mkdir(parents=True)
        self.session_id = "session-123"
        self.transcript = self.sessions / f"rollout-{self.session_id}.jsonl"
        self.state_root = self.root / ".raindrop"
        self.cwd = self.root / "project"
        self.cwd.mkdir()
        (self.cwd / ".env").write_text("API_TOKEN=token-abcdef123456\n", encoding="utf-8")
        self.records = [
            {
                "timestamp": "2026-11-07T18:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Fix the parser"}],
                },
            },
            {
                "timestamp": "2026-11-07T18:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "apply_patch",
                    "arguments": json.dumps({"file_path": "src/parser.py"}),
                },
            },
            {
                "timestamp": "2026-11-07T18:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final",
                    "content": [{"type": "output_text", "text": "Parser fixed."}],
                },
            },
        ]
        self.transcript.write_text(
            "".join(json.dumps(record) + "\n" for record in self.records),
            encoding="utf-8",
        )
        self.payload = {
            "session_id": self.session_id,
            "transcript_path": str(self.transcript),
            "cwd": str(self.cwd),
        }
        self.cfg = {"api_url": "https://example.test", "api_key": "api-key-123456"}

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_sends_shared_exchange_fields_and_advances_cursor(self) -> None:
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"accepted": 1, "ids": [f"{self.session_id}:0"]})

        with (
            patch.object(stop_sync, "_session_root", return_value=self.sessions),
            patch.object(stop_sync, "_state_root", return_value=self.state_root),
            patch.object(stop_sync.config, "load", return_value=self.cfg),
        ):
            result = stop_sync.sync_stop(self.payload, httpx.MockTransport(handler))

        self.assertEqual(result, 1)
        exchange = requests[0]["exchanges"][0]
        self.assertEqual(
            set(exchange),
            {
                "id", "session_id", "idx", "ts", "user_text", "assistant_text",
                "tool_summaries", "files_touched",
            },
        )
        self.assertEqual(exchange["user_text"], "Fix the parser")
        self.assertEqual(exchange["assistant_text"], "Parser fixed.")
        self.assertEqual(exchange["tool_summaries"], ["apply_patch"])
        self.assertEqual(exchange["files_touched"], ["src/parser.py"])
        state = json.loads((self.state_root / f"{self.session_id}.json").read_text())
        self.assertEqual(state["next_idx"], 1)
        self.assertEqual(state["offset"], self.transcript.stat().st_size)

    def test_redacts_env_secrets_before_upload(self) -> None:
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"accepted": 1, "ids": [f"{self.session_id}:0"]})

        self.records[0]["payload"]["content"][0]["text"] += " token-abcdef123456"
        self.transcript.write_text(
            "".join(json.dumps(record) + "\n" for record in self.records),
            encoding="utf-8",
        )

        with (
            patch.object(stop_sync, "_session_root", return_value=self.sessions),
            patch.object(stop_sync, "_state_root", return_value=self.state_root),
            patch.object(stop_sync.config, "load", return_value=self.cfg),
        ):
            stop_sync.sync_stop(self.payload, httpx.MockTransport(handler))

        self.assertNotIn(
            "token-abcdef123456",
            requests[0]["exchanges"][0]["user_text"],
        )

    def test_does_not_advance_cursor_when_upload_fails(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "storage_failed", "message": "retry"})

        with (
            patch.object(stop_sync, "_session_root", return_value=self.sessions),
            patch.object(stop_sync, "_state_root", return_value=self.state_root),
            patch.object(stop_sync.config, "load", return_value=self.cfg),
            self.assertRaises(stop_sync.api_client.ApiError),
        ):
            stop_sync.sync_stop(self.payload, httpx.MockTransport(handler))

        self.assertFalse((self.state_root / f"{self.session_id}.json").exists())

    def test_sends_every_completed_exchange_since_cursor(self) -> None:
        second_user = {
            "timestamp": "2026-11-07T18:01:00Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Now add a test"}],
            },
        }
        second_assistant = {
            "timestamp": "2026-11-07T18:01:01Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "final",
                "content": [{"type": "output_text", "text": "Test added."}],
            },
        }
        self.transcript.write_text(
            "".join(
                json.dumps(record) + "\n"
                for record in self.records + [second_user, second_assistant]
            ),
            encoding="utf-8",
        )
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"accepted": 2, "ids": []})

        with (
            patch.object(stop_sync, "_session_root", return_value=self.sessions),
            patch.object(stop_sync, "_state_root", return_value=self.state_root),
            patch.object(stop_sync.config, "load", return_value=self.cfg),
        ):
            result = stop_sync.sync_stop(self.payload, httpx.MockTransport(handler))

        self.assertEqual(result, 2)
        exchanges = requests[0]["exchanges"]
        self.assertEqual([exchange["idx"] for exchange in exchanges], [0, 1])
        self.assertEqual(
            [exchange["user_text"] for exchange in exchanges],
            ["Fix the parser", "Now add a test"],
        )
        state = json.loads((self.state_root / f"{self.session_id}.json").read_text())
        self.assertEqual(state["next_idx"], 2)

    def test_ignores_incomplete_trailing_jsonl_record(self) -> None:
        with self.transcript.open("ab") as transcript:
            transcript.write(b'{"type":"event_msg"')
        records, offset = stop_sync._read_new_records(self.transcript, 0)
        self.assertEqual(len(records), len(self.records))
        self.assertEqual(offset, self.transcript.stat().st_size - len(b'{"type":"event_msg"'))

    def test_reads_valid_final_record_without_newline(self) -> None:
        final_record = {"type": "event_msg", "payload": {"type": "task_complete"}}
        encoded = json.dumps(final_record).encode("utf-8")
        with self.transcript.open("ab") as transcript:
            transcript.write(encoded)
        records, offset = stop_sync._read_new_records(self.transcript, 0)
        self.assertEqual(records[-1], final_record)
        self.assertEqual(offset, self.transcript.stat().st_size)


if __name__ == "__main__":
    unittest.main()
