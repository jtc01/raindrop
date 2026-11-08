#!/usr/bin/env bash
# Usage: bench/trace.sh <baseline|raindrop> <question number>
# Runs ONE benchmark question with streamed output and prints each tool call the
# agent makes, in order, so we can see where turns go. Sharing should be paused.
set -euo pipefail
COND="${1:?usage: bench/trace.sh baseline|raindrop N}"; N="${2:?question number}"
BENCH_HOME="${RAINDROP_BENCH_HOME:-$HOME/raindrop-bench}"
q="$(sed -n "${N}p" "$BENCH_HOME/questions.txt")"
TOOLS="Read,Grep,Glob,Bash(ls:*)"
[ "$COND" = "raindrop" ] && TOOLS="$TOOLS,mcp__raindrop__search_team_context,mcp__raindrop__get_recent_activity,mcp__raindrop__list_teammates"
cd "$(git rev-parse --show-toplevel)"

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
mkdir -p "$BENCH_HOME/traces"
out="$BENCH_HOME/traces/${COND}_q${N}_$(date +%H%M%S).jsonl"
claude -p "$q Answer only this one question, concisely." --output-format stream-json --verbose \
  --allowedTools "$TOOLS" --strict-mcp-config --mcp-config "$MCP_CFG" < /dev/null > "$out"
python3 - "$out" <<'PY'
import json, sys
step = 0
for line in open(sys.argv[1]):
    try: e = json.loads(line)
    except ValueError: continue
    if e.get("type") == "system" and e.get("subtype") == "init":
        names = e.get("tools", [])
        print(f"tools loaded: {len(names)}  raindrop: {[n for n in names if 'raindrop' in n]}")
    if e.get("type") == "assistant":
        for c in e["message"].get("content", []):
            if c.get("type") == "tool_use":
                step += 1
                arg = json.dumps(c.get("input", {}))[:110]
                print(f"{step:2}. {c['name']:40} {arg}")
    if e.get("type") == "user":
        for c in e["message"].get("content", []):
            if isinstance(c, dict) and c.get("type") == "tool_result":
                txt = c.get("content")
                n = len(json.dumps(txt)) if txt is not None else 0
                err = " ERROR" if c.get("is_error") else ""
                print(f"      -> result {n} chars{err}")
    if e.get("type") == "result":
        u = e.get("usage", {})
        tot = sum(u.get(k, 0) for k in ("input_tokens","cache_creation_input_tokens","cache_read_input_tokens","output_tokens"))
        print(f"turns={e.get('num_turns')} tokens={tot} cost=${e.get('total_cost_usd',0):.3f}")
PY
echo "full trace: $out"
