from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from raindrop.adapters.base import SessionRef
from raindrop.adapters.codex import CodexAdapter
from raindrop.adapters.cursor import CursorAdapter


class CodexAdapterTests(unittest.TestCase):
    def test_reads_completed_turn_into_shared_exchange(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout-session-1.jsonl"
            records = [
                {
                    "timestamp": "2026-11-07T18:00:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Fix parser"}],
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
            path.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            size = path.stat().st_size
            result = CodexAdapter().read_new(
                SessionRef("session-1", path, temp),
                0,
            )

        self.assertEqual(len(result.exchanges), 1)
        exchange = result.exchanges[0]
        self.assertEqual(exchange.idx, 0)
        self.assertEqual(exchange.user_text, "Fix parser")
        self.assertEqual(exchange.assistant_text, "Parser fixed.")
        self.assertEqual(exchange.tool_summaries, ["apply_patch"])
        self.assertEqual(exchange.files_touched, ["src/parser.py"])
        self.assertEqual(result.new_cursor, size)

    def test_codex_hook_install_is_idempotent_and_preserves_other_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "hooks.json"
            path.write_text(json.dumps({
                "hooks": {
                    "Stop": [{
                        "hooks": [{
                            "type": "command",
                            "command": "echo keep",
                        }]
                    }]
                }
            }), encoding="utf-8")
            adapter = CodexAdapter()
            with patch("raindrop.adapters.codex._hooks_path", return_value=path):
                adapter.plan_install(rules=False)[0].run()
                adapter.plan_install(rules=False)[0].run()
            hooks = json.loads(path.read_text(encoding="utf-8"))["hooks"]["Stop"]

        commands = [
            handler["command"]
            for group in hooks
            for handler in group["hooks"]
        ]
        self.assertIn("echo keep", commands)
        self.assertEqual(commands.count("raindrop sync --tool codex"), 1)

    def test_holds_unanswered_turn_at_prompt_offset(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout-session-1.jsonl"
            prompt = json.dumps({
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "still working"}],
                },
            }) + "\n"
            path.write_text(prompt, encoding="utf-8")
            result = CodexAdapter().read_new(SessionRef("session-1", path), 0)
        self.assertEqual(result.exchanges, [])
        self.assertEqual(result.new_cursor, 0)


class CursorAdapterTests(unittest.TestCase):
    def test_holds_prompt_until_response_arrives(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            spool = Path(temp) / "cursor.jsonl"
            adapter = CursorAdapter()
            payload = {
                "conversation_id": "conversation-1",
                "cwd": temp,
                "hook_event_name": "beforeSubmitPrompt",
                "prompt": "wait for the answer",
            }
            with patch("raindrop.adapters.cursor._spool_path", return_value=spool):
                adapter.capture(payload)
                pending = adapter.read_new(SessionRef("conversation-1", None, temp), 0)
                adapter.capture({
                    "conversation_id": "conversation-1",
                    "cwd": temp,
                    "hook_event_name": "afterAgentResponse",
                    "text": "Here is the answer.",
                })
                complete = adapter.read_new(
                    SessionRef("conversation-1", None, temp),
                    pending.new_cursor,
                )

        self.assertEqual(pending.exchanges, [])
        self.assertEqual(pending.new_cursor, 0)
        self.assertEqual(len(complete.exchanges), 1)
        self.assertEqual(complete.exchanges[0].assistant_text, "Here is the answer.")

    def test_spools_cursor_events_as_shared_exchange(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            spool = Path(temp) / "cursor.jsonl"
            adapter = CursorAdapter()
            payload_base = {"conversation_id": "conversation-1", "cwd": temp}
            with patch("raindrop.adapters.cursor._spool_path", return_value=spool):
                adapter.capture({
                    **payload_base,
                    "hook_event_name": "beforeSubmitPrompt",
                    "prompt": "Fix parser",
                })
                adapter.capture({
                    **payload_base,
                    "hook_event_name": "postToolUse",
                    "tool_name": "edit_file",
                    "tool_input": {"file_path": "src/parser.py"},
                })
                adapter.capture({
                    **payload_base,
                    "hook_event_name": "afterAgentResponse",
                    "text": "Parser fixed.",
                })
                size = spool.stat().st_size
                result = adapter.read_new(SessionRef("conversation-1", None, temp), 0)

        self.assertEqual(len(result.exchanges), 1)
        exchange = result.exchanges[0]
        self.assertEqual(exchange.user_text, "Fix parser")
        self.assertEqual(exchange.assistant_text, "Parser fixed.")
        self.assertEqual(exchange.tool_summaries, ["edit_file"])
        self.assertEqual(exchange.files_touched, ["src/parser.py"])
        self.assertEqual(result.new_cursor, size)

    def test_cursor_hook_install_merges_events_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "hooks.json"
            path.write_text(json.dumps({
                "version": 1,
                "hooks": {
                    "stop": [{"command": "echo keep"}],
                },
            }), encoding="utf-8")
            adapter = CursorAdapter()
            with patch("raindrop.adapters.cursor._hooks_path", return_value=path):
                adapter.plan_install(rules=False)[0].run()
                adapter.plan_install(rules=False)[0].run()
            data = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(data["hooks"]["stop"][0]["command"], "echo keep")
        for event in ("beforeSubmitPrompt", "postToolUse", "afterAgentResponse", "stop"):
            self.assertEqual(
                sum("raindrop sync --tool cursor" in hook["command"]
                    for hook in data["hooks"][event]),
                1,
            )


if __name__ == "__main__":
    unittest.main()
