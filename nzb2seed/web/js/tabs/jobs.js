// The Jobs tab: the list of builds with a filter and bulk actions, and the open job with
// its piece map, steps and log. Both follow the server live.
import { html, useEffect, useLayoutEffect, useRef, useState } from "../lib.js";
import { api, onChange } from "../api.js";
import { backToJobs, jobs, jobScreen, nearlyLimitText, openJob, openJobId, setTrackerLimit, settings, shortEnough, toast, trackerLimit, useStore } from "../store.js";
import { age, filterMatch, havePct, linked, missText, remember, keep, sortBy, trackerName } from "../util.js";
import { SortSelect } from "../components/sort.js";
import { BADLY_ENDED, JOB_KINDS, SELECT, builtLater, canAbandon, canAdd, canRemove, canRetry, dotOf, finalOf, isLive, kindOf, trackersOf, viaTracker } from "./jobs-rules.js";
import { OtherTrackers, approved } from "./jobs-other.js";
import { addFromTrackers, addToClient, cancelMany, clearAndDelete, overrideAdd, removeMany, retryJob, retryMany, wholePosts, wholePostsText } from "./jobs-actions.js";

export function JobsTab({ active }) {
  const all = useStore(jobs);
  const current = useStore(openJobId);
  const showing = useStore(jobScreen);
  const [filter, setFilter] = useState(() => remember("jobFilter", "all"));
  // ticked jobs: id -> null (the job itself) or the release of another tracker it is ticked "via"
  const [picked, setPicked] = useState(() => new Map());

  const counts = {};
  for (const j of all) counts[kindOf(j)] = (counts[kindOf(j)] || 0) + 1;
  const kind = filter !== "all" && !counts[filter] ? "all" : filter;      // nothing left of that kind
  const shown = kind === "all" ? all : all.filter(j => kindOf(j) === kind);
  // a tick on a job the filter hides would be acted on unseen: ticks follow the filter
  const ticked = shown.filter(j => picked.has(j.id));

  useEffect(() => {                     // the newest job is open when nothing else is
    if (active && current == null && all.length) openJob(all[0].id, false);
  }, [active, current, all.length]);

  const choose = (v) => { setFilter(v); keep("jobFilter", v); setPicked(new Map()); };
  const tick = (id, on) => setPicked(p => { const n = new Map(p); on ? n.set(id, null) : n.delete(id); return n; });
  const untick = () => setPicked(new Map());
  const select = (entries) => setPicked(new Map(entries));          // a "Select:" choice replaces the ticks

  return html`
    <div class=${"jobs" + (showing ? " showing" : "")}>
      <div class="jobside">
        <${JobBar} all=${all} shown=${shown} ticked=${ticked} picked=${picked} kind=${kind} counts=${counts}
          choose=${choose} select=${select} untick=${untick} />
        <p class="status jobtip">Tip: clearing old jobs from the list lets nzb2seed clear the downloads they made -
          clear them once you are done with them (Clear from list, delete files does it at once).</p>
        <div class="joblist" id="joblist">
          ${shown.length ? shown.map(j => html`
            <${JobRow} key=${j.id} j=${j} all=${all} on=${picked.has(j.id)} via=${picked.get(j.id)} current=${j.id === current}
              tick=${tick} />`)
          : html`<div class="empty">${all.length ? "No jobs match this filter." : "No builds yet. Start one from Build."}</div>`}
        </div>
      </div>
      ${current != null && html`<${JobView} id=${current} />`}
    </div>`;
}

function JobBar({ all, shown, ticked, picked, kind, counts, choose, select, untick }) {
  const [tracker, setTracker] = useState("");
  const trackers = trackersOf(shown);
  // a greyed-out button says why, rather than doing nothing without a word
  const live = ticked.filter(isLive).length;
  const btn = (label, list, fn, title) => html`
    <button type="button" class="btn" disabled=${!list.length}
      title=${list.length || !ticked.length ? title
        : live === ticked.length ? `${title}. The ticked jobs are still running - cancel them first`
        : `${title}. None of the ticked jobs can have this done`}
      onClick=${() => fn(list, untick)}>${list.length ? `${label} (${list.length})` : label}</button>`;
  const pick = (fits) => select(shown.filter(fits).map(j => [j.id, null]));
  const byTracker = (t) => {
    setTracker(t);
    if (!t) return untick();
    select(shown.flatMap(j => { const via = viaTracker(j, t); return via === undefined ? [] : [[j.id, via]]; }));
  };
  // Add to torrent client: a job ticked as itself, if it can go; one ticked via another
  // tracker builds that tracker's torrent instead
  const viaOther = ticked.filter(j => picked.get(j.id));
  const addable = ticked.filter(j => !picked.get(j.id) && canAdd(j, all));
  const add = (list, done) => addFromTrackers(addable, viaOther.map(j => [j, picked.get(j.id)]), done);
  return html`
    <div class="jobbar" id="jobbar" role="toolbar" aria-label="Act on the ticked jobs">
      <select aria-label="Show only jobs of one kind" value=${kind} onChange=${(e) => choose(e.target.value)}>
        <option value="all">All jobs (${all.length})</option>
        ${JOB_KINDS.filter(([k]) => counts[k]).map(([k, label]) => html`<option value=${k}>${label} (${counts[k]})</option>`)}
      </select>
      <small>${ticked.length ? `${ticked.length} ticked` : "Tick jobs to act on several at once"}</small>
      <div class="jbrow"><b>Select:</b>
        ${SELECT.map(([k, label, fits]) => html`<button type="button" class="btn" disabled=${!shown.some(fits)}
          onClick=${() => { setTracker(""); pick(fits); }}>${label}</button>`)}
        <button type="button" class="btn" disabled=${!ticked.length} onClick=${() => { setTracker(""); untick(); }}>None</button>
        ${trackers.length > 0 && html`<select aria-label="Select by tracker" value=${tracker} onChange=${(e) => byTracker(e.target.value)}
          title="The jobs from this tracker - and those it was found on by Look on other trackers, ticked for that tracker's torrent only">
          <option value="">Tracker…</option>${trackers.map(t => html`<option value=${t}>${trackerName(t)}</option>`)}</select>`}
      </div>
      <div class="jbrow"><b>Action:</b>
        ${btn("Retry selected", ticked.filter(canRetry), retryMany, "Run each ticked job again, exactly as it was started")}
        ${btn("Cancel selected (kept on list)", ticked.filter(isLive), cancelMany, "Stop each ticked running or queued build after its current step - nothing is deleted, and it can be tried again")}
        ${btn("Add to torrent client (download the rest)", [...addable, ...viaOther], add,
          "Hand each ticked build that is within what its tracker lets you download to qBittorrent - and build the torrent of the tracker a job is ticked via, reusing what was downloaded")}
        ${btn("Clear from list, keep files", ticked.filter(canRemove), removeMany, "Take the ticked finished jobs off this list. The files they placed and anything in qBittorrent stay; their unused Usenet downloads are cleared within the hour")}
        ${btn("Clear from list, delete files", ticked.filter(canRemove), clearAndDelete, "Take the ticked finished jobs off this list and delete their Usenet downloads and the files they placed - never anything a torrent in qBittorrent uses")}
      </div>
    </div>`;
}

function JobRow({ j, all, on, via, current, tick }) {
  const open = () => openJob(j.id);
  const says = j.status === "waiting" ? "Waiting for you: pick an NZB"
    : j.status === "running" ? (j.steps.at(-1) || "Starting") : (j.result || j.status);
  return html`
    <div class=${"jrow" + (on ? " picked" : "")} aria-current=${String(current)}>
      <input type="checkbox" checked=${on} aria-label=${"Tick " + j.title} onChange=${(e) => tick(j.id, e.target.checked)} />
      <div class="jmain" role="button" tabindex="0" onClick=${open}
        onKeyDown=${(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } }}>
        <div class="t"><span class=${"dot " + dotOf(j)}></span>${j.title}${via && html` <span class="via">via ${trackerName(via.indexer)}</span>`}</div>
        <div class="s">${says}</div>
      </div>
      ${(BADLY_ENDED.includes(j.status) || j.extra?.settled?.length > 0) && html`<${JobActions} j=${j} all=${all} />`}
    </div>`;
}

/* a span with a button's role: a row's actions sit inside the row, and a button cannot
   hold other buttons */
function Act({ label, title, onPress }) {
  const run = (e) => { e.stopPropagation(); onPress(); };
  return html`<span class="btn" role="button" tabindex="0" title=${title} onClick=${run}
    onKeyDown=${(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); run(e); } }}>${label}</span>`;
}

/* a build that stopped short on a tracker with no limit set: say how much it allows, here */
function LimitPrompt({ tracker, where }) {
  const [pct, setPct] = useState(""), [mb, setMb] = useState(""), [busy, setBusy] = useState(false);
  const save = async (e) => {
    e.preventDefault(); e.stopPropagation();
    if (!pct && !mb) return toast("Give a percentage, megabytes, or both.");
    setBusy(true);
    try { const r = await setTrackerLimit(tracker, Number(pct) || 0, Number(mb) || 0); toast(`${where} lets you download ${r.limit}.`); }
    catch (err) { toast("Could not save it: " + err.message); }
    finally { setBusy(false); }
  };
  return html`<form class="limitask" onSubmit=${save} onClick=${(e) => e.stopPropagation()}>
    <small class="status">How much does ${where} let you download without it counting against you? None is set, so nothing goes to the torrent client yet.</small>
    <label>% <input type="number" min="0" max="100" step="0.1" value=${pct} aria-label=${`Percent ${where} allows`} onInput=${(e) => setPct(e.target.value)} /></label>
    <label>MB <input type="number" min="0" step="1" value=${mb} aria-label=${`Megabytes ${where} allows`} onInput=${(e) => setMb(e.target.value)} /></label>
    <button class="btn" disabled=${busy}>Save for ${where}</button></form>`;
}

function JobActions({ j, all }) {
  const x = j.extra || {};
  const [starting, setStarting] = useState(false);
  useStore(settings);                      // a tracker's limit set here (or in Settings) shows at once
  // the same as the Jobs bar's actions, for this one job
  const clear = canRemove(j) && html`
    <${Act} label="Clear from list, keep files" title="Take this job off the list. The files it placed and anything in qBittorrent stay; its unused Usenet downloads are cleared within the hour"
      onPress=${() => removeMany([j])} />
    <${Act} label="Clear from list, delete files" title="Take this job off the list and delete its Usenet downloads and the files it placed - never anything a torrent in qBittorrent uses"
      onPress=${() => clearAndDelete([j])} />`;
  if (x.abandoned) return html`<div class="jretry">${clear}</div>`;
  if (builtLater(j, all)) {
    return html`<div class="jretry"><small class="status">built when tried again, as job ${finalOf(j, all).id}</small>${clear}</div>`;
  }
  if (x.added) return html`<div class="jretry"><small class="status">${x.complete ? "complete in qBittorrent - seeding" : "in qBittorrent, downloading the rest"}</small>${clear}</div>`;
  if (x.added_via) return html`<div class="jretry"><small class="status">added to qBittorrent as ${trackerName(x.added_via)}'s torrent</small>${clear}</div>`;
  const limit = nearlyLimitText(x.tracker);
  // the tracker is always named: whether downloading the rest risks a hit-and-run depends on it
  const where = x.tracker ? trackerName(x.tracker) : "tracker unknown";
  const why = `Many trackers let you download a little of a torrent (here: less than ${limit}) without it counting `
    + `towards a hit-and-run. Check that ${where} is one of them before adding this: the missing `
    + `${x.have != null ? missText(x) : ""} will be downloaded over BitTorrent.`;
  const nearly = shortEnough(x);
  return html`
    <div class="jretry">
      ${nearly && x.seeders === 0 && html`<small class="status">Not offered for the torrent client: Prowlarr reported no seeders
        for it on ${trackerName(x.tracker || "its tracker")}, so the missing ${missText(x)} could never come over BitTorrent.</small>
        <${OtherTrackers} j=${j} Act=${Act} />`}
      ${x.have != null && x.tracker && !trackerLimit(x.tracker) && html`<${LimitPrompt} tracker=${x.tracker} where=${where} />`}
      ${x.have != null && !(nearly && x.seeders !== 0) && html`<${Act} label=${`Override: add to torrent client (${where})`}
        title=${`More than ${where} allows (${limit})${x.seeders === 0 ? ", and no seeders were reported" : ""}: add it anyway, knowing the missing ${missText(x)} would be downloaded over BitTorrent`}
        onPress=${() => overrideAdd(j, where)} />
        ${x.seeders !== 0 && !approved(x.tracker) && html`<${OtherTrackers} j=${j} Act=${Act} />`}`}
      ${nearly && x.seeders !== 0 && html`
        <${Act} label=${`Add to torrent client (${where}): ${havePct(x.have)}%`} title=${why} onPress=${() => addToClient(j, where)} />
        <span class="info" role="button" tabindex="0" aria-label=${why} title=${why}
          onClick=${(e) => { e.stopPropagation(); toast(why); }}>i</span>
        ${!approved(x.tracker) && html`<${OtherTrackers} j=${j} Act=${Act} />`}
        ${x.tracker && html`<${Act} label=${`Always add nearly complete for ${where}`}
          title=${`From now on, every build from ${where} that stops less than ${limit} short goes to qBittorrent to download the rest, without asking. Undo it in Settings.`}
          onPress=${() => addToClient(j, where, true)} />`}`}
      ${x.settled?.length > 0 && !j.retried_as && html`<${Act}
        label=${`Try whole posts (${wholePostsText(x).parts} part${x.settled.length === 1 ? "" : "s"}, ${wholePostsText(x).miss} missing)`}
        title=${`Only small files were missing from ${wholePostsText(x).names}, so no other whole post was downloaded for them. Build it again trying whole posts - about ${wholePostsText(x).post} or more - to see if they hold the rest.`}
        onPress=${() => wholePosts(j)} />`}
      ${j.retried_as ? html`<small class="status">tried again as job ${j.retried_as}</small>`
        : j.can_retry && BADLY_ENDED.includes(j.status) && (starting ? html`<small class="status">starting again...</small>`
          // the button goes the moment it is pressed, so it cannot be pressed twice
          : html`<${Act} label="Retry" title="Run it again, exactly as it was started"
              onPress=${() => { setStarting(true); retryJob(j); }} />`)}
      ${clear}
    </div>`;
}

/* ---------------------------------------------------------------- the open job */
function JobView({ id }) {
  const [job, setJob] = useState(null);
  const [lines, setLines] = useState([]);
  const cursor = useRef(0), logRef = useRef(null), stick = useRef(true);

  useEffect(() => {
    let gone = false, busy = false, timer = null;
    cursor.current = 0; setLines([]); setJob(null);
    async function load() {
      if (busy || gone) return;
      busy = true;
      try {
        const j = await api(`/api/jobs/${id}?since=${cursor.current}`);
        if (gone) return;
        const pre = logRef.current;
        stick.current = !pre || pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
        cursor.current = j.next;
        if (j.lines.length) setLines(l => l.concat(j.lines));
        setJob(j);
        clearTimeout(timer);
        if (isLive(j)) timer = setTimeout(load, 2000);    // a safety net under the live feed
      } catch { timer = setTimeout(load, 3000); }
      finally { busy = false; }
    }
    load();
    const off = onChange("jobs", load);
    return () => { gone = true; clearTimeout(timer); off(); };
  }, [id]);

  useLayoutEffect(() => {
    const pre = logRef.current;
    if (pre && stick.current) pre.scrollTop = pre.scrollHeight;
  }, [lines]);

  if (!job) return html`<div class="job" id="job"></div>`;
  const steps = lines.filter(l => l.k === "step").map(l => l.t);
  // a step repeated on every try is listed once, with how often: "Finding the NZBs (8 times)"
  const once = [], times = {};
  for (const s of steps) { if (!times[s]) once.push(s); times[s] = (times[s] || 0) + 1; }
  const last = steps.at(-1);
  const result = job.status === "running" ? "Building…" : job.status === "waiting" ? "Waiting for you"
    : job.status === "cancelled" ? "Cancelled" : job.result;
  const cancel = async () => {
    try { await api(`/api/jobs/${id}/cancel`, {}); toast("Cancelling after the current step…"); } catch (err) { toast(err.message); }
  };
  return html`
    <div class="job" id="job">
      <div class="top">
        <div><button type="button" class="btn jback" onClick=${backToJobs}>← All jobs</button>
          <h2>${job.title}</h2><p class=${"result " + job.status}>${result}</p></div>
        ${isLive(job) && html`<button class="btn danger" onClick=${cancel}>Cancel build</button>`}
      </div>
      <${PieceMap} pieces=${job.pieces} />
      ${job.auto_remove != null && html`<${AutoRemove} id=${id} on=${job.auto_remove} />`}
      ${job.question && html`<${Question} id=${id} q=${job.question} />`}
      <ol class="steps">
        ${once.map(s => html`<li class=${s === last && job.status !== "done" ? "now" + (BADLY_ENDED.includes(job.status) ? " failed" : "") : ""}>
          ${times[s] > 1 ? `${s} (${times[s]} times)` : s}</li>`)}
      </ol>
      <div class="progress">${job.progress || ""}</div>
      <details class="log"><summary>Full log</summary>
        <pre ref=${logRef}>${lines.map(l => html`<span class=${l.k === "step" ? "step" : l.k === "warn" ? "warn" : ""}>${
          l.k === "step" ? "\n==> " : l.k === "warn" ? "  ! " : l.k === "info" ? "    " : ""}${linked(l.t)}${"\n"}</span>`)}</pre>
      </details>
    </div>`;
}

/* per automatic build: follow the removal rules (on by default), or be kept for good */
function AutoRemove({ id, on }) {
  const s = useStore(settings);
  const r = s?.settings?.retention || {};
  const [now, setNow] = useState(on);
  useEffect(() => setNow(on), [on]);
  const flip = async (e) => {
    const want = e.target.checked;
    try { setNow((await api("/api/jobs/auto_remove", { id, on: want })).auto_remove); }
    catch (err) { toast(err.message); }
  };
  return html`<label class="autoremove" title="Ticked, this build follows Removing builds again in Settings - whatever it is set to, now or later, including off. Unticked, it is never removed.">
    <input type="checkbox" checked=${now} onChange=${flip} />
    ${" Follow the automatic removal rules"}
    <small class="status">${now ? (r.enabled ? ` - removed once older than ${r.days ?? 30} days` : " - removal is off in Settings at the moment")
      : " - kept, whatever the rules say"}</small>
  </label>`;
}

/* a build waiting for the person to pick an NZB */
function Question({ id, q }) {
  const [pick, setPick] = useState(null);
  const [busy, setBusy] = useState(false);
  const [filter, setFilter] = useState("");
  const [how, setHow] = useState(() => remember("asksort", "best"));
  useEffect(() => { setPick(null); setFilter(""); }, [id, q.prompt]);     // a new question: nothing picked yet
  // filtered and sorted - each keeps its place in the question, which is what the answer names
  const shown = sortBy(q.choices.map((c, i) => ({ ...c, i })).filter(c => filterMatch(c.title + " " + c.indexer, filter)), how);
  const answer = async (choice) => {
    setBusy(true);
    try { await api(`/api/jobs/${id}/answer`, { choice }); } catch (err) { toast(err.message); }
    finally { setBusy(false); }
  };
  return html`
    <div class="ask">
      <h3>Pick the NZB to use</h3>
      <p>${q.prompt}</p>
      <div class="askfind">
        <input type="text" placeholder="Filter these NZBs" aria-label="Filter these NZBs" value=${filter}
          onInput=${(e) => setFilter(e.target.value)} />
        <${SortSelect} label="Sort these NZBs" value=${how} grabs onChange=${(e) => { setHow(e.target.value); keep("asksort", e.target.value); }} />
        <small class="status">${shown.length === q.choices.length ? `${q.choices.length} NZBs` : `${shown.length} of ${q.choices.length} NZBs`}</small>
      </div>
      <div class="rows" role="radiogroup" aria-label="Search results">
        ${!shown.length && html`<div class="empty">Nothing matches the filter.</div>`}
        ${shown.map(({ i, ...c }) => html`
          <label key=${i} class=${"row" + (pick === i ? " on" : "")}>
            <input type="radio" name="askpick" aria-label=${c.title} checked=${pick === i} onChange=${() => setPick(i)} />
            <div><div class="title">${c.title}</div>
              <div class="meta"><span>${c.indexer}</span><span>${c.size_text}</span>
                ${c.publish_date && html`<span>${age(c.publish_date)}</span>`}
                ${c.grabs != null && html`<span>${c.grabs} grabs</span>`}
                ${c.note ? html`<span class="why">${c.note}</span>` : html`<span class="same">fits</span>`}</div></div>
          </label>`)}
      </div>
      <div class="actions">
        <button class="btn primary" disabled=${pick == null || busy} onClick=${() => answer(pick)}>Download this NZB</button>
        <button class="btn danger" disabled=${busy} onClick=${() => answer(null)}>Stop the build</button>
      </div>
    </div>`;
}

/* the piece map: every piece of the torrent, grey until verified */
function PieceMap({ pieces }) {
  const canvas = useRef(null);
  useEffect(() => {
    if (!pieces) return;
    const draw = () => drawPieces(canvas.current, pieces);
    draw();
    window.addEventListener("resize", draw);
    return () => window.removeEventListener("resize", draw);
  }, [pieces]);
  if (!pieces) return null;
  const n = pieces.length;
  let good = 0, bad = 0;
  for (const ch of pieces) { if (ch === "#") good++; else if (ch === "x") bad++; }
  const key = (v, label) => html`<span><i style=${`background:var(${v})`}></i>${label}</span>`;
  return html`
    <div class="map">
      <canvas ref=${canvas} height="34" aria-label="Torrent pieces"></canvas>
      <div class="legend">
        <span>${good.toLocaleString()} of ${n.toLocaleString()} pieces verified (${(good / n * 100).toFixed(1)}%)</span>
        ${key("--ok", "verified")}
        ${bad > 0 && key("--bad", `${bad.toLocaleString()} failed`)}
        ${good + bad < n && key("--idle", "not checked yet")}
      </div>
    </div>`;
}

function drawPieces(c, p) {
  if (!c) return;
  const dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth, hgt = c.clientHeight;
  c.width = Math.round(w * dpr); c.height = Math.round(hgt * dpr);
  const ctx = c.getContext && c.getContext("2d");
  if (!ctx) return;
  ctx.scale(dpr, dpr);
  const css = getComputedStyle(document.documentElement);
  const col = { ok: css.getPropertyValue("--ok"), soft: css.getPropertyValue("--ok-soft"),
                bad: css.getPropertyValue("--bad"), idle: css.getPropertyValue("--idle") };
  const n = p.length;
  if (n <= w / 2) {                       // one visible cell per piece
    const cw = w / n;
    for (let i = 0; i < n; i++) {
      ctx.fillStyle = p[i] === "#" ? col.ok : p[i] === "x" ? col.bad : col.idle;
      ctx.fillRect(i * cw, 0, Math.max(cw - (cw > 4 ? 1 : 0), 1), hgt);
    }
  } else {                                // one column per pixel, summarising its pieces
    for (let x = 0; x < w; x++) {
      const a = Math.floor(x * n / w), b = Math.max(a + 1, Math.floor((x + 1) * n / w));
      let g = 0, bad = 0;
      for (let i = a; i < b; i++) { if (p[i] === "#") g++; else if (p[i] === "x") bad++; }
      ctx.fillStyle = bad ? col.bad : g === b - a ? col.ok : g ? col.soft : col.idle;
      ctx.fillRect(x, 0, 1, hgt);
    }
  }
}
