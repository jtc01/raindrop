// Loads the team feed from the raindrop API (GET /recent) and maps it to the
// shape the dashboard renders.
const STORAGE_KEY = "raindrop.connection"

export function loadConnection() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY)) ?? null
  } catch {
    return null
  }
}

export function saveConnection(connection) {
  if (connection) localStorage.setItem(STORAGE_KEY, JSON.stringify(connection))
  else localStorage.removeItem(STORAGE_KEY)
}

export async function fetchFeed({ apiUrl, apiKey }) {
  const res = await fetch(`${apiUrl.replace(/\/+$/, "")}/recent`, {
    headers: { Authorization: `Bearer ${apiKey}` },
  })
  if (res.status === 401) throw new Error("API key rejected. Run `raindrop doctor` or rejoin.")
  if (!res.ok) throw new Error(`API returned ${res.status}`)
  const body = await res.json()
  return {
    now: Date.now(),
    team: { product: "raindrop", name: body.team_id },
    teammates: body.members.map((m) => ({
      id: m.agent_id,
      name: m.you ? `${m.display_name} (you)` : m.display_name,
      focus: m.focus,
    })),
    // `ts` from the CLI is in seconds.
    exchanges: body.exchanges.map((e) => ({
      id: `${e.agent_id}:${e.session_id}:${e.idx}`,
      agentId: e.agent_id,
      ts: (e.ts ?? 0) * 1000,
      prompt: e.user_text ?? "",
      reply: e.assistant_text ?? "",
      files: e.files_touched ?? [],
    })),
  }
}
