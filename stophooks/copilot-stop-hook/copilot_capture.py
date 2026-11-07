"""Privacy-preserving GitHub Copilot hook schema capture.

Copilot's hooks reference documents the `agentStop` payload (sessionId, cwd,
transcriptPath, stopReason) but not the transcript file format, so a parser
would be a guess. This hook records the *structure* of one real payload and its
transcript instead; it saves no prompt or reply text and uploads nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

MAX_TRANSCRIPT_RECORDS = 20
MAX_RECORD_BYTES = 2_000_000
MAX_TRANSCRIPT_TAIL_BYTES = 4_000_000
SAFE_CATEGORIES = {"type", "role", "kind", "phase", "event", "status", "stopreason", "stop_reason"}
# The documented CLI payload is camelCase; the VS Code-compatible variant is snake_case.
SESSION_KEYS = ("sessionId", "session_id")
TRANSCRIPT_KEYS = ("transcriptPath", "transcript_path")


def _diagnostics_dir() -> Path:
    return Path(os.environ.get("RAINDROP_HOME", Path.home() / ".raindrop")) / "copilot-hook-diagnostics"


def _shape(value: Any) -> Any:
    """Describe JSON structure without retaining free-form string values."""
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            description: dict[str, Any] = {"shape": _shape(child)}
            if key.lower() in SAFE_CATEGORIES and isinstance(child, str):
                description["category"] = child[:80]
            result[str(key)] = description
        return result
    if isinstance(value, list):
        return {"type": "array", "length": len(value), "item_shapes": [_shape(i) for i in value[:3]]}
    if isinstance(value, str):
        return {"type": "string", "length": len(value)}
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, (int, float)):
        return {"type": "number"}
    return {"type": type(value).__name__}


def _first(payload: dict, keys: tuple[str, ...]) -> Any:
    return next((payload[k] for k in keys if payload.get(k)), None)


def _transcript_structure(path_value: Any) -> dict[str, Any]:
    if not isinstance(path_value, str) or not path_value.strip():
        return {"available": False, "reason": "transcript_path_missing"}
    path = Path(path_value).expanduser()
    if not path.is_file():
        return {"available": False, "reason": "transcript_unreadable"}

    records: list[Any] = []
    try:
        with path.open("rb") as transcript:
            transcript.seek(0, os.SEEK_END)
            end = transcript.tell()
            start = max(0, end - MAX_TRANSCRIPT_TAIL_BYTES)
            transcript.seek(start)
            lines = transcript.read(MAX_TRANSCRIPT_TAIL_BYTES).splitlines()
            if start and lines:
                lines = lines[1:]
            for line in lines[-MAX_TRANSCRIPT_RECORDS:]:
                if len(line) > MAX_RECORD_BYTES:
                    records.append({"oversized_record": True})
                    continue
                try:
                    records.append(json.loads(line))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    records.append({"invalid_json_record": True})
    except OSError:
        return {"available": False, "reason": "transcript_unreadable"}

    return {
        "available": True,
        "records_inspected": len(records),
        "record_shapes": [_shape(record) for record in records],
    }


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    session_id = _first(payload, SESSION_KEYS)
    identity = session_id if isinstance(session_id, str) else repr(sorted(payload))
    return {
        "hook": "agentStop",
        "payload_shape": _shape(payload),
        "session_id_present": isinstance(session_id, str),
        "transcript": _transcript_structure(_first(payload, TRANSCRIPT_KEYS)),
        "capture_id": hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16],
    }


def capture(payload: dict[str, Any], output_dir: Path | None = None) -> Path:
    report = build_report(payload)
    destination = output_dir or _diagnostics_dir()
    destination.mkdir(parents=True, exist_ok=True)
    output_path = destination / f"{report['capture_id']}.json"
    temporary_path = output_path.with_suffix(".tmp")
    try:
        with temporary_path.open("w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
            handle.write("\n")
        try:
            os.chmod(temporary_path, 0o600)
        except OSError:
            pass
        os.replace(temporary_path, output_path)
    except OSError as exc:
        raise RuntimeError(f"Could not write Copilot hook diagnostics to {output_path}") from exc
    return output_path


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("Copilot hook input must be a JSON object.")
        path = capture(payload)
    except (OSError, ValueError, RuntimeError) as exc:  # ValueError covers bad JSON
        print(f"Copilot hook diagnostic capture failed: {exc}", file=sys.stderr)
        return 0  # a diagnostic must never disturb the agent
    print(f"Copilot hook structure saved locally to {path}", file=sys.stderr)  # stdout must stay empty
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
