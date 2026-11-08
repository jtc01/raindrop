# Cursor hooks

`cursor_stop_sync.py` syncs Cursor turns to Raindrop. It is a standalone script, like
the other hooks in this folder. (`capture.py`, described at the bottom, is a separate
structure-only diagnostic.)

Cursor passes each hook event as JSON on stdin (`conversation_id`, `hook_event_name`,
`workspace_roots`, plus `prompt`, `text`, `tool_name` or `tool_input` depending on the
event). Cursor has no readable chat transcript the script can rely on, so Raindrop builds
the exchange from the events. One script handles four Cursor hooks:

| Cursor hook | What it records |
|---|---|
| `beforeSubmitPrompt` | the prompt |
| `postToolUse` | tool names and file paths (not tool output) |
| `afterAgentResponse` | the agent's reply; the turn is uploaded once a reply arrives |
| `stop` | recorded, adds nothing once the reply has been uploaded |

Events are spooled to `~/.raindrop/spools/cursor/<conversation_id>.jsonl`. The upload goes
through the CLI's own pipeline (`raindrop.sync_cmd.run_sync`): secrets are redacted locally
first, only repos you opted in with `raindrop share --repo` upload, and the position advances
only after the API confirms.

The script always exits 0 and keeps stdout empty. In Cursor, exit code 2 blocks the user's
action and a crashing hook can interrupt a prompt; errors go to stderr and
`~/.raindrop/sync.log` instead.

## Two ways to install

Use one, not both:

- **Installed by the CLI (recommended):** `raindrop tools add cursor` registers the MCP server
  in `~/.cursor/mcp.json` and adds the four hooks to `~/.cursor/hooks.json`, each running
  `raindrop sync --tool cursor`. Cursor has no global rules file, so `--rules` does nothing.
- **This script:** merge the four hooks into `~/.cursor/hooks.json` yourself (keep any hooks
  you already have). Replace the path with this repository's absolute path:

```json
{
  "version": 1,
  "hooks": {
    "beforeSubmitPrompt": [{ "command": "python \"C:/path/to/raindrop/stophooks/cursor-stop-hook/cursor_stop_sync.py\"", "timeout": 60 }],
    "postToolUse":        [{ "command": "python \"C:/path/to/raindrop/stophooks/cursor-stop-hook/cursor_stop_sync.py\"", "timeout": 60 }],
    "afterAgentResponse": [{ "command": "python \"C:/path/to/raindrop/stophooks/cursor-stop-hook/cursor_stop_sync.py\"", "timeout": 60 }],
    "stop":               [{ "command": "python \"C:/path/to/raindrop/stophooks/cursor-stop-hook/cursor_stop_sync.py\"", "timeout": 60 }]
  }
}
```

If you ran `raindrop tools add cursor`, skip this script: with both, every event is recorded
twice. `raindrop tools remove cursor` removes the CLI's entries.

## What was verified, and what wasn't

- The field names the adapter reads (`conversation_id`, `workspace_roots`, `prompt`, `text`,
  `tool_name`, `tool_input`), the `version: 1` hooks file and the timeout in seconds were
  checked against Cursor's hooks documentation.
- It has **not** been run in Cursor. Cursor is not installed on the machine this was written
  on, so no real hook payload has been seen.
- **Open question: replies.** The adapter treats a turn as finished as soon as the first
  `afterAgentResponse` arrives. If Cursor sends a reply before each tool call (one per
  assistant message), the uploaded "reply" would be the first message and the final answer would
  be missed, because the position has already moved on. Reports differ on whether it fires once
  per turn or once per message, and one says it does not fire at all in headless mode. If a real
  session shows several replies per turn, finish the turn at `stop` instead (using the last
  reply). `stop` is the reliable once-per-turn event.

## Requirements

- Python 3.10+ and the `raindrop-cli` package installed (`pip install -e raindrop-cli`)
- A joined team (`raindrop join ...`)
- Sharing enabled for the repo (`raindrop share --repo`)

## Checking what Cursor actually sends: `capture.py`

`capture.py` is a diagnostic. It records only the **structure** of one real `stop` payload
and its transcript (field names, types, string lengths and a few categorical values; never
text or file paths) to `%USERPROFILE%\.raindrop\cursor-hook-diagnostics\`, and uploads
nothing. To use it, add it as a `stop` hook in `~/.cursor/hooks.json`:

```json
{
  "version": 1,
  "hooks": {
    "stop": [{ "command": "py -3 \"C:\\path\\to\\raindrop\\stophooks\\cursor-stop-hook\\capture.py\"", "timeout": 30 }]
  }
}
```

Run one harmless Cursor Agent turn, then read the newest report. If you share a report, review
it first. Do not leave it installed alongside the sync hooks longer than needed.

## Tests

From the repository root:

```bash
python -m unittest discover -s stophooks/cursor-stop-hook -p "test_*.py" -v
python -m pytest raindrop-cli/tests/test_codex_cursor_adapters.py raindrop-cli/tests/test_codex_cursor_mcp.py -v
```
