"""`raindrop sync`: push new exchanges from a tool's transcript to the team.

Run by the tool's Stop hook (hook JSON on stdin). Works for any adapter in
raindrop/adapters/registry.py; the hook passes `--tool <name>`.

Cursor handling: one file per session at ~/.raindrop/cursors/<tool>/<session_id>
holding the adapter's opaque cursor. It only advances past exchanges that are
either confirmed stored (in the server's `ids`) or permanently given up on
(see below), so a failed push is retried next turn. Exchange ids are
deterministic, so a resend overwrites rather than duplicates.

Per-exchange failure tracking: push_messages can accept fewer exchanges than
were sent (e.g. one exchange's embedding keeps failing) while still returning
200. Comparing `accepted` to the batch size only tells you *that* something
was rejected, not *which* exchange -- and retrying the same batch forever
means one permanently-bad exchange blocks every later exchange in the session
too. So failures are tracked per exchange id (alongside the cursor, at
~/.raindrop/cursors/<tool>/<session_id>.state.json) using the `ids` the server
already returns. An exchange still missing from `ids` after
MAX_EXCHANGE_FAILURES attempts is logged and skipped for good; everything
else keeps retrying normally.
"""
from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import typer

from raindrop import api_client
from raindrop import config
from raindrop.redact import LocalIdentifiers, load_env_secrets, redact_obj, scrub_obj
from raindrop.adapters.base import Adapter, SessionRef
from raindrop.adapters.registry import get_adapter
from raindrop.constants import BATCH_SIZE
from raindrop.errors import NotJoinedError, RaindropError

SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

# Give up on a single exchange after this many push attempts where the server's
# `ids` still doesn't include it, rather than letting it block the session's
# sync forever. Comfortably more than enough retries to ride out a transient
# failure (throttling, a brief S3 Vectors blip) without papering over a
# systemic outage affecting every exchange.
MAX_EXCHANGE_FAILURES = 5


def _cursor_path(tool: str, session_id: str) -> Path:
    if not SAFE_SESSION_ID.fullmatch(session_id) or ".." in session_id:
        raise RaindropError(f"Refusing unsafe session_id: {session_id!r}")
    # ":" is legal in session ids but not in Windows filenames.
    return config.CONFIG_DIR / "cursors" / tool / session_id.replace(":", "_")


def _load_cursor(path: Path) -> int:
    try:
        return int(path.read_text().strip())
    except FileNotFoundError:
        return 0
    except (OSError, ValueError) as e:
        raise RaindropError(f"Sync cursor is unreadable: {path}") from e


def _save_cursor(path: Path, cursor: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(str(cursor))
    tmp.replace(path)


def _state_path(cursor_file: Path) -> Path:
    return cursor_file.parent / (cursor_file.name + ".state.json")


def _load_state(cursor_file: Path) -> tuple[dict[str, int], set[str]]:
    """{exchange_id: consecutive push failures} and the set of exchange ids
    given up on for good."""
    try:
        data = json.loads(_state_path(cursor_file).read_text())
        return dict(data.get("failures", {})), set(data.get("skipped", []))
    except (FileNotFoundError, ValueError):
        return {}, set()


def _save_state(cursor_file: Path, failures: dict[str, int], skipped: set[str]) -> None:
    path = _state_path(cursor_file)
    if not failures and not skipped:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"failures": failures, "skipped": sorted(skipped)}))
    tmp.replace(path)


def _known_secrets(cfg: dict, session: SessionRef) -> list[str]:
    """Exact values to scrub: our API key plus secret-looking values from the
    session repo's .env files. Shared by real and dry-run syncs."""
    secrets = [cfg["api_key"]]
    if session.cwd:
        secrets += load_env_secrets(Path(session.cwd))
    return secrets


def _local_identifiers(session: SessionRef) -> LocalIdentifiers:
    """This machine's paths, username and hostname, which must not reach
    teammates. The repo root makes in-repo paths repo-relative."""
    repo = config.repo_root(session.cwd) if session.cwd else None
    return LocalIdentifiers.detect(repo_root=repo)


def _redact_wire(raw: dict, secrets: list[str], local: LocalIdentifiers) -> tuple[dict, dict, list]:
    """The one redaction path for both real and dry-run syncs. Local
    identifiers go first (paths -> repo-relative or ~/..., username -> <user>,
    hostname -> <host>), then secrets. Returns (scrubbed, wire, rewrites):
    `wire` is what gets POSTed; `scrubbed` is the text secrets were removed
    from, which the dry run aligns against `wire` to locate each marker."""
    scrubbed, rewrites = scrub_obj(raw, local)
    return scrubbed, redact_obj(scrubbed, known_secrets=secrets), rewrites


def run_sync(payload: dict, tool: str, transport: httpx.BaseTransport | None = None) -> int:
    """Sync one hook event. Returns the number of exchanges pushed."""
    cfg = config.load()
    if not cfg:
        raise NotJoinedError()
    adapter = get_adapter(tool)
    # Event-capture tools (e.g. Kiro) spool every hook event before we read.
    if type(adapter).capture is not Adapter.capture:
        adapter.capture(payload)
    session = adapter.session_from_hook(payload)
    cursor_file = _cursor_path(tool, session.session_id)
    cursor = _load_cursor(cursor_file)

    result = adapter.read_new(session, cursor)

    if not config.should_share(session.cwd, cfg):
        # Paused, or opt-in mode and this repo isn't shared: advance past the
        # content without uploading so it is skipped for good, never queued.
        if result.new_cursor != cursor:
            _save_cursor(cursor_file, result.new_cursor)
        return 0

    secrets = _known_secrets(cfg, session)
    local = _local_identifiers(session)
    wire_all = [_redact_wire(e.to_wire(), secrets, local)[1] for e in result.exchanges]

    failures, skipped = _load_state(cursor_file)

    # Exchanges already given up on are never resent, but they're still
    # "dealt with" as far as this read is concerned, so the cursor can still
    # advance past them.
    wire = [ex for ex in wire_all if ex["id"] not in skipped]
    if len(wire) != len(wire_all):
        _log_failure(f"{tool}: excluding {len(wire_all) - len(wire)} previously abandoned exchange(s).")

    blocked = False
    for i in range(0, len(wire), BATCH_SIZE):
        batch = wire[i : i + BATCH_SIZE]
        resp = api_client._post(
            cfg["api_url"], "/messages", {"exchanges": batch},
            api_key=cfg["api_key"], transport=transport,
        )
        accepted_ids = set(resp.get("ids", []))
        for ex in batch:
            if ex["id"] in accepted_ids:
                failures.pop(ex["id"], None)
                continue
            count = failures.get(ex["id"], 0) + 1
            if count >= MAX_EXCHANGE_FAILURES:
                _log_failure(
                    f"{tool}: giving up on exchange {ex['id']} after {count} failed push "
                    "attempts; skipping it so the rest of this session isn't stuck forever."
                )
                failures.pop(ex["id"], None)
                skipped.add(ex["id"])
            else:
                failures[ex["id"]] = count
                blocked = True

    _save_state(cursor_file, failures, skipped)

    if blocked:
        raise RaindropError("API did not confirm the full batch was stored.")
    if result.new_cursor != cursor:
        _save_cursor(cursor_file, result.new_cursor)
    return len(wire)


# ---------------------------------------------------------------------------
# Dry run: show what a sync would send, without sending or saving anything
# ---------------------------------------------------------------------------

_MARKER = re.compile(r"\[REDACTED(?::[a-z0-9-]+)?\]")
# Past this many markers in one string, skip span alignment (regex backtracking).
_MAX_ALIGNED_MARKERS = 200


@dataclass
class Redaction:
    field: str  # e.g. "user_text", "tool_summaries[2]"
    marker: str  # what replaced it, e.g. "[REDACTED:openai-key]" or "api/app.py"
    start: int | None  # char offsets into the original string; None if unknown
    end: int | None
    original: str | None
    # "secret": shown as a short hint only. "local": a path/username/hostname of
    # this machine, shown in full (it is being displayed to its owner).
    kind: str = "secret"
    count: int = 1  # identical local rewrites in one field are reported once


@dataclass
class DryRunExchange:
    wire: dict  # redacted, exactly what would be POSTed
    redactions: list[Redaction]
    abandoned: bool = False  # given up on earlier; a real sync won't resend it


@dataclass
class DryRunReport:
    tool: str
    session: SessionRef
    source: str  # "hook payload" or "latest session"
    repo: Path | None
    shared: bool
    skip_reason: str  # why the repo isn't shared ("" if it is)
    cursor: int
    new_cursor: int
    exchanges: list[DryRunExchange] = field(default_factory=list)


def redacted_spans(original: str, redacted: str, field_name: str = "") -> list[Redaction]:
    """Which parts of `original` became [REDACTED...] markers in `redacted`.

    redact() only returns the final text, so align the two: the redacted text
    is the original's kept pieces with markers between them, and a lazy regex
    over those pieces recovers each replaced span."""
    if original == redacted:
        return []
    pieces = _MARKER.split(redacted)
    markers = _MARKER.findall(redacted)
    m = None
    if len(markers) <= _MAX_ALIGNED_MARKERS:
        pattern = re.escape(pieces[0]) + "".join("(.+?)" + re.escape(p) for p in pieces[1:])
        m = re.fullmatch(pattern, original, re.DOTALL)
    if m is None:
        return [Redaction(field_name, mk, None, None, None) for mk in markers]
    return [
        Redaction(field_name, mk, m.start(i), m.end(i), m.group(i))
        for i, mk in enumerate(markers, 1)
        if m.group(i) != mk  # a marker that was already in the text
    ]


def _diff_redactions(before: object, after: object, path: str = "") -> list[Redaction]:
    """Walk an exchange before/after redact_obj and collect every redacted span."""
    if isinstance(before, str) and isinstance(after, str):
        return redacted_spans(before, after, path)
    if isinstance(before, dict) and isinstance(after, dict):
        return [r for k in before for r in _diff_redactions(before[k], after.get(k), k)]
    if isinstance(before, list) and isinstance(after, list):
        return [r for i, (b, a) in enumerate(zip(before, after)) for r in _diff_redactions(b, a, f"{path}[{i}]")]
    return []


def _local_redactions(rewrites: list) -> list[Redaction]:
    """Rewrite records from scrub_obj as Redactions, one per distinct
    (field, original, replacement) with a count."""
    merged: dict[tuple[str, str, str], Redaction] = {}
    for rw in rewrites:
        key = (rw.field, rw.original, rw.replacement)
        if key in merged:
            merged[key].count += 1
        else:
            merged[key] = Redaction(rw.field, rw.replacement, None, None, rw.original, kind="local")
    return list(merged.values())


def _skip_reason(cwd: str | None, cfg: dict) -> str:
    sharing = cfg.get("sharing", {})
    if sharing.get("paused"):
        return "sharing is paused everywhere (`raindrop resume` to resume)"
    if config.sharing_paused(cwd, cfg):
        return "sharing is paused for this repo (`raindrop resume --repo` to resume)"
    return "sharing is opt-in and this repo hasn't been shared (`raindrop share --repo` to share it)"


def run_dry_run(payload: dict | None, tool: str, cwd: str | Path | None = None) -> DryRunReport:
    """Everything run_sync would do up to the upload, with no writes at all:
    no capture/spooling, no push, no cursor or failure-state update.

    With no payload, previews the most recent `tool` session in the repo
    containing `cwd` (default: the current directory)."""
    cfg = config.load()
    if not cfg:
        raise NotJoinedError()
    adapter = get_adapter(tool)
    if payload is not None:
        session = adapter.session_from_hook(payload)
        source = "hook payload"
    else:
        repo = config.repo_root(cwd)
        found = adapter.latest_session(repo)
        if found is None:
            raise RaindropError(f"No {adapter.display_name} session found for {repo}.")
        session, source = found, "latest session"

    cursor_file = _cursor_path(tool, session.session_id)
    cursor = _load_cursor(cursor_file)
    result = adapter.read_new(session, cursor)
    shared = config.should_share(session.cwd, cfg)
    _, skipped = _load_state(cursor_file)

    secrets = _known_secrets(cfg, session)
    local = _local_identifiers(session)
    exchanges = []
    for ex in result.exchanges:
        scrubbed, wire, rewrites = _redact_wire(ex.to_wire(), secrets, local)
        exchanges.append(DryRunExchange(
            wire=wire,
            redactions=_local_redactions(rewrites) + _diff_redactions(dict(scrubbed), wire),
            abandoned=wire["id"] in skipped,
        ))
    return DryRunReport(
        tool=tool,
        session=session,
        source=source,
        repo=config.repo_root(session.cwd) if session.cwd else None,
        shared=shared,
        skip_reason="" if shared else _skip_reason(session.cwd, cfg),
        cursor=cursor,
        new_cursor=result.new_cursor,
        exchanges=exchanges,
    )


def _preview(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _hint(secret: str) -> str:
    """Enough of a redacted value to recognize a false positive, not enough to leak it."""
    shown = " ".join(secret.split())[:4]
    return f'"{shown}…" ({len(secret)} chars)'


def format_dry_run(report: DryRunReport) -> str:
    s = report.session
    out = [f"raindrop sync --dry-run ({report.tool}, {report.source})"]
    out.append(f"  Session:    {s.session_id}")
    if s.transcript_path:
        out.append(f"  Transcript: {s.transcript_path}")
    out.append(f"  Repo:       {report.repo or '(unknown)'}")
    out.append(f"  Cursor:     {report.cursor} -> {report.new_cursor} (not saved)")
    out.append("")

    n = len(report.exchanges)
    if not report.shared:
        out.append(f"This repo isn't shared: {report.skip_reason}.")
        if n:
            what = "this exchange" if n == 1 else f"these {n} exchanges"
            out.append(f"A real sync would skip {what} for good "
                       "(cursor advances, nothing is uploaded):")
        else:
            out.append("No new exchanges to skip.")
    elif not n:
        out.append("No new exchanges since the last sync.")
    else:
        sending = sum(not e.abandoned for e in report.exchanges)
        out.append(f"A real sync would upload {sending} exchange{'' if sending == 1 else 's'}:")

    for e in report.exchanges:
        w = e.wire
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(w["ts"]))
        tag = "  (previously abandoned; won't be resent)" if e.abandoned else ""
        out.append("")
        out.append(f"[{w['id']}] {when}{tag}")
        out.append(f"  Prompt: {_preview(w['user_text'], 500)}")
        out.append(f"  Reply:  {_preview(w['assistant_text'], 240)}")
        out.append(f"  Files:  {', '.join(w['files_touched']) or '(none)'}")
        if e.redactions:
            out.append(f"  Redacted ({len(e.redactions)}):")
            for r in e.redactions:
                if r.kind == "local":
                    times = f" (x{r.count})" if r.count > 1 else ""
                    out.append(f"    - {r.field}: {r.original} -> {r.marker}{times}")
                    continue
                where = f" chars {r.start}-{r.end}" if r.start is not None else ""
                what = f"{_hint(r.original)} -> " if r.original is not None else ""
                out.append(f"    - {r.field}{where}: {what}{r.marker}")
        else:
            out.append("  Redacted: nothing")

    out.append("")
    out.append("Dry run: nothing was uploaded and the cursor was not advanced.")
    return "\n".join(out)


def _log_failure(message: str) -> None:
    try:
        config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with (config.CONFIG_DIR / "sync.log").open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def _read_optional_payload() -> dict | None:
    """Hook payload from stdin for --dry-run, or None if stdin is a terminal or empty."""
    if sys.stdin is None or sys.stdin.isatty():
        return None
    text = sys.stdin.read()
    if not text.strip():
        return None
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise RaindropError("Hook input must be a JSON object.")
    return payload

def _hook_reply(tool: str) -> None:
    """Print the tool's required hook answer, if it has one (see Adapter.hook_stdout)."""
    try:
        reply = get_adapter(tool).hook_stdout()
    except Exception:
        return
    if reply:
        typer.echo(reply)


def sync(
    tool: str = typer.Option("claude-code", "--tool", help="Which tool's hook is calling."),
    fail_open: bool = typer.Option(
        False, "--fail-open",
        help="Always exit 0 and log errors to ~/.raindrop/sync.log. Needed for hooks "
             "that run before a prompt or tool call, where a non-zero exit blocks the tool.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Show what would be synced (after redaction) without uploading or advancing "
             "the cursor. With no hook payload on stdin, previews the most recent --tool "
             "session in the current repo.",
    ),
):
    """Sync new exchanges to your team. Reads the hook's JSON payload from stdin.

    Output goes to stderr only (some tools add a hook's stdout to the agent's context), except
    for the answer a tool requires on stdout (Adapter.hook_stdout).
    """
    if dry_run:
        try:
            report = run_dry_run(_read_optional_payload(), tool)
        except (RaindropError, json.JSONDecodeError, OSError) as e:
            typer.secho(f"raindrop sync --dry-run failed: {e}", fg="red", err=True)
            raise typer.Exit(1)
        typer.echo(format_dry_run(report))
        return
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise RaindropError("Hook input must be a JSON object.")
        n = run_sync(payload, tool)
    except (api_client.ApiError, RaindropError, json.JSONDecodeError, OSError) as e:
        message = getattr(e, "message", None) or str(e)
        _log_failure(f"{tool}: {message}")
        typer.secho(f"raindrop sync failed: {message}", fg="red", err=True)
        if fail_open:
            _hook_reply(tool)
        raise typer.Exit(0 if fail_open else 1)
    _hook_reply(tool)
    if n:
        typer.echo(f"raindrop synced {n} exchange{'' if n == 1 else 's'}.", err=True)
