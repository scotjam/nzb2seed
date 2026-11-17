// The "Torrent list 2 Seed" tab: paste torrent names from a tracker's web page,
// choose the tracker, and add the list to the queue. nzb2seed checks the queue itself, one
// name at a time and oldest list first - Usenet for posts of the same release, then (only
// for those) the tracker for the torrent - so a list goes on being checked with the page
// closed. Only plausible names are built (as they are found, or when Build ticked is
// pressed), and never one already on the jobs list.
import { html, useEffect, useReducer, useRef, useState } from "../lib.js";
import { api, onChange } from "../api.js";
import { refreshJobs, toast } from "../store.js";
import { keep, remember, size, when } from "../util.js";
import { Options, useOptions } from "../components/options.js";
import { PauseButton } from "../components/pause.js";

const ACTIVE = ["queued", "checking"];
// the tracker choice for a list searched on Usenet only: no torrent is looked for, nothing built
const NO_TRACKER = "-";

function counts(l) {
  const done = l.rows.filter(r => r.state === "done");
  return { done: done.length, plausible: done.filter(r => r.result.plausible).length,
           left: l.rows.filter(r => r.state === "waiting" || r.state === "checking").length };
}
function stateOf(l, place) {
  const c = counts(l);
  if (l.state === "checking") return `checking ${c.done + 1} of ${l.rows.length} · ${c.plausible} plausible so far`;
  if (l.state === "queued") return `queued${place > 1 ? ` (${place - 1} list${place > 2 ? "s" : ""} ahead)` : " - next"} · ${l.rows.length} names`;
  return `${l.state === "stopped" ? "stopped" : "checked"}: ${c.done} of ${l.rows.length} · ${c.plausible} plausible`;
}
const buildable = (r) => r.state === "done" && (r.result.plausible || r.result.download) && !r.job && !r.listed;

/* How a list goes about its names. Only the first is safe: the other two download from the
   tracker over BitTorrent, using its ratio and bringing its seeding (hit-and-run) rules. */
const RISK = "WARNING: uses tracker ratio and creates HnR exposure";
const MODES = [
  ["normal", "Normal safe operation", "built from Usenet only - nothing comes over BitTorrent"],
  ["fallback", "Download from torrents if not on usenet", `${RISK} - don't do this`],
  ["torrent", "Skip usenet search and just download from torrent tracker", `${RISK} - don't do this`],
];
const MODE_SHORT = { fallback: "downloads over BitTorrent if not on Usenet", torrent: "downloads over BitTorrent only" };

/* a list queued with a warning option: what it downloads over BitTorrent, and from where */
function ListWarning({ l }) {
  if (!MODE_SHORT[l.mode]) return null;
  const rows = l.rows.filter(r => r.state === "done" && r.result?.download);
  const bytes = rows.reduce((n, r) => n + (r.result.torrent?.size || 0), 0);
  const going = ACTIVE.includes(l.state);
  return html`<div class="twarn tlistwarn">${RISK} - ${l.tracker}: ${MODE_SHORT[l.mode]} ·
    ${size(bytes)} to download over BitTorrent in ${rows.length} torrent${rows.length === 1 ? "" : "s"}${
    going ? " so far (still checking)" : ""}${l.auto ? "" : " - when built with Build ticked"}</div>`;
}

/* one list in the queue: its names, what was found, and its buttons */
function List({ l, place, open, unpicked, redraw }) {
  const key = (r) => l.id + "|" + r.name;
  const picked = l.rows.filter(r => buildable(r) && !unpicked.has(key(r)));
  const act = async (what) => {
    try { await api("/api/tracklist/" + what, { list: l.id }); }
    catch (err) { toast(`Could not ${what} it: ${err.message}`); }
  };
  const again = async (row) => {
    try { await api("/api/tracklist/recheck", { list: l.id, name: row.name }); }
    catch (err) { toast("Could not search it again: " + err.message); }
  };
  const build = async () => {
    try {
      const r = await api("/api/tracklist/build", { list: l.id, names: picked.map(x => x.name) });
      await refreshJobs();
      toast(r.started ? `${r.started} build${r.started === 1 ? "" : "s"} started - see the Jobs tab.` : "Nothing was started.");
    } catch (err) { toast("Could not start them: " + err.message); }
  };
  return html`<details class="tqueue" open=${open}>
    <summary><b>${l.tracker || "Usenet only (no tracker)"}</b> <span class="status">· added ${when(l.added)} · ${stateOf(l, place)}${l.auto ? " · builds as found" : ""}</span>
      <${ListWarning} l=${l} /></summary>
    <div class="actions">
      ${ACTIVE.includes(l.state)
        ? html`<button type="button" class="btn" onClick=${() => act("stop")}>Stop this list</button>`
        : html`<button type="button" class="btn" onClick=${() => act("remove")}>Remove from the page</button>`}
      ${(!l.auto || picked.length > 0) && html`<button type="button" class="btn primary" disabled=${!picked.length} onClick=${build}>
        Build ticked${picked.length ? ` (${picked.length})` : ""}</button>`}
    </div>
    <ul class="trows">${l.rows.map(row => {
      const r = row.result, t = r?.torrent;
      const job = row.job || row.listed;
      return html`<li class=${row.state === "done" ? (r.plausible || r.on_usenet || r.download ? "good" : "poor") : ""}>
        ${buildable(row) && html`<input type="checkbox" checked=${!unpicked.has(key(row))} aria-label=${"Build " + row.name}
          onChange=${(e) => { e.target.checked ? unpicked.delete(key(row)) : unpicked.add(key(row)); redraw(); }} />`}
        <div>
          <div class="tname">${row.name}${t && t.title !== row.name && html`<small>${t.title}</small>`}</div>
          <div class="tmeta">
            ${t && html`<span>${size(t.size)}</span>`}${t?.seeders != null && html`<span>${t.seeders} seeders</span>`}
            <span class="verdict">${row.state === "waiting" ? "waiting" : row.state === "checking" ? "checking…"
              : row.state === "skipped" ? "not checked - the list was stopped"
              : row.state === "error" ? row.note : r.verdict}</span>
            ${job && html`<span>${row.job ? "started as" : "already on the jobs list:"} <a href=${"#jobs/" + job}>job ${job}</a></span>`}
            ${row.state === "done" && !job && row.note && html`<span>${row.note}</span>`}
            ${row.state === "done" && r.reused && html`<button type="button" class="btn"
              title="Another list found no Usenet post of this lately, so it was not searched again - search Usenet for it now"
              onClick=${() => again(row)}>Search again</button>`}
          </div>
          ${row.state === "done" && !t && r.others?.length > 0 && html`<details><summary>what the tracker found</summary>
            ${r.others.map(o => html`<div><small>${o}</small></div>`)}</details>`}
        </div>
      </li>`;
    })}</ul>
  </details>`;
}

/* Advanced options: the rules names are read and matched by - for the tracker chosen, or
   the default ones (for trackers without their own, and Not specified). For a tracker whose
   pages read badly: change them, save, and the pasted list is read again. */
const AGREE = [["year", "year"], ["source", "source (WEB-DL, Blu-ray, Remux, disc)"], ["codec", "codec (H.264, H.265)"],
               ["hdr", "HDR / Dolby Vision"], ["service", "streaming service (NF, AMZN...)"]];
const LISTS = [
  ["uploader_words", "Words that stand for a hidden uploader", "left off the end of a name"],
  ["no_group_words", "Words that mean no release group", "such a name is matched only very strictly"],
  ["skip_words", "Skip lines with any of these words", "e.g. patch - never searched"],
  ["other_tags", "End tags of books, programs and games", "for Not only video: [AUDIOBOOK], [PDF]..."],
  ["repost_suffixes", "Repost endings on Usenet names", "left off a post's name: -xpost..."],
  ["not_release_words", "Posts with these words are not the release", "for games and programs: update, trainer..."],
];
function Advanced({ tracker, saved, mode, setMode, noTracker }) {
  const [open, setOpen] = useState(false);
  const [r, setR] = useState(null), [own, setOwn] = useState(false), [busy, setBusy] = useState(false);
  const load = (fields = {}) => api("/api/tracklist/rules", { tracker, ...fields })
    .then(d => { setR({ ...d.rules, ...Object.fromEntries(LISTS.map(([k]) => [k, d.rules[k].join(", ")])) }); setOwn(d.own); })
    .catch(err => toast("Could not read the rules: " + err.message));
  useEffect(() => { if (open) load(); }, [open, tracker]);
  const set = (k, v) => setR({ ...r, [k]: v });
  const act = async (fields, said) => {
    setBusy(true);
    try { await load(fields); saved(); toast(said); } finally { setBusy(false); }
  };
  const who = tracker || "the default rules";
  return html`<div class=${"tadvanced" + (open ? " open" : "")}>
    <button type="button" class="btn" aria-expanded=${open} onClick=${() => setOpen(!open)}>
      ${open ? "Hide advanced options" : "Advanced options"}</button>
    ${open && html`<fieldset class="trules tmodes">
      <legend>How names are fetched</legend>
      ${noTracker && html`<p class="status">With no tracker chosen nothing is downloaded: Normal safe operation only.</p>`}
      ${MODES.map(([k, label, hint]) => html`<label class=${"check" + (noTracker && k !== "normal" ? " disabled" : "")}>
        <input type="radio" name="tracklist-mode" value=${k} checked=${mode === k}
          disabled=${noTracker && k !== "normal"} onChange=${() => setMode(k)} />
        ${label}<small class=${k === "normal" ? "status" : "twarn"}>${hint}</small></label>`)}
    </fieldset>`}
    ${open && r && html`<fieldset class="trules">
      <legend>${tracker ? `Rules for ${tracker}` : "Default rules (UNIT3D)"}
        <small class="status">${tracker ? (own ? " - its own" : " - the default (UNIT3D) ones, until saved for it")
          : " - for trackers without their own, and Not specified"}</small></legend>
      ${!tracker && html`<p class="status">The defaults suit UNIT3D sites - those Prowlarr lists with "(API)" after
        their name: each torrent is listed under its uploader's release name, with the uploader in a column of its own.
        A site that lists them its own way (renamed, or with the uploader after the name) is best given rules of its own.</p>`}
      <p class="status">How names pasted from ${tracker || "a tracker"} are read, and matched to Usenet posts. Change
        them if its pages are not read well; the pasted list is read again when saved.</p>
      <label class="check"><input type="checkbox" checked=${r.strip_uploader} onChange=${(e) => set("strip_uploader", e.target.checked)} />
        The word after a release group is the uploader - leave it off</label>
      <label class="check"><input type="checkbox" checked=${r.renamed_match} onChange=${(e) => set("renamed_match", e.target.checked)} />
        Match names this tracker writes its own way by what the release is (spaces, "DD+ 5.1", "iT"...)</label>
      ${r.renamed_match && html`<div class="tagree"><span>These have to agree as well as title, year or season,
          resolution and group:</span>
        ${AGREE.map(([k, label]) => html`<label class="check"><input type="checkbox" checked=${r.must_agree.includes(k)}
          onChange=${(e) => set("must_agree", e.target.checked ? [...r.must_agree, k] : r.must_agree.filter(x => x !== k))} />${label}</label>`)}</div>`}
      <label class="check"><input type="checkbox" checked=${r.title_year_search} onChange=${(e) => set("title_year_search", e.target.checked)} />
        Also search a film by its title and year alone (an edition after the year is written in many ways)</label>
      <label class="field"><span>A post matched with no release group: at most this much bigger than the torrent (%)</span>
        <input type="number" min="0" max="100" step="0.5" value=${r.groupless_max_pct}
          onInput=${(e) => set("groupless_max_pct", e.target.value)} /></label>
      ${LISTS.map(([k, label, hint]) => html`<label class="field"><span>${label} <small class="status">${hint}</small></span>
        <input type="text" value=${r[k]} placeholder="none" onInput=${(e) => set(k, e.target.value)} /></label>`)}
      <div class="actions">
        <button type="button" class="btn primary" disabled=${busy}
          onClick=${() => act({ save: { ...r, groupless_max_pct: Number(r.groupless_max_pct) } }, `Saved for ${who}.`)}>
          ${tracker ? `Save for ${tracker}` : "Save as the default rules"}</button>
        <button type="button" class="btn" disabled=${busy || (tracker && !own)}
          title=${tracker ? "Forget this tracker's own rules: it follows the default ones again" : "Back to the rules nzb2seed comes with"}
          onClick=${() => act({ reset: true }, tracker ? `${tracker} follows the default rules again.` : "The default rules are as nzb2seed came.")}>
          ${tracker ? "Use the default (UNIT3D) rules" : "Reset to how nzb2seed came"}</button>
      </div>
    </fieldset>`}
  </div>`;
}

export function TracklistTab({ active }) {
  const s = useRef({ text: "", tracker: remember("tracklist.tracker", ""), trackers: [], lists: [],
                     auto: remember("tracklist.auto", "1") === "1",     // on unless unticked
                     anyKind: remember("tracklist.anykind", "0") === "1",  // books, programs, games too
                     mode: remember("tracklist.mode", "normal"),            // see MODES
                     asking: false, nowarn: false,                          // the BitTorrent warning
                     warn: remember("tracklist.nowarn", "0") !== "1",       // "Warn me": on unless switched off
                     adding: false, preview: [], reading: false, seq: 0, timer: null, skip: new Set(),
                     unpicked: new Set(), sent: new Set() }).current;
  const [, redraw] = useReducer(n => n + 1, 0);
  const [opts, setOpts] = useOptions();
  // coming to this tab, or back to the browser tab showing it (by a click on the page too):
  // the names are scrolled to and selected, so the next list pasted replaces them
  const box = useRef(null);
  useEffect(() => {
    if (!active) return;
    let timer = 0;
    const pick = () => {
      clearTimeout(timer);
      // after a click that brought the window back has put the focus where it landed
      timer = setTimeout(() => {
        const el = box.current;
        if (!el || document.hidden) return;
        el.focus({ preventScroll: true });
        el.select();
        el.scrollIntoView({ block: "center" });
      }, 150);
    };
    pick();
    window.addEventListener("focus", pick);
    return () => { clearTimeout(timer); window.removeEventListener("focus", pick); };
  }, [active]);
  useEffect(() => {
    if (!active || s.trackers.length) return;
    api("/api/library/indexers", {}).then(r => { s.trackers = r.indexers || []; redraw(); }).catch(() => {});
  }, [active]);
  // the queue is kept and checked by nzb2seed: the page shows it, and follows it as it changes
  useEffect(() => {
    if (!active) return;
    const load = () => api("/api/tracklist/lists", {}).then(r => { s.lists = r.lists || []; redraw(); }).catch(() => {});
    load();
    return onChange("tracklist", load);
  }, [active]);

  /* the names the pasted text holds - read by the server (no search is made), shortly after
     typing stops, so what will be searched for is shown before anything is */
  function pasted(text) {
    s.text = text; clearTimeout(s.timer);
    const n = ++s.seq;
    if (!text.trim()) { Object.assign(s, { preview: [], reading: false }); s.skip.clear(); s.sent.clear(); redraw(); return; }
    s.reading = true; redraw();
    s.timer = setTimeout(async () => {
      try {
        const names = (await api("/api/tracklist/parse", { text, any_kind: s.anyKind,
                                                         tracker: s.tracker === NO_TRACKER ? "" : s.tracker })).names;
        if (n !== s.seq) return;                    // typed again meanwhile
        s.preview = names;
        for (const x of [...s.skip]) if (!names.includes(x)) s.skip.delete(x);
        for (const x of [...s.sent]) if (!names.includes(x)) s.sent.delete(x);
      } catch (err) { if (n === s.seq) toast("Could not read the list: " + err.message); }
      finally { if (n === s.seq) { s.reading = false; redraw(); } }
    }, 300);
  }
  const toSearch = () => s.preview.filter(x => !s.skip.has(x));

  async function add(e) {
    e.preventDefault();
    if (risky && s.warn) {
      s.asking = true; s.nowarn = false; redraw();          // asked first: OK proceed, or Cancel
      return;
    }
    return send();
  }
  const answer = (go) => {
    s.asking = false;
    if (go && s.nowarn) setWarn(false);           // Don't warn me again: the switch goes off
    redraw();
    if (go) send();
  };

  async function send() {
    if (!s.tracker) return toast("Choose the tracker the names are from, or Not specified to search Usenet only.");
    if (s.reading) return toast("Still reading the list - a moment.");
    const names = toSearch();
    if (!names.length) return toast(s.preview.length ? "Every name is unticked." : "No torrent names found in what was pasted.");
    keep("tracklist.tracker", s.tracker);
    s.adding = true; redraw();
    try {
      const r = await api("/api/tracklist/add", { names, tracker: noTracker ? "" : s.tracker,
                                                   auto: auto, options: opts, mode });
      // the pasted list stays, with what was just added unticked: the next few can be picked from it
      names.forEach(x => { s.sent.add(x); s.skip.add(x); });
      s.lists = (await api("/api/tracklist/lists", {})).lists || s.lists;
      const ahead = s.lists.filter(l => ACTIVE.includes(l.state) && l.id !== r.id).length;
      toast(ahead ? `Added to the queue, behind ${ahead} list${ahead === 1 ? "" : "s"} still being checked.` : "Added - checking now.");
    } catch (err) { toast("Could not add it: " + err.message); }
    finally { s.adding = false; redraw(); }
  }

  const setAuto = (e) => { s.auto = e.target.checked; keep("tracklist.auto", s.auto ? "1" : "0"); redraw(); };
  const setAnyKind = (e) => {
    s.anyKind = e.target.checked; keep("tracklist.anykind", s.anyKind ? "1" : "0");
    if (s.text.trim()) pasted(s.text); else redraw();       // the names read again, the new way
  };
  const noTracker = s.tracker === NO_TRACKER;
  const mode = noTracker || !MODES.some(([k]) => k === s.mode) ? "normal" : s.mode;
  const risky = mode !== "normal";
  const setMode = (k) => { s.mode = k; keep("tracklist.mode", k); redraw(); };
  const setWarn = (on) => { s.warn = on; keep("tracklist.nowarn", on ? "0" : "1"); redraw(); };
  const auto = s.auto && !noTracker;       // nothing can be built without a tracker to take the torrent from
  const busy = s.lists.filter(l => ACTIVE.includes(l.state)).reverse();      // in the order they are checked
  const ended = s.lists.filter(l => !ACTIVE.includes(l.state));
  const label = (auto ? "Check and build" : "Check") + (toSearch().length ? ` ${toSearch().length}` : "");

  return html`
    <h1>Torrent list 2 Seed</h1>
    <p class="lede">Paste in a list of torrent file names from your favourite tracker to be parsed and searched for
      on Usenet. If files are found on Usenet, the corresponding torrent will be downloaded from the tracker over
      prowlarr and the files will be arranged to match the torrent, allowing it to be seeded without
      torrent-content downloads</p>
    <form class="form" onSubmit=${add}>
      <label class="field"><span>Torrent names</span>
        <textarea ref=${box} rows="10" placeholder="One per line - copied rows of a tracker's list are fine: sizes, dates and the like beside the names are ignored"
          value=${s.text} onInput=${(e) => pasted(e.target.value)}></textarea></label>
      <label class="check"><input type="checkbox" checked=${s.anyKind} onChange=${setAnyKind} />
        Not only video: books, audiobooks, programs and games too (less reliable parsing)</label>
      ${s.anyKind && html`<small class="status thelp">Without a resolution or source to go by, a line counts when it
        ends in a tag such as [AUDIOBOOK] or [PDF], in a group after a dash ("- GRP"), carries a version number,
        or reads "Title - Author". Some page text may be taken for a name and some names missed - untick what is not
        one. These are searched for on Usenet by their exact name only.</small>`}
      ${s.text.trim() && html`<div class="tpreview">
        ${s.reading ? html`<p class="status">Reading the names…</p>`
          : !s.preview.length ? html`<p class="status">No torrent names found in what was pasted.</p>`
          : html`<p class="status"><b>${toSearch().length} of ${s.preview.length}</b> name${s.preview.length === 1 ? "" : "s"} will be
              searched for - untick any to leave out. Nothing is searched until you press ${auto ? "Check and build" : "Check"}.</p>
            ${s.sent.size > 0 && html`<p class="status">${s.sent.size} already added to the queue - unticked, and marked below.</p>`}
            <div class="actions tpick">
              <button type="button" class="btn" disabled=${s.preview.every(x => !s.skip.has(x) || s.sent.has(x))}
                onClick=${() => { s.skip = new Set(s.sent); redraw(); }}>Select all${s.sent.size ? " not yet added" : ""}</button>
              <button type="button" class="btn" disabled=${s.skip.size === s.preview.length}
                onClick=${() => { s.preview.forEach(x => s.skip.add(x)); redraw(); }}>Select none</button>
              <button type="button" class="btn" onClick=${() => pasted("")}>Clear the list</button>
            </div>
            <ul>${s.preview.map(x => html`<li><label class="check"><input type="checkbox" checked=${!s.skip.has(x)}
              onChange=${(e) => { e.target.checked ? s.skip.delete(x) : s.skip.add(x); redraw(); }} />${x}${
              s.sent.has(x) && html` <small class="tadded">added</small>`}</label></li>`)}</ul>`}
      </div>`}
      <label class="field"><span>Tracker</span>
        <select value=${s.tracker} onChange=${(e) => { s.tracker = e.target.value; if (s.text.trim()) pasted(s.text); else redraw(); }}>
          <option value="">${s.trackers.length ? "Choose…" : "Loading the trackers from Prowlarr…"}</option>
          <option value=${NO_TRACKER}>Not specified (search Usenet only)</option>
          ${s.trackers.map(t => html`<option value=${t}>${t}</option>`)}
        </select></label>
      ${noTracker && html`<small class="status thelp">Not specified: each name is only searched for on Usenet,
        to see which releases are there. No tracker is asked for the torrent, so nothing can be built from this
        list - choose a tracker for that.</small>`}
      <div class="opts actions"><${Options} value=${opts} onChange=${setOpts} matchTorrent /></div>
      <${Advanced} tracker=${s.tracker && s.tracker !== NO_TRACKER ? s.tracker : ""}
        saved=${() => { if (s.text.trim()) pasted(s.text); }} mode=${mode} setMode=${setMode} noTracker=${noTracker} />
      <label class=${"check" + (noTracker ? " disabled" : "")}
        title=${noTracker ? "Choose a tracker first: without one there is no torrent to build" : ""}>
        <input type="checkbox" checked=${auto} disabled=${noTracker} onChange=${setAuto} />
        Auto start plausible builds when found</label>
      <small class="status thelp">Check searches each ticked name in turn: first Usenet, for a post of the same
        release; then, only if there is one, the tracker, for the torrent. A name is plausible when the post is at least
        as big as the torrent (or, for a season pack, every episode is there). With this ticked, a plausible name's build
        starts as soon as it is found, unless it is already on the jobs list; unticked, nothing is built until you press
        Build ticked.</small>
      <div class="actions">
        <button class="btn primary" disabled=${s.adding || s.reading || !toSearch().length}>${busy.length ? label + " - add to the queue" : label}</button>
        ${risky && html`<span class="twarn tbtnwarn" role="alert">${RISK}</span>
          <label class="switch tswitch" title="Ask before downloading over BitTorrent">
            <input type="checkbox" role="switch" checked=${s.warn} onChange=${(e) => setWarn(e.target.checked)} />
            <span>Warn me</span></label>`}
      </div>
      ${auto && html`<small class="status thelp">"Auto start plausible builds when found" is
        ticked, so this checks and builds in one go: each plausible name's build starts without asking first. Untick it
        to check only, and choose what to build afterwards.</small>`}
      ${busy.length > 0 && html`<small class="status thelp">A list is still being checked: this one is added to the
        queue and checked after it. The lists go on being checked with this page closed.</small>`}
    </form>
    <${PauseButton} />
    ${busy.length > 0 && html`<h2 class="tqhead">Queue</h2>
      ${busy.map((l, i) => html`<${List} key=${l.id} l=${l} place=${i + 1} open=${i === 0} unpicked=${s.unpicked} redraw=${redraw} />`)}`}
    ${ended.length > 0 && html`<h2 class="tqhead">Checked lists</h2>
      ${ended.map((l, i) => html`<${List} key=${l.id} l=${l} open=${i === 0 && !busy.length} unpicked=${s.unpicked} redraw=${redraw} />`)}`}
    ${s.asking && html`<div class="tconfirm" onKeyDown=${(e) => { if (e.key === "Escape") answer(false); }}>
      <div class="tconfirm-card" role="alertdialog" aria-modal="true" aria-labelledby="tconfirm-title" aria-describedby="tconfirm-text">
        <h2 id="tconfirm-title" class="twarn">Download over torrents?</h2>
        <p id="tconfirm-text">${mode === "torrent"
          ? "Usenet is not searched: every name found on the tracker will be downloaded from it over BitTorrent."
          : "Names that cannot be built from Usenet will be downloaded from the tracker over BitTorrent instead."}
          This will download over torrents and could create problems if you have insufficient ratio or you
          do not seed for long enough (hit-and-run).</p>
        <label class="check"><input type="checkbox" checked=${s.nowarn}
          onChange=${(e) => { s.nowarn = e.target.checked; redraw(); }} />Don't warn me again</label>
        <div class="actions">
          <button type="button" class="btn primary" ref=${(el) => el && !el.dataset.f && (el.dataset.f = "1", el.focus())}
            onClick=${() => answer(true)}>OK proceed</button>
          <button type="button" class="btn" onClick=${() => answer(false)}>Cancel</button>
        </div>
      </div>
    </div>`}`;
}
