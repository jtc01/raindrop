#!/usr/bin/env bash
# Usage: bench/run.sh <baseline|raindrop> [runs_per_question]
# Runs each question in ~/raindrop-bench/questions.txt (kept OUTSIDE the repo,
# otherwise the agent greps it and answers every question at once) in a fresh headless Claude Code
# session and saves the JSON result (includes token usage) to bench/results/<condition>/.
set -euo pipefail
COND="${1:?usage: bench/run.sh baseline|raindrop [runs]}"
RUNS="${2:-3}"
ROOT="$(git rev-parse --show-toplevel)"
BENCH_HOME="${RAINDROP_BENCH_HOME:-$HOME/raindrop-bench}"  # outside the repo so the agent can't find the questions or past answers
OUT="$BENCH_HOME/results/$COND"
mkdir -p "$OUT"
cd "$ROOT"

# Same read-only exploration tools in both conditions; Raindrop adds its MCP tools.
# No git log/show: the question list is in git history and would leak.
TOOLS="Read,Grep,Glob,Bash(ls:*)"
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
    # < /dev/null: otherwise claude reads the rest of questions.txt from the loop's stdin
    claude -p "$q Answer only this one question, concisely." --output-format json --allowedTools "$TOOLS" < /dev/null > "$f" || echo "  (run failed, see $f)"
  done
done < "$BENCH_HOME/questions.txt"
echo "Saved to $OUT. Next: python3 bench/summarize.py"
