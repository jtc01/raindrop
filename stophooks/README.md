# Raindrop stop hooks

Every tool has the same two parts:

- an **adapter** inside the installed `raindrop` package (`raindrop-cli/raindrop/adapters/`),
  which `raindrop tools add <tool>` uses to install the tool's hook, and which `raindrop sync`
  uses to read the finished turn; and
- a **standalone script** in this folder, for running the hook straight from a repo checkout
  without installing anything into the tool. The scripts are not part of the installed package.

Use one or the other for a given tool, not both, or every turn is synced twice.

| Tool | Install with the CLI (hook + MCP server) | Standalone script | Where the turn comes from |
|---|---|---|---|
| Claude Code | `raindrop tools add claude-code` | `claude-code-stop-hook/claude_stop_sync.py` | transcript file |
| Kiro | `raindrop tools add kiro` | `kiro-stop-hook/kiro_stop_sync.py` | hook events spooled locally |
| Antigravity | `raindrop tools add antigravity` | `antigravity-stop-hook/antigravity_stop_sync.py` | transcript file |
| GitHub Copilot | `raindrop tools add copilot` | `copilot-stop-hook/copilot_stop_sync.py` | transcript file |
| Codex | `raindrop tools add codex` | `codex-stop-hook/stop_sync.py` | transcript file |
| Cursor | `raindrop tools add cursor` | `cursor-stop-hook/cursor_stop_sync.py` | hook events spooled locally |

How the hooks behave (the scripts for Claude Code, Kiro, Antigravity, Copilot and Cursor all go through
`raindrop.sync_cmd.run_sync`, so they behave the same way; the Codex script repeats the same rules in
its own code):

- Secrets are redacted locally before anything is uploaded.
- Only repos you opted in with `raindrop share --repo` upload (or all, after `raindrop share`);
  `raindrop pause` stops everything. Skipped content is never queued for later.
- The sync position only advances after the API confirms the upload, so a failed upload is retried.
- A hook never breaks the agent. The Kiro, Antigravity, Copilot and Cursor scripts always exit 0 and
  log problems to `~/.raindrop/sync.log`. Antigravity's Stop hook must also answer `{"decision": "allow"}`.

Verification status:

| Tool | Parser/adapter checked against | Hook seen firing in the real app |
|---|---|---|
| Claude Code | real transcripts; a manual run of the installed hook uploaded a real session | yes (its cursor advances after real turns) |
| Antigravity | a real transcript and a real Stop payload | **yes**, in the `agy` CLI (the hook ran and one real turn became one exchange); the IDE is untested |
| GitHub Copilot | real VS Code Copilot Chat transcripts, and a real session from Copilot's own store | partly: Copilot loaded the hook (shown under Hooks in its settings), but no turn has finished because the test account has no usable model |
| Kiro | synthetic events in the tests | not yet (app not installed on the dev machine) |
| Codex | synthetic transcripts in the tests | not yet (app not installed on the dev machine) |
| Cursor | Cursor's hooks documentation (field names, file format, timeout) | not yet (app not installed on the dev machine) |

For all six, the real `raindrop tools add` was also run into an isolated fake home, and the exact hook
command line it wrote was executed through the shell with a realistic payload (your real Claude Code,
Antigravity and Copilot transcripts where they exist). With default sharing every tool parsed its turn,
uploaded nothing and moved its position; with sharing on and the API unreachable every tool reached the
upload step, failed there and did not move its position. That checks the command, its quoting, the
parsing and the failure path, but not that the app actually calls the hook.

Known gaps:

- **Cursor replies.** The adapter treats a turn as finished when the first `afterAgentResponse` arrives.
  If Cursor sends a reply before each tool call, the final answer would be missed. It needs a real Cursor
  session to settle; see `cursor-stop-hook/README.md`.
- **Codex and Cursor hook commands** are written as bare `raindrop sync --tool ...` (no full path to the
  program and no `--fail-open`), unlike the other four. They work when `raindrop` is on the PATH the app
  launches with, and a failed sync exits 1 (Cursor and Codex carry on when a hook fails).
- `codex-stop-hook/stop_sync.py` keeps its own parser and upload code, unlike the scripts above, which
  all go through `run_sync`. It honours the sharing settings, but any future change to the shared sync
  (a new redaction step, say) has to be repeated there.
- `codex-stop-hook/stop_sync.py` and `cursor-stop-hook/capture.py` predate the `<tool>_stop_sync.py`
  naming the other scripts use.

## 1. Run automated tests

From the Raindrop repository root in PowerShell:

```powershell
py -3 -m unittest discover -s stophooks/codex-stop-hook -p "test_*.py" -v
py -3 -m unittest discover -s stophooks/cursor-stop-hook -p "test_*.py" -v
py -3 -m pytest raindrop-cli/tests -v
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
