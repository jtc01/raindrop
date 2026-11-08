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

# Load ONLY the MCP servers this condition needs. Otherwise whatever else is configured
# (claude.ai connectors like Notion load intermittently, ~85 tool schemas) rides along in
# some runs and not others, which swamps the difference we are measuring.
MCP_CFG="$(mktemp -t raindrop-bench-mcp.XXXXXX)"
trap 'rm -f "$MCP_CFG"' EXIT
if [ "$COND" = "raindrop" ]; then
  RD="$(command -v raindrop)"
  # alwaysLoad matches what `raindrop tools add claude-code` installs (no ToolSearch step)
  printf '{"mcpServers":{"raindrop":{"type":"stdio","command":"%s","args":["proxy"],"alwaysLoad":true}}}' "$RD" > "$MCP_CFG"
else
  printf '{"mcpServers":{}}' > "$MCP_CFG"
fi

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
    claude -p "$q Answer only this one question, concisely." --output-format json --allowedTools "$TOOLS" --strict-mcp-config --mcp-config "$MCP_CFG" < /dev/null > "$f" || echo "  (run failed, see $f)"
  done
done < "$BENCH_HOME/questions.txt"
echo "Saved to $OUT. Next: python3 bench/summarize.py"
