# Raindrop

Shared team context for AI coding agents. Raindrop syncs what each teammate's
coding agent (Claude Code, Cursor, Codex, Kiro, ...) has been doing into a
shared, searchable store, so any teammate's agent can ask "what has the team
already built/decided here?" instead of rediscovering it or asking a human.

Built at the AWS x A10 Hackathon (SCU, Nov 2026).

## How it works

1. **CLI** (`raindrop-cli/`) — `raindrop create-team` / `raindrop join` sets up
   a local config (`~/.raindrop/config.json`, 0600) with an API key. A stop
   hook in each coding tool calls `raindrop sync` after a session, which:
   - reads new exchanges since the last cursor via an **adapter**
     (`raindrop/adapters/`, one per tool: Claude Code, Cursor, Codex, Kiro, ...)
   - redacts secrets and local identifiers (paths, usernames, hostnames)
   - skips upload if sharing is paused (`raindrop pause`) for that repo
   - POSTs the batch to `/messages`
2. **API** (`api/`) — single FastAPI/Starlette app on Lambda (via AWS Lambda
   Web Adapter) behind API Gateway HTTP API. Bearer-key auth middleware,
   DynamoDB for teams/join-codes/agents, S3 for raw exchanges, Bedrock Titan
   embeddings + S3 Vectors for semantic search.
3. **MCP proxy** (`raindrop proxy`) — a local stdio MCP server the coding tool
   launches, which forwards to the team API's `/mcp` endpoint and exposes:
   `search_team_context`, `get_recent_activity`, `list_teammates`, `whoami`,
   plus an always-available `raindrop_status` tool for diagnosing setup
   issues.

```
teammate's agent --sync--> API (Lambda) --embed--> S3 Vectors
                                |
your agent <--search_team_context (MCP proxy)------+
```

## Setup

```bash
# backend (one time, per team)
cd api && sam build --use-container && sam deploy

# CLI (each teammate)
cd raindrop-cli && pip install -e .
raindrop create-team   # or: raindrop join <code>
raindrop tools add claude-code --rules --yes
```

Run `raindrop doctor` to check config/connectivity, `raindrop whoami` to test
auth directly, `raindrop teammates` to list the team.

## Security

- Secrets (API keys, tokens) are redacted client-side before upload (4-layer
  pipeline: known values, known formats, key-value pairs, high-entropy
  strings).
- Local identifiers (absolute paths, usernames, hostnames) are stripped
  client-side and re-checked server-side.
- Shared context returned to an agent is wrapped in a delimited, explicitly
  untrusted fence to mitigate prompt injection from teammates' content.
- API Gateway throttling (burst 50 / 20 req/s) on all routes.
- `raindrop pause` / `raindrop unshare` opt a repo or machine out of sharing
  entirely; paused content is never uploaded.

## CLI commands

`create-team`, `join`, `invite`, `teammates`, `remove`, `codes` (list/revoke),
`rotate-key`, `leave`, `disband`, `pause`/`resume`, `share`/`unshare`, `doctor`,
`whoami`, `tools` (add/remove/list), `sync`, `proxy`, `help`.

## Status

Backend deployed and verified end-to-end (auth, team mgmt, sync, search,
recent activity). Benchmarked for token savings vs. agents working without
shared context (see `bench/`).

## Useful commands

```bash
# status / health
raindrop doctor                      # config + connectivity check
raindrop whoami                      # who the API thinks you are
curl -s "$API_URL/health"            # is the backend up

# team
raindrop teammates                   # who's on the team, who's lead
raindrop invite                      # get a fresh join code
raindrop codes                       # list active join codes
raindrop codes revoke <code|id>      # kill a join code
raindrop disband                     # (lead) delete all shared context + revoke every key

# sharing controls
raindrop pause                       # stop sharing everywhere (e.g. during a demo/benchmark)
raindrop resume                      # turn sharing back on
raindrop share / raindrop unshare    # per-repo opt in/out

# sync + search, manually
raindrop sync --dry-run              # preview what would be uploaded, no POST
raindrop sync                        # force a sync now (normally the stop hook does this)
raindrop tools list                  # which coding tools have Raindrop wired in
raindrop tools add claude-code --rules --yes
raindrop tools remove claude-code

# backend ops (AWS)
sam logs -n RaindropFunction --tail --stack-name <stack>     # live Lambda logs
sam deploy                                                    # redeploy after a code change
aws dynamodb scan --table-name raindrop-agents --max-items 5   # peek at a table
aws s3 ls s3://raindrop-exchanges-<account-id>/teams/ --recursive | tail -20
aws s3vectors query-vectors --vector-bucket-name <bucket> --index-name <index> --top-k 5 \
  --query-vector '{"float32":[...]}'   # raw vector search, for debugging

# benchmark
bash bench/run.sh baseline 3 && bash bench/run.sh raindrop 3
python3 bench/summarize.py
```
