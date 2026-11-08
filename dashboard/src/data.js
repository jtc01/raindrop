export const now = Date.now()
const minutes = (m) => now - m * 60 * 1000

export const team = {
  product: "raindrop",
  name: "raindrop",
}

export const teammates = [
  { id: "agt_dante", name: "Dante", focus: "frontend" },
  { id: "agt_maya", name: "Maya", focus: "backend" },
  { id: "agt_jordan", name: "Jordan", focus: "auth" },
]

export const exchanges = [
  {
    id: "ex_01",
    agentId: "agt_dante",
    ts: minutes(2),
    prompt: "Add a password visibility toggle to the login form",
    reply:
      "Added a button beside the password field that switches the input between password and text. The label flips between Show and Hide, and the button stays keyboard-focusable.",
    files: ["src/Login.tsx", "src/Login.css"],
    model: "claude-sonnet-4-5",
  },
  {
    id: "ex_02",
    agentId: "agt_maya",
    ts: minutes(18),
    prompt: "Hash the join code before storing it",
    reply:
      "The create-team route now stores sha256 of the 8-character join code in DynamoDB. The plaintext code is returned once in the response and is not written to the table.",
    files: ["api/teams.py"],
    model: "claude-sonnet-4-5",
  },
  {
    id: "ex_03",
    agentId: "agt_jordan",
    ts: minutes(3 * 60),
    prompt: "Reject tool calls when the bearer key does not match an active agent",
    reply:
      "Auth middleware hashes the bearer token and looks it up in cache, then DynamoDB. A match attaches team_id and agent_id to the request. Anything else returns 401 before a tool runs.",
    files: ["api/auth.py", "api/mcp.py"],
    model: "claude-opus-4-5",
  },
  {
    id: "ex_04",
    agentId: "agt_dante",
    ts: minutes(4 * 60),
    prompt: "Lay out the team activity page as a feed, not a chart",
    reply:
      "The page is a teammate list plus a newest-first feed. Each card shows the prompt, a short reply, and the files that exchange touched. Search filters the same list.",
    files: ["dashboard/src/App.jsx", "dashboard/src/App.css"],
  },
  {
    id: "ex_05",
    agentId: "agt_maya",
    ts: minutes(5 * 60),
    prompt: "Store each exchange in S3 and embed a short version",
    reply:
      "push_messages writes one JSON object per exchange under teams/{team}/agents/{agent}/sessions/{session}/{idx}.json. The embedding text keeps the prompt, the final reply, and the file paths, and drops raw tool output.",
    files: ["api/messages.py", "api/embed.py"],
  },
  {
    id: "ex_06",
    agentId: "agt_jordan",
    ts: minutes(8 * 60),
    prompt: "Generate a join code and a one-time agent key on raindrop join",
    reply:
      "Join validates the code, creates an agent id, and returns secrets.token_urlsafe(32) once. Only sha256 of that key is stored. The CLI writes ~/.raindrop/config.json with permissions 0600.",
    files: ["cli/join.py"],
  },
  {
    id: "ex_07",
    agentId: "agt_maya",
    ts: minutes(26 * 60),
    prompt: "Embed exchanges with the same model search will use",
    reply:
      "Both push and search call Bedrock Titan V2, amazon.titan-embed-text-v2:0, 512 dimensions, normalize true. Search reranks the nearest vectors with a recency penalty, then loads the full text with the s3_key on each vector.",
    files: ["api/embed.py", "api/search.py"],
  },
  {
    id: "ex_08",
    agentId: "agt_dante",
    ts: minutes(28 * 60),
    prompt: "Only move the sync cursor after the upload succeeds",
    reply:
      "raindrop sync reads the transcript past the saved offset, holds back a prompt that has no reply yet, and posts complete exchanges in batches. The cursor file updates only when the response is HTTP 200.",
    files: ["cli/sync.py", "cli/cursor.py"],
  },
  {
    id: "ex_09",
    agentId: "agt_jordan",
    ts: minutes(30 * 60),
    prompt: "Strip secrets on the machine before anything is uploaded",
    reply:
      "Redaction runs inside sync, before the POST. It replaces AWS keys, bearer tokens, and common API key assignments with [REDACTED] so those values never leave the laptop.",
    files: ["cli/redact.py"],
  },
  {
    id: "ex_10",
    agentId: "agt_maya",
    ts: minutes(32 * 60),
    prompt: "Return teammates with name and focus for the dashboard",
    reply:
      "list_teammates reads the agents on this team and returns agent id, display name, and focus. get_recent_activity returns the newest exchanges with the same attribution: who, when, and which files.",
    files: ["api/teammates.py"],
  },
]
