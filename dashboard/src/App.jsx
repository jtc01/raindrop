import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react"
import * as sample from "./data.js"
import { fetchFeed, loadConnection, saveConnection } from "./api.js"
import "./App.css"

const ACTIVE_MS = 30 * 60 * 1000

function formatAgo(ts, now) {
  const minutes = Math.round((now - ts) / 60000)
  if (minutes < 1) return "just now"
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.round(hours / 24)
  return days === 1 ? "1d ago" : `${days}d ago`
}

function Reply({ id, text, open, onToggle }) {
  const ref = useRef(null)
  const [overflows, setOverflows] = useState(false)

  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const measure = () => {
      if (open) return
      setOverflows(el.scrollHeight > el.clientHeight + 1)
    }
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(el)
    return () => observer.disconnect()
  }, [text, open])

  return (
    <>
      <p ref={ref} className={open ? "reply" : "reply clamped"}>
        {text}
      </p>
      {overflows && (
        <button
          type="button"
          className="more"
          aria-expanded={open}
          onClick={() => onToggle(id)}
        >
          {open ? "Show less" : "Show full reply"}
        </button>
      )}
    </>
  )
}

function Feed({ data, onDisconnect }) {
  const { exchanges, now, teammates, team } = data
  const personById = useCallback(
    (id) => teammates.find((person) => person.id === id) ?? { name: "Unknown", focus: "" },
    [teammates],
  )
  const [selectedId, setSelectedId] = useState(null)
  const [query, setQuery] = useState("")
  const [openId, setOpenId] = useState(null)

  const lastActive = useMemo(() => {
    const times = {}
    for (const exchange of exchanges) {
      times[exchange.agentId] = Math.max(times[exchange.agentId] ?? 0, exchange.ts)
    }
    return times
  }, [exchanges])

  const newest = useMemo(
    () => Math.max(0, ...exchanges.map((exchange) => exchange.ts)),
    [exchanges],
  )

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return exchanges
      .filter((exchange) => !selectedId || exchange.agentId === selectedId)
      .filter((exchange) => {
        if (!needle) return true
        const person = personById(exchange.agentId)
        const haystack = [
          exchange.prompt,
          exchange.reply,
          person?.name,
          person?.focus,
          ...exchange.files,
        ]
          .join("\n")
          .toLowerCase()
        return haystack.includes(needle)
      })
      .sort((a, b) => b.ts - a.ts)
  }, [exchanges, personById, query, selectedId])

  function selectPerson(id) {
    setSelectedId((current) => (current === id ? null : id))
  }

  return (
    <div className="app">
      <header className="top">
        <div className="brand">
          <p className="product">{team.product}</p>
        </div>
        <p className="status">
          <span>{teammates.length} people</span>
          <span className="sep" aria-hidden="true" />
          <span>last sync {newest ? formatAgo(newest, now) : "never"}</span>
          <button type="button" className="more" onClick={onDisconnect}>
            {data.sample ? "Connect" : "Disconnect"}
          </button>
        </p>
      </header>

      <div className="body">
        <aside className="roster" aria-label="Teammates">
          <button
            type="button"
            className={selectedId ? "person" : "person selected"}
            aria-pressed={selectedId === null}
            onClick={() => setSelectedId(null)}
          >
            <span className="who">
              <span className="name">Everyone</span>
              <span className="focus">{exchanges.length} exchanges</span>
            </span>
          </button>
          {teammates.map((person) => {
            const active = now - (lastActive[person.id] ?? 0) < ACTIVE_MS
            const selected = selectedId === person.id
            return (
              <button
                key={person.id}
                type="button"
                className={selected ? "person selected" : "person"}
                aria-pressed={selected}
                onClick={() => selectPerson(person.id)}
              >
                <span
                  className={active ? "presence on" : "presence"}
                  title={active ? "Synced in the last 30 minutes" : "Idle"}
                />
                <span className="who">
                  <span className="name">{person.name}</span>
                  <span className="focus">{person.focus}</span>
                </span>
                <span className="when">{lastActive[person.id] ? formatAgo(lastActive[person.id], now) : ""}</span>
              </button>
            )
          })}
        </aside>

        <main className="feed">
          <form className="search" role="search" onSubmit={(event) => event.preventDefault()}>
            <label htmlFor="q">Search</label>
            <input
              id="q"
              type="search"
              value={query}
              placeholder="Search prompts, replies, or files"
              onChange={(event) => setQuery(event.target.value)}
            />
            <p className="count">
              {visible.length} of {exchanges.length} exchanges
            </p>
          </form>

          {visible.length === 0 ? (
            <p className="empty">
              {exchanges.length ? "Nothing matches that search." : "No exchanges synced yet."}
            </p>
          ) : (
            <ol className="cards">
              {visible.map((exchange) => {
                const person = personById(exchange.agentId)
                const open = openId === exchange.id
                return (
                  <li key={exchange.id}>
                    <article className="card">
                      <header className="card-meta">
                        <span className="name">{person.name}</span>
                        <span className="focus">{person.focus}</span>
                        <time dateTime={new Date(exchange.ts).toISOString()}>
                          {formatAgo(exchange.ts, now)}
                        </time>
                      </header>
                      <h2>{exchange.prompt}</h2>
                      <Reply
                        id={exchange.id}
                        text={exchange.reply}
                        open={open}
                        onToggle={(id) =>
                          setOpenId((current) => (current === id ? null : id))
                        }
                      />
                      <ul className="files">
                        {exchange.files.map((file) => (
                          <li key={file}>{file}</li>
                        ))}
                      </ul>
                    </article>
                  </li>
                )
              })}
            </ol>
          )}
        </main>
      </div>
    </div>
  )
}

function Connect({ error, onConnect, onSample }) {
  const [apiUrl, setApiUrl] = useState("")
  const [apiKey, setApiKey] = useState("")
  return (
    <div className="app">
      <main className="feed">
        <form
          className="search"
          onSubmit={(event) => {
            event.preventDefault()
            onConnect({ apiUrl: apiUrl.trim(), apiKey: apiKey.trim() })
          }}
        >
          <label htmlFor="url">API URL</label>
          <input id="url" required value={apiUrl} onChange={(e) => setApiUrl(e.target.value)} />
          <label htmlFor="key">API key</label>
          <input
            id="key"
            type="password"
            required
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
          />
          {error && <p className="empty">{error}</p>}
          <p className="count">
            The key is kept in this browser only (find it in ~/.raindrop/config.json).
          </p>
          <button type="submit" className="more">Connect</button>{" "}
          <button type="button" className="more" onClick={onSample}>Use sample data</button>
        </form>
      </main>
    </div>
  )
}

const SAMPLE = { ...sample, sample: true }

function App() {
  const [connection, setConnection] = useState(loadConnection)
  const [data, setData] = useState(null)
  const [error, setError] = useState("")

  useEffect(() => {
    if (!connection) return
    let cancelled = false
    const load = () =>
      fetchFeed(connection)
        .then((feed) => !cancelled && (setData(feed), setError("")))
        .catch((e) => !cancelled && setError(e.message))
    load()
    const timer = setInterval(load, 30000)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [connection])

  function disconnect() {
    saveConnection(null)
    setConnection(null)
    setData(null)
  }

  if (data) return <Feed data={data} onDisconnect={disconnect} />
  if (connection && !error) return <p className="empty">Loading…</p>
  return (
    <Connect
      error={error}
      onConnect={(c) => {
        saveConnection(c)
        setError("")
        setConnection(c)
      }}
      onSample={() => setData(SAMPLE)}
    />
  )
}

export default App
