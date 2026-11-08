// Loads the team feed from the raindrop API (GET /recent) and maps it to the
// shape the dashboard renders.
const STORAGE_KEY = "raindrop.connection"

// How many chats each view shows: every person is limited to their own newest PER_PERSON,
// and the Everyone tab shows the newest EVERYONE_LIMIT sent by anyone.
export const PER_PERSON = 25
export const EVERYONE_LIMIT = 50

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
  const base = apiUrl.replace(/\/+$/, "")
  const headers = { Authorization: `Bearer ${apiKey}` }

  // First fetch: the members, plus the newest EVERYONE_LIMIT chats sent by anyone.
  const membersRes = await fetch(`${base}/recent?n=${EVERYONE_LIMIT}`, { headers })
  if (membersRes.status === 401) throw new Error("API key rejected. Run `raindrop doctor` or rejoin.")
  if (!membersRes.ok) throw new Error(`API returned ${membersRes.status}`)
  const body = await membersRes.json()

  const members = body.members.map((m) => ({
    id: m.agent_id,
    name: m.you ? `${m.display_name} (you)` : m.display_name,
    focus: m.focus,
  }))

  // Fetch each person's own newest PER_PERSON chats in parallel, so a quiet person still shows
  // theirs when someone else dominates the Everyone feed.
  const perPersonResults = await Promise.all(
    members.map(({ id }) =>
      fetch(`${base}/recent?n=${PER_PERSON}&agent_id=${encodeURIComponent(id)}`, { headers })
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`API returned ${r.status}`))))
        .then((b) => b.exchanges ?? [])
    )
  )

  // Deduplicate by exchange id across the everyone feed and the per-person results.
  const seen = new Set()
  const allExchanges = []
  for (const group of [body.exchanges ?? [], ...perPersonResults]) {
    for (const e of group) {
      const key = `${e.agent_id}:${e.session_id}:${e.idx}`
      if (!seen.has(key)) {
        seen.add(key)
        allExchanges.push(e)
      }
    }
  }

  const mapExchange = (e) => ({
    id: `${e.agent_id}:${e.session_id}:${e.idx}`,
    agentId: e.agent_id,
    ts: (e.ts ?? 0) * 1000,
    prompt: e.user_text ?? "",
    reply: e.assistant_text ?? "",
    files: e.files_touched ?? [],
    model: e.model ?? "",
  })

  return {
    now: Date.now(),
    team: { product: "raindrop", name: body.team_id },
    teammates: members,
    exchanges: allExchanges.map(mapExchange),
  }
}
