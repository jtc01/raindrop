# Cursor Stop hook schema capture

This is the first, local-only step toward syncing Cursor turns to Raindrop.
Cursor's [Hooks documentation](https://cursor.com/docs/hooks) describes the
`stop` event and transcript path, but the transcript format needs to be
confirmed against an actual Cursor session before implementing an exchange
parser. This hook captures the payload and transcript **structure** only; it
does not save prompt or response text and does not upload data.

The structural report includes field names, value types, string lengths,
selected categorical values (such as `role`, `type`, and `phase`), and the
shapes of up to 20 transcript records. It omits arbitrary string values and
file paths. A short hash is used as the report filename.

## Configure

Add a Stop hook to your user-level Cursor hooks file at
`%USERPROFILE%\.cursor\hooks.json`. Merge the following into existing JSON if
you already have hooks configured. Replace the script path with this
repository's absolute path:

```json
{
  "version": 1,
  "hooks": {
    "stop": [
      {
        "command": "py -3 \"C:\\path\\to\\raindrop\\cursor-stop-hook\\capture.py\"",
        "timeout": 30
      }
    ]
  }
}
```

Cursor reloads hooks when the config changes. After one Cursor Agent turn ends,
inspect the local report under
`%USERPROFILE%\\.raindrop\\cursor-hook-diagnostics\\`. The report contains
structure only, not conversation text. Do not share full transcripts; if you
share the generated report, review it first.

## Next step

Use the captured structure to confirm how the transcript represents prompt,
final assistant response, tool activity, and file changes. Then implement and
test a mapper to the shared `Exchange` fields (`session_id`, `idx`, `ts`,
`user_text`, `assistant_text`, `tool_summaries`, and `files_touched`) before
enabling uploads.

This folder does not modify `teamctx-cli` or install the hook automatically.

## Tests

From the repository root:

```powershell
py -3 -m unittest discover -s cursor-stop-hook -p "test_*.py" -v
```
