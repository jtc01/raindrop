# Kiro hook

`kiro_stop_sync.py` syncs Kiro turns to Raindrop. It is a standalone script, like
the Claude Code, Codex and Cursor hooks.

Kiro keeps its chat history in an undocumented database, so Raindrop builds
exchanges from hook events instead of reading a transcript. One script handles
three Kiro hooks:

| Kiro trigger | What it records |
|---|---|
| `UserPromptSubmit` | the prompt |
| `PreToolUse` | tool names and file paths (not tool output) |
| `Stop` | the final reply, then uploads the completed turn |

Events are spooled to `~/.raindrop/spool/kiro/<session_id>.jsonl`. Nothing is
uploaded until `Stop`. The upload goes through the CLI's own pipeline
(`raindrop.sync_cmd.run_sync`): secrets are redacted locally first, and the cursor
advances only after the API confirms.

## Fails open

Two of the three hooks run *before* Kiro acts, and a non-zero exit there blocks
the prompt or tool call. So the script always exits 0. Errors are printed to
stderr and appended to `~/.raindrop/sync.log`.

## Two ways to install

Use one, not both:

- **Installed by the CLI (recommended):** `raindrop tools add kiro` adds the MCP
  server and writes `~/.kiro/hooks/raindrop.json` with all three hooks, running
  `raindrop sync --tool kiro --fail-open`. Nothing else to configure.
- **This script:** create `~/.kiro/hooks/raindrop.json` yourself, with every
  hook pointing at the script. Replace the path with this repository's absolute
  path:

```json
{
  "version": "v1",
  "hooks": [
    {
      "name": "raindrop-userpromptsubmit",
      "description": "Record your prompt so the finished turn can be shared with your team",
      "trigger": "UserPromptSubmit",
      "action": {"type": "command", "command": "python \"C:/path/to/raindrop/stophooks/kiro-stop-hook/kiro_stop_sync.py\""},
      "timeout": 30
    },
    {
      "name": "raindrop-pretooluse",
      "description": "Record tool use (names and file paths only) for team context",
      "trigger": "PreToolUse",
      "action": {"type": "command", "command": "python \"C:/path/to/raindrop/stophooks/kiro-stop-hook/kiro_stop_sync.py\""},
      "timeout": 30
    },
    {
      "name": "raindrop-stop",
      "description": "Sync the finished turn to your team",
      "trigger": "Stop",
      "action": {"type": "command", "command": "python \"C:/path/to/raindrop/stophooks/kiro-stop-hook/kiro_stop_sync.py\""},
      "timeout": 30
    }
  ]
}
```

If you ran `raindrop tools add kiro`, skip this script. With both, every event is
recorded twice and the prompt and tool records would be duplicated.
`raindrop tools remove kiro` removes the CLI's hooks.

## Requirements

- Python 3.10+ and the `raindrop-cli` package installed (`pip install -e raindrop-cli`)
- A joined team (`raindrop join ...`)
- Sharing enabled for the repo: by default Raindrop uploads only from repos you
  opted in with `raindrop share --repo`

## Tests

From the repository root:

```bash
python -m unittest discover -s stophooks/kiro-stop-hook -p "test_*.py" -v
```
