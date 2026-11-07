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

- The transcript parser was checked against a real Antigravity transcript (record
  shapes, prompt wrapper, reply and tool steps).
- The hook configuration format comes from Antigravity's hooks documentation and
  has not been run inside Antigravity. Check `/hooks` in the `agy` CLI, or
  **Customizations > Hooks** in the IDE, to confirm it loads.
- The docs are unclear on whether `transcriptPath` is the transcript file or the
  conversation folder, so the adapter accepts either and falls back to the known
  location under `~/.gemini/antigravity*/brain/`.
- A user report on Google's developer forum says hooks in plugins did not fire in
  IDE 2.1.1 but did in the CLI. If nothing syncs from the IDE, that is the first
  thing to check.
- If the agent is still running background tasks when it stops, the final text of
  that turn may arrive after the sync. It would be missed.

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
