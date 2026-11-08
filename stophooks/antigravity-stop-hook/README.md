# Antigravity Stop hook

`antigravity_stop_sync.py` syncs each finished Google Antigravity turn to
Raindrop. It is a standalone script, like the other hooks in this folder.

Antigravity (the App, the IDE and the `agy` CLI share these files) runs it when
the agent stops and passes the event as JSON on stdin (`conversationId`,
`transcriptPath`, `workspacePaths`, ...). The script reuses the CLI's sync
pipeline (`raindrop.sync_cmd.run_sync`):

- reads only the new part of the conversation transcript
  (`~/.gemini/antigravity*/brain/<conversationId>/.system_generated/logs/transcript.jsonl`),
  tracked by a cursor in `~/.raindrop/cursors/antigravity/<conversationId>`
- turns each `USER_INPUT` step into a prompt and the last `PLANNER_RESPONSE` text
  after the final tool step into the reply. The model's `thinking` is never shared,
  and neither are tool outputs.
- holds back a prompt that has no reply yet
- redacts secrets locally (`raindrop/redact.py`) before anything leaves the machine
- uploads, and advances the cursor only after the API confirms

## It always answers "allow"

A Stop hook must answer on stdout with a JSON decision, and `"continue"` keeps the
agent running. The script always prints `{"decision": "allow"}` and exits 0,
whether the sync worked or not. Errors go to stderr and `~/.raindrop/sync.log`.

## Two ways to install

Use one, not both:

- **Installed by the CLI (recommended):** `raindrop tools add antigravity`
  registers the MCP server and adds the Stop hook to `~/.gemini/config/hooks.json`
  (running `raindrop sync --tool antigravity --fail-open`). Nothing else to configure.
- **This script:** add it to `~/.gemini/config/hooks.json` yourself (merge it with
  any hooks you already have). Replace the path with this repository's absolute path:

```json
{
  "raindrop": {
    "Stop": [
      {
        "type": "command",
        "command": "python \"C:/path/to/raindrop/stophooks/antigravity-stop-hook/antigravity_stop_sync.py\"",
        "timeout": 60
      }
    ]
  }
}
```

If you ran `raindrop tools add antigravity`, skip this script. With both, every
turn is synced twice (the API is idempotent, so no duplicate data is stored, but
it wastes a request). `raindrop tools remove antigravity` removes the CLI's hook.

## What was verified, and what wasn't

Verified in the real `agy` CLI (Antigravity's own log shows it loading and running the hook, and
the script ran on the real payload):

- **The hook file format.** A `~/.gemini/config/hooks.json` shaped `{"<name>": {"Stop": [{"type":
  "command", "command": ..., "timeout": ...}]}}` is loaded ("loaded 1 named hooks").
- **The Stop payload.** Fields are `conversationId`, `transcriptPath`, `workspacePaths`,
  `lastUserInput`, `modelName`, `terminationReason` (`NO_TOOL_CALL` for a plain reply),
  `executionNum`, `fullyIdle`, `error` and `artifactDirectoryPath`.
- **`transcriptPath` is a file**, and the one Antigravity sends is
  `.../brain/<conversationId>/.system_generated/logs/transcript_full.jsonl` (not `transcript.jsonl`).
  Both have the same record shape. The adapter accepts the file, the conversation folder, or finds it
  by `conversationId`.
- **The answer.** The script printed `{"decision": "allow"}`, exited 0, and one real `pong` turn became
  exactly one exchange (prompt `pong`, the reply, no tools).

Things to know:

- **Antigravity splits the command on spaces and does not process quotes.** A quoted path reaches the
  program with the quote characters still in it (this broke my first test hook), and the command runs
  from `~/.gemini/config`. So a program path containing a space cannot work. On Windows the installer
  uses the 8.3 short form of the path when it has a space; elsewhere install the CLI somewhere
  without spaces in the path.
- **IDE not tested.** This was the `agy` CLI. A user report on Google's developer forum says hooks in
  plugins did not fire in IDE 2.1.1 but did in the CLI. If nothing syncs from the IDE, check that first.
- If the agent is still running background tasks when it stops (`fullyIdle` is false), the final text of
  that turn may arrive after the sync and would be missed.

## Requirements

- Python 3.10+ and the `raindrop-cli` package installed (`pip install -e raindrop-cli`)
- A joined team (`raindrop join ...`)
- Sharing enabled for the repo: by default Raindrop uploads only from repos you
  opted in with `raindrop share --repo`

## Tests

From the repository root:

```bash
python -m unittest discover -s stophooks/antigravity-stop-hook -p "test_*.py" -v
python -m pytest raindrop-cli/tests/test_antigravity_sync.py -v
```
