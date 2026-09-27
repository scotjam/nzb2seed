// Talking to the nzb2seed server: requests, and the live-change feed it pushes.

export async function api(path, body) {
  const opt = body === undefined ? {} :
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

/* What changed on the server, as it happens: "jobs", "auto" or "settings". The server
   pushes a name when something of that kind changes, and the page fetches just that.
   If the feed is not available (an old server, a proxy that buffers), everything is
   fetched every few seconds instead, so the page is never stale for long. */
const listeners = new Map();          // topic -> Set of callbacks
let source = null, fallback = null;

export function onChange(topic, fn) {
  if (!listeners.has(topic)) listeners.set(topic, new Set());
  listeners.get(topic).add(fn);
  connect();
  return () => listeners.get(topic).delete(fn);
}

function tell(topic) {
  for (const fn of listeners.get(topic) || []) fn();
}

function connect() {
  if (source || typeof EventSource === "undefined") {
    if (!source && !fallback) poll();
    return;
  }
  source = new EventSource("/api/events");
  source.addEventListener("change", (e) => {
    let topics = [];
    try { topics = JSON.parse(e.data).topics || []; } catch { /* ignore a garbled message */ }
    topics.forEach(tell);
  });
  source.addEventListener("open", () => { clearInterval(fallback); fallback = null; for (const t of listeners.keys()) tell(t); });
  source.addEventListener("error", () => { if (!fallback) poll(); });   // it reconnects by itself
}

function poll() {
  fallback = setInterval(() => { for (const t of listeners.keys()) tell(t); }, 5000);
}
