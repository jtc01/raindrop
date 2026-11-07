# Claude Code Stop hook

`claude_stop_sync.py` syncs each finished Claude Code turn to Raindrop. It is a
standalone script, like the Codex and Cursor hooks.

There are two ways to get a Claude Code Stop hook. Use one, not both:

- **Installed by the CLI (recommended):** `raindrop tools add claude-code`
  registers the MCP server and adds a Stop hook that runs
  `raindrop sync --tool claude-code`. Nothing else to configure.
- **This script:** add it to `~/.claude/settings.json` yourself, as described
  below. Use it if you want to run the hook straight from the repo checkout.

The script reuses the Raindrop CLI's sync pipeline (`raindrop.sync_cmd.run_sync`):

- reads only the new part of the session transcript, tracked by a cursor in
  `~/.raindrop/cursors/claude-code/<session_id>`
- holds back a prompt that has no reply yet
- redacts secrets locally (`raindrop/redact.py`) before anything leaves the machine
- uploads in batches and advances the cursor only after the API confirms

## Requirements

- Python 3.10+ and the `raindrop-cli` package installed (`pip install -e raindrop-cli`)
- A joined team (`raindrop join ...`)

## Configure

Merge a `Stop` hook into `~/.claude/settings.json`, keeping any hooks you
already have. Replace the path with this repository's absolute path:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python \"C:/path/to/raindrop/stophooks/claude-code-stop-hook/claude_stop_sync.py\"",
            "timeout": 60
          }
        ]
      }
    ]
  }
}
```

On success the script prints `Raindrop synced N exchange(s).` to stderr. On
failure it prints the error and exits 1.

## Don't install both

If you ran `raindrop tools add claude-code`, the CLI already added a hook, so
skip this script. With both, every turn is synced twice. The API is idempotent,
so no duplicate data is stored, but it wastes a request. `raindrop tools remove
claude-code` removes the CLI's hook.

## Tests

From the repository root:

```bash
python -m unittest discover -s stophooks/claude-code-stop-hook -p "test_*.py" -v
```
