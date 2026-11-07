# Codex Stop hook (isolated prototype)

This prototype syncs a completed Codex turn to the Raindrop `/messages` API.
It intentionally lives outside `raindrop-cli`; it imports that directory's
existing config, API client, redactor, and `Exchange` model without modifying
any files there.

The shared exchange is the shape in `raindrop.models.Exchange`:

- session ID, exchange index, and timestamp
- user and final assistant text
- tool names and repository-relative file paths

The server adds the authenticated team and agent identity when it stores each
exchange. Local cursor state stays in `~/.raindrop/cursors/codex/` and is
advanced only after the API confirms the write. Retries therefore resend the
same exchange ID, which the API stores idempotently.

## Requirements

- Python 3.10+
- `raindrop-cli` alongside the `stophooks` folder in the Raindrop repository
- A joined Raindrop team (`raindrop join ...`)
- Sharing enabled for the repo: by default Raindrop uploads only from repos you opted in with
  `raindrop share --repo` (or every repo after `raindrop share`), and nothing uploads after
  `raindrop pause`. A skipped turn is passed over for good, not queued for later.
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
            "commandWindows": "py -3 \"C:\\path\\to\\raindrop\\stophooks\\codex-stop-hook\\stop_sync.py\"",
            "command": "python3 \"/path/to/raindrop/stophooks/codex-stop-hook/stop_sync.py\"",
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

From the repository root:

```powershell
py -3 -m unittest discover -s stophooks/codex-stop-hook -p "test_*.py" -v
```

This is a prototype against the current local Raindrop interfaces. Confirm the
Codex hook payload and transcript format for the installed Codex version
before integrating it into the CLI adapter registry.
