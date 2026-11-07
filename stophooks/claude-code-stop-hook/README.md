# Claude Code Stop hook

`claude_stop_sync.py` syncs each finished Claude Code turn to Raindrop. It is a
standalone script, like the Codex and Cursor hooks. `raindrop tools add
claude-code` no longer installs a hook; it only registers the MCP server (and
the optional rules block). Add this hook yourself as described below.

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

## Removing an older hook

Earlier versions of `raindrop tools add claude-code` installed a hook that ran
`raindrop sync --tool claude-code`. `raindrop tools remove claude-code` still
removes that older hook. If you installed both, remove one so turns are not
synced twice (the API is idempotent, so a duplicate would not create duplicate
data, but it wastes a request).

## Tests

From the repository root:

```bash
python -m unittest discover -s stophooks/claude-code-stop-hook -p "test_*.py" -v
```
