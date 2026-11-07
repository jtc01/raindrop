"""Codex Stop hook prototype for syncing completed turns to Raindrop."""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CLI_DIR = Path(__file__).resolve().parents[2] / "teamctx-cli"
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

import api_client
import config
from redact import load_env_secrets, redact_obj
from teamctx.constants import BATCH_SIZE
from teamctx.models import Exchange

SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
PATH_KEYS = {"file_path", "path", "filename"}
TURN_END_TYPES = {"task_complete", "turn_complete"}


class SyncError(Exception):
    """A hook input, transcript, or upload could not be processed safely."""


def _session_root() -> Path:
    return Path.home() / ".codex" / "sessions"


def _state_root() -> Path:
    return Path(os.environ.get("RAINDROP_HOME", Path.home() / ".raindrop")) / "cursors" / "codex"


def _session_id(payload: dict[str, Any]) -> str:
    value = payload.get("session_id")
    if not isinstance(value, str) or not SAFE_SESSION_ID.fullmatch(value) or ".." in value:
        raise SyncError("Codex Stop hook did not provide a valid session_id.")
    return value


def _transcript_path(payload: dict[str, Any], session_id: str) -> Path:
    sessions = _session_root().resolve()
    given = payload.get("transcript_path")
    if given:
        path = Path(given).expanduser().resolve()
        if not path.is_relative_to(sessions):
            raise SyncError("Codex transcript_path is outside the Codex sessions directory.")
        if not path.is_file():
            raise SyncError(f"Codex transcript does not exist: {path}")
        return path

    matches = [
        path.resolve()
        for path in sessions.rglob("*.jsonl")
        if session_id in path.name and path.is_file()
    ]
    if len(matches) != 1:
        raise SyncError(
            f"Expected one Codex transcript for session {session_id}, found {len(matches)}."
        )
    return matches[0]


def _read_new_records(path: Path, offset: int) -> tuple[list[dict[str, Any]], int]:
    size = path.stat().st_size
    if offset < 0 or offset > size:
        raise SyncError("Stored transcript cursor is invalid; refusing to skip session data.")

    records: list[dict[str, Any]] = []
    with path.open("rb") as transcript:
        transcript.seek(offset)
        while True:
            line_start = transcript.tell()
            line = transcript.readline()
            if not line:
                return records, line_start
            if not line.endswith(b"\n"):
                try:
                    record = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return records, line_start
                if not isinstance(record, dict):
                    raise SyncError(f"Unexpected Codex transcript record at byte {line_start}.")
                records.append(record)
                return records, transcript.tell()
            try:
                record = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SyncError(f"Could not parse Codex transcript at byte {line_start}.") from exc
            if isinstance(record, dict):
                records.append(record)
            else:
                raise SyncError(f"Unexpected Codex transcript record at byte {line_start}.")


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") not in {"input_text", "output_text", "text"}:
            continue
        text = item.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts)


def _timestamp(value: Any) -> int:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp())
        except ValueError:
            pass
    return int(datetime.now(timezone.utc).timestamp())


def _tool_name(payload: dict[str, Any]) -> str | None:
    if payload.get("type") not in {"function_call", "tool_call"}:
        return None
    name = payload.get("name")
    return name if isinstance(name, str) and name else None


def _collect_paths(value: Any, cwd: Path, paths: list[str], key: str = "") -> None:
    if isinstance(value, dict):
        for child_key, child in value.items():
            _collect_paths(child, cwd, paths, child_key.lower())
        return
    if isinstance(value, list):
        for child in value:
            _collect_paths(child, cwd, paths, key)
        return
    if key not in PATH_KEYS or not isinstance(value, str) or not value:
        return

    candidate = Path(value)
    if candidate.is_absolute():
        try:
            candidate = candidate.resolve().relative_to(cwd.resolve())
        except (OSError, ValueError):
            return
    if ".." in candidate.parts:
        return
    normalized = candidate.as_posix()
    if normalized not in paths:
        paths.append(normalized)


def _record_payload(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else {}


def _extract_exchanges(records: list[dict[str, Any]], session_id: str, idx: int,
                       cwd: str, last_assistant_message: Any) -> list[Exchange]:
    current: dict[str, Any] | None = None
    completed: list[Exchange] = []
    workdir = Path(cwd).expanduser()

    def finish() -> None:
        nonlocal current
        if current is None:
            return
        assistant = current.get("final_text") or current.get("assistant_text", "")
        if current.get("user_text", "").strip() and assistant.strip():
            completed.append(
                Exchange(
                    session_id=session_id,
                    idx=idx + len(completed),
                    ts=current["ts"],
                    user_text=current["user_text"],
                    assistant_text=assistant,
                    tool_summaries=current["tool_summaries"],
                    files_touched=current["files_touched"],
                )
            )
        current = None

    for record in records:
        payload = _record_payload(record)
        kind = record.get("type")
        item_type = payload.get("type")

        if kind == "response_item" and item_type == "message":
            role = payload.get("role")
            text = _text_from_content(payload.get("content"))
            if role == "user":
                finish()
                current = {
                    "user_text": text,
                    "assistant_text": "",
                    "final_text": "",
                    "ts": _timestamp(record.get("timestamp")),
                    "tool_summaries": [],
                    "files_touched": [],
                }
            elif role == "assistant" and current is not None and text:
                current["assistant_text"] = text
                if payload.get("phase") == "final":
                    current["final_text"] = text
            continue

        name = _tool_name(payload)
        if name and current is not None:
            if name not in current["tool_summaries"]:
                current["tool_summaries"].append(name)
            arguments = payload.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = None
            _collect_paths(arguments, workdir, current["files_touched"])

        if kind == "event_msg" and payload.get("type") in TURN_END_TYPES:
            finish()

    finish()
    if completed and isinstance(last_assistant_message, str) and last_assistant_message.strip():
        completed[-1].assistant_text = last_assistant_message
    return completed


def _load_state(path: Path) -> dict[str, int]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"offset": 0, "next_idx": 0}
    except (OSError, json.JSONDecodeError) as exc:
        raise SyncError(f"Could not read Codex sync cursor: {path}") from exc
    if (
        not isinstance(state, dict)
        or not isinstance(state.get("offset"), int)
        or not isinstance(state.get("next_idx"), int)
        or state["offset"] < 0
        or state["next_idx"] < 0
    ):
        raise SyncError(f"Codex sync cursor is invalid: {path}")
    return state


def _save_state(path: Path, state: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(temporary, path)
    except OSError as exc:
        raise SyncError(f"Could not save Codex sync cursor: {path}") from exc


def sync_stop(payload: dict[str, Any], transport: Any = None) -> int:
    session_id = _session_id(payload)
    transcript = _transcript_path(payload, session_id)
    state_path = _state_root() / f"{session_id}.json"
    state = _load_state(state_path)
    records, new_offset = _read_new_records(transcript, state["offset"])
    if not records:
        raise SyncError("No complete Codex transcript records were available at Stop.")

    cfg = config.load()
    if not cfg:
        raise SyncError("Not joined to a Raindrop team; run `raindrop join` first.")
    for key in ("api_url", "api_key"):
        if not isinstance(cfg.get(key), str) or not cfg[key]:
            raise SyncError(f"Raindrop config is missing {key}.")

    exchanges = _extract_exchanges(
        records,
        session_id,
        state["next_idx"],
        payload.get("cwd") if isinstance(payload.get("cwd"), str) else str(Path.cwd()),
        payload.get("last_assistant_message"),
    )
    if not exchanges:
        raise SyncError("Could not find a completed user/assistant exchange in the Codex transcript.")

    known_secrets = [cfg["api_key"]]
    cwd = payload.get("cwd")
    if isinstance(cwd, str):
        known_secrets.extend(load_env_secrets(Path(cwd)))
    wire_exchanges = [
        redact_obj(exchange.to_wire(), known_secrets=known_secrets)
        for exchange in exchanges
    ]
    for start in range(0, len(wire_exchanges), BATCH_SIZE):
        batch = wire_exchanges[start : start + BATCH_SIZE]
        response = api_client._post(
            cfg["api_url"],
            "/messages",
            {"exchanges": batch},
            api_key=cfg["api_key"],
            transport=transport,
        )
        if type(response.get("accepted")) is not int or response["accepted"] != len(batch):
            raise SyncError("Raindrop API did not confirm that the full exchange batch was stored.")

    _save_state(state_path, {
        "offset": new_offset,
        "next_idx": exchanges[-1].idx + 1,
    })
    return len(exchanges)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise SyncError("Codex Stop hook input must be a JSON object.")
        accepted = sync_stop(payload)
    except (api_client.ApiError, SyncError) as exc:
        print(f"Raindrop Codex sync failed: {exc}", file=sys.stderr)
        return 1
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Raindrop Codex sync could not read hook input: {exc}", file=sys.stderr)
        return 1
    noun = "exchange" if accepted == 1 else "exchanges"
    print(f"Raindrop synced {accepted} {noun}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
