# Codex Stop hook (isolated prototype)

This prototype syncs a completed Codex turn to the Raindrop `/messages` API.
It intentionally lives outside `teamctx-cli`; it imports that directory's
existing config, API client, redactor, and `Exchange` model without modifying
any files there.

The shared exchange is the shape in `teamctx.models.Exchange`:

- session ID, exchange index, and timestamp
- user and final assistant text
- tool names and repository-relative file paths

The server adds the authenticated team and agent identity when it stores each
exchange. Local cursor state stays in `~/.raindrop/cursors/codex/` and is
advanced only after the API confirms the write. Retries therefore resend the
same exchange ID, which the API stores idempotently.

## Requirements

- Python 3.10+
- `teamctx-cli` alongside this folder in the Raindrop repository
- A joined Raindrop team (`raindrop join ...`)
- Codex with hooks enabled and an accessible `~/.codex/sessions` transcript

## Configure the hook

Add a `Stop` command hook to `~/.codex/hooks.json` (or merge this event into an
existing hooks file). Replace the script path with the absolute path to this
repository on your machine:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "commandWindows": "py -3 \"C:\\path\\to\\raindrop\\codex-stop-hook\\stop_sync.py\"",
            "command": "python3 \"/path/to/raindrop/codex-stop-hook/stop_sync.py\"",
            "timeout": 30,
            "statusMessage": "Syncing Codex turn to Raindrop"
          }
        ]
      }
    ]
  }
}
```

Codex requires hooks to be reviewed/trusted before they run. Check the hook in
Codex's `/hooks` browser after adding it. The hook reads its event JSON from
stdin and does not send anything if it cannot identify and parse a completed
exchange.

## Tests

From this directory:

```powershell
py -3 -m unittest -v
```

This is a prototype against the current local Raindrop interfaces. Confirm the
Codex hook payload and transcript format for the installed Codex version
before integrating it into the CLI adapter registry.
