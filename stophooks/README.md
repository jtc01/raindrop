# Raindrop stop hooks

The hooks are grouped here, but kept as separate scripts because Codex and
Cursor provide different hook payloads and have different behavior:

- `claude-code-stop-hook/claude_stop_sync.py` syncs finished Claude Code turns
  through the CLI's sync pipeline (redaction included). `raindrop tools add
  claude-code` can also install a hook for you; see that folder's README.
- `codex-stop-hook/stop_sync.py` reads Codex transcript data, redacts it, and
  uploads completed exchanges to Raindrop.
- `cursor-stop-hook/capture.py` currently records only the structure of
  Cursor's hook payload/transcript locally. It does not store prompt text or
  upload anything yet; we need a real Cursor diagnostic report before writing
  a transcript parser.

The Codex and Cursor **adapters** (`raindrop.adapters.codex` and
`raindrop.adapters.cursor`) now live inside the installed `raindrop` package in
`raindrop-cli/raindrop/adapters/`, and are registered in the CLI, so a plain
`pip install` includes them. What stays in this folder are the standalone
scripts below; they are not part of the installed package and import it from the
repo checkout. Neither hook is configured automatically.

## 1. Run automated tests

From the Raindrop repository root in PowerShell:

```powershell
py -3 -m unittest discover -s stophooks/codex-stop-hook -p "test_*.py" -v
py -3 -m unittest discover -s stophooks/cursor-stop-hook -p "test_*.py" -v
py -3 -m pytest raindrop-cli/tests/test_codex_cursor_adapters.py -v
```

Each should finish with `OK` (or all tests passed). These are local tests and do not upload any
conversation data.

## 2. Test Cursor locally

1. Follow `cursor-stop-hook/README.md` to add its `capture.py` command to
   `%USERPROFILE%\.cursor\hooks.json`. If that file already has hooks, merge
   the `"stop"` entry into it; do not replace the rest of the file.
2. Make a harmless Cursor Agent turn, such as asking it to explain a small
   function. Do not include secrets or private project data.
3. Check that a new report was written:

   ```powershell
   Get-ChildItem "$HOME\.raindrop\cursor-hook-diagnostics"
   ```

4. Open the newest report and share it for review if you want help implementing
   Cursor's exchange parser. The report is designed to omit chat text.

If there is no report, verify the script path in `hooks.json` and confirm
Cursor reloaded the hooks configuration.

## 3. Test Codex sync only after unit tests pass

1. Follow `codex-stop-hook/README.md` to add the `Stop` command to
   `~/.codex/hooks.json`; merge it with existing hooks rather than replacing
   the file.
2. Confirm you have joined the intended team with `raindrop whoami`.
3. Make one harmless Codex turn. The Codex hook uploads its prompt and final
   answer, so avoid sensitive content even though the hook redacts recognizable
   secrets.
4. A successful hook prints `Raindrop synced 1 exchange.` (or more than one).
   A failure prints an error. Do not treat the hook as verified until you can
   see the exchange in the team's available viewer/dashboard.

Cursor's diagnostic hook is not a Cursor sync implementation. Do not expect
Cursor conversations to appear in Raindrop yet. The Cursor adapter is a
testable prototype; it is registered in the CLI but still needs real hook-payload
validation before live sync can be tested.
