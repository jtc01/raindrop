#!/usr/bin/env bash
# Usage: bench/run.sh <baseline|raindrop> [runs_per_question]
# Runs each question in bench/questions.txt in a fresh headless Claude Code
# session and saves the JSON result (includes token usage) to bench/results/<condition>/.
set -euo pipefail
COND="${1:?usage: bench/run.sh baseline|raindrop [runs]}"
RUNS="${2:-3}"
ROOT="$(git rev-parse --show-toplevel)"
OUT="$ROOT/bench/results/$COND"
mkdir -p "$OUT"
cd "$ROOT"

# Same read-only exploration tools in both conditions; Raindrop adds its MCP tools.
TOOLS="Read,Grep,Glob,Bash(git log:*),Bash(git show:*),Bash(ls:*)"
if [ "$COND" = "raindrop" ]; then
  TOOLS="$TOOLS,mcp__raindrop__search_team_context,mcp__raindrop__get_recent_activity,mcp__raindrop__list_teammates"
fi

i=0
while IFS= read -r q || [ -n "$q" ]; do
  [ -z "$q" ] && continue
  i=$((i+1))
  for r in $(seq 1 "$RUNS"); do
    f="$OUT/q${i}_r${r}.json"
    echo "[$COND] q$i run $r: $q"
    claude -p "$q Answer concisely." --output-format json --allowedTools "$TOOLS" > "$f" || echo "  (run failed, see $f)"
  done
done < "$ROOT/bench/questions.txt"
echo "Saved to $OUT. Next: python3 bench/summarize.py"
