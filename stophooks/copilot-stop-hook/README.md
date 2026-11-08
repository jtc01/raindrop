# GitHub Copilot Stop hook

`copilot_stop_sync.py` syncs each finished GitHub Copilot turn to Raindrop. It is
a standalone script, like the other hooks in this folder. VS Code, the Copilot
app and the Copilot CLI share one hooks format and one user-level hooks folder
(`~/.copilot/hooks/`).

Copilot runs it when the agent stops (the `agentStop` event) and passes the event
as JSON on stdin (`sessionId`, `cwd`, `transcriptPath`, `stopReason`, ...). The
script reuses the CLI's sync pipeline (`raindrop.sync_cmd.run_sync`):

- reads only the new part of the chat transcript
  (VS Code: `<VS Code user dir>/workspaceStorage/<id>/GitHub.copilot-chat/transcripts/<sessionId>.jsonl`),
  tracked by a cursor in `~/.raindrop/cursors/copilot/<sessionId>`. If the hook gives
  no transcript path, or a stale one, the transcript is found by session ID.
- turns each `user.message` event into a prompt, and the `assistant.message` text after
  the last tool call into the reply. Reasoning text and tool output are never shared.
- holds back a prompt that has no reply yet
- redacts secrets locally (`raindrop/redact.py`) before anything leaves the machine
- uploads, and advances the cursor only after the API confirms

Copilot parses a hook's stdout as JSON, so stdout stays empty. The script always
exits 0 so a sync problem can never disturb the agent; errors go to stderr and
`~/.raindrop/sync.log`.

## Two ways to install

Use one, not both:

- **Installed by the CLI (recommended):** `raindrop tools add copilot` registers the
  MCP server and writes `~/.copilot/hooks/raindrop.json` with an `agentStop` hook
  running `raindrop sync --tool copilot --fail-open`. Nothing else to configure.
- **This script:** create a hook file in `~/.copilot/hooks/` yourself (on Windows
  `%USERPROFILE%\.copilot\hooks\`, or `$COPILOT_HOME/hooks/`). Replace the path with
  this repository's absolute path:

```json
{
  "version": 1,
  "hooks": {
    "agentStop": [
      {
        "type": "command",
        "command": "python \"C:/path/to/raindrop/stophooks/copilot-stop-hook/copilot_stop_sync.py\"",
        "timeoutSec": 60
      }
    ]
  }
}
```

If you ran `raindrop tools add copilot`, skip this script. With both, every turn is
synced twice (the API is idempotent, so no duplicate data is stored, but it wastes a
request). `raindrop tools remove copilot` removes the CLI's hook file.

## What was verified, and what wasn't

- The transcript parser was checked against real VS Code Copilot Chat transcripts (event types,
  prompt, reply and tool events, and finding a transcript by session ID).
- **Copilot loaded the hook.** A hook file in `~/.copilot/hooks/` shows up under **Hooks** in Copilot's
  Agent Customizations screen in VS Code, so the folder and file format are right.
- **Copilot's own engine keeps sessions in `~/.copilot/session-state/<sessionId>/events.jsonl`**, in the
  same event style (`user.message`, `assistant.message`, `tool.execution_start`, ...) plus `hook.start`
  and `hook.end` events recording every hook it ran and the payload it sent. The parser reads this too
  and ignores the hook and system events. A real session that failed before any reply correctly
  produces nothing to upload.
- **Not yet seen: `agentStop` firing on a finished turn.** `agentStop` only fires when a turn completes,
  and the test account has no usable model (Copilot says "No model available. Check policy enablement
  under GitHub Settings > Copilot"), so no turn has finished. After a successful turn, the `hook.start`
  event for `agentStop` in that session's `events.jsonl` shows the exact payload Copilot sends, even
  without the capture script.
- The hook file format and the `agentStop` payload fields otherwise come from the Copilot hooks
  documentation.

## Checking what Copilot actually sends

`copilot_capture.py` is a diagnostic. It records only the *structure* of one real
`agentStop` payload and its transcript (field names, types, string lengths; never text
or paths) to `~/.raindrop/copilot-hook-diagnostics/`, and uploads nothing. Add it as
a second `agentStop` hook the same way as above, run one harmless turn, and read the
report. Do not leave it installed alongside the sync hook longer than needed.

## Requirements

- Python 3.10+ and the `raindrop-cli` package installed (`pip install -e raindrop-cli`)
- A joined team (`raindrop join ...`)
- Sharing enabled for the repo: by default Raindrop uploads only from repos you opted
  in with `raindrop share --repo`

## Tests

From the repository root:

```bash
python -m unittest discover -s stophooks/copilot-stop-hook -p "test_*.py" -v
python -m pytest raindrop-cli/tests/test_copilot_sync.py -v
```
