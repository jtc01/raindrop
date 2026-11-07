"""Privacy-preserving Cursor Stop hook schema capture."""

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
SAFE_CATEGORIES = {"type", "role", "kind", "phase", "event", "status"}


def _diagnostics_dir() -> Path:
    return Path(
        os.environ.get(
            "RAINDROP_HOME",
            Path.home() / ".raindrop",
        )
    ) / "cursor-hook-diagnostics"


def _shape(value: Any, depth: int = 0) -> Any:
    """Describe JSON structure without retaining free-form string values."""
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            description: dict[str, Any] = {"shape": _shape(child, depth + 1)}
            if key.lower() in SAFE_CATEGORIES and isinstance(child, str):
                description["category"] = child[:80]
            result[str(key)] = description
        return result
    if isinstance(value, list):
        item_shapes = [_shape(item, depth + 1) for item in value[:3]]
        return {
            "type": "array",
            "length": len(value),
            "item_shapes": item_shapes,
        }
    if isinstance(value, str):
        return {"type": "string", "length": len(value)}
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, (int, float)):
        return {"type": "number"}
    return {"type": type(value).__name__}


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
            tail = transcript.read(MAX_TRANSCRIPT_TAIL_BYTES)
            lines = tail.splitlines()
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
    conversation_id = payload.get("conversation_id")
    transcript_path = payload.get("transcript_path")
    identity = conversation_id if isinstance(conversation_id, str) else repr(payload)
    report: dict[str, Any] = {
        "hook": "stop",
        "payload_shape": _shape(payload),
        "conversation_id_present": isinstance(conversation_id, str) and bool(conversation_id),
        "transcript": _transcript_structure(transcript_path),
    }
    report["capture_id"] = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return report


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
        raise RuntimeError(f"Could not write Cursor hook diagnostics to {output_path}") from exc
    return output_path


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("Cursor hook input must be a JSON object.")
        path = capture(payload)
    except (OSError, json.JSONDecodeError, ValueError, RuntimeError) as exc:
        print(f"Cursor hook diagnostic capture failed: {exc}", file=sys.stderr)
        return 1
    print(f"Cursor hook structure saved locally to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
