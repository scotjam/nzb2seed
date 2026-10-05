// The Build tab: search Prowlarr (or upload a .torrent), tick torrents on the left, the
// NZBs for each on the right, and build. Several torrents can be ticked at once; each
// keeps its own picked NZBs, and one of them is the one whose NZBs are being edited.
import { html, useEffect, useReducer, useRef } from "../lib.js";
import { api } from "../api.js";
import { jobs, openJob, refreshJobs, toast, useStore } from "../store.js";
import { age, fileB64, filterMatch, groupOf, keep, norm, pageLink, remember, size, sortBy, sortKey } from "../util.js";
import { Options, useOptions } from "../components/options.js";
import { SortSelect } from "../components/sort.js";
import { GroupChips } from "../components/groups.js";
import { Digest, isWhole, withNzbs } from "../components/digest.js";
import { LANGUAGES, fitsLanguage } from "../languages.js";

/* What to filter the Usenet side by for the torrent being picked for.

   An NZB that can build this torrent is the same release, so the filter carries what
   identifies it: the title, the season (or episode), the resolution and the release
   group. Several ticked torrents are nearly always seasons of one show or films of one
   series, and those share every other word - the season or the year is what tells them
   apart. Every word has to appear somewhere in the name, so a post that leaves one out
   (an obfuscated name, say) is filtered away: clear the box to see everything again. */
export function usenetFilterFor(title) {
  const clean = title.replace(/\.(mkv|mp4|avi|ts|m2ts)$/i, "");
  const words = norm(clean).replace(/[.\-_]+/g, " ").split(/\s+/).filter(Boolean);
  const out = [];
  const ep = words.findIndex(w => /^s\d{1,3}(e\d{1,3})?$/.test(w));
  const year = words.findIndex(w => /^(19|20)\d{2}$/.test(w));
  if (ep > 0) {
    // the show, without a year the Usenet post may not carry, then the season
    out.push(...words.slice(0, ep).filter(w => !/^(19|20)\d{2}$/.test(w)), words[ep]);
  } else if (year > 0) {
    out.push(...words.slice(0, year + 1));                  // the film, and which one
  } else {
    out.push(...words.slice(0, 4));
  }
  const res = words.find(w => /^(2160|1080|720|576|480)p$/.test(w));
  if (res) out.push(res);
  const group = groupOf(clean);
  if (group) out.push(group.toLowerCase());
  return out.join(" ");
}

/* a torrent is blocked while its own build runs, or once one has succeeded; a failed,
   cancelled or interrupted build can be tried again */
function blockedFor(t, all) {
  const job = all.filter(j => j.kind === "build" && j.title === t.title).sort((a, b) => b.id - a.id)[0];
  return job && ["running", "waiting", "done"].includes(job.status) ? job : null;
}

const fresh = () => ({
  torrents: [], usenet: [], chosen: new Map(), editing: null, pairing: false, starting: false,
  searched: false, autoFilter: "", tfilter: "", tgroup: "", ufilter: "", earlier: [],
  tview: "digest", dsort: remember("dsort", "auto"),
  // what the torrent results show: by default only whole seasons or films, and only those
  // Usenet has the same release group, resolution and season of - untick for everything
  whole: true, withNzbs: true, lang: "any",
  tsort: remember("tsort", "best"), usort: remember("usort", "best"),
  searching: false, finding: false, upload: "Upload .torrent",
});

export function BuildTab({ active }) {
  const s = useRef(fresh()).current;           // one state object, as the build flow reads it
  const [, redraw] = useReducer(n => n + 1, 0);
  const all = useStore(jobs);
  const [opts, setOpts] = useOptions();
  const queryRef = useRef(null), usenetRows = useRef(null);

  const entryFor = (t) => {
    if (!s.chosen.has(t.guid)) s.chosen.set(t.guid, { torrent: t, picked: new Set(), found: new Set(), how: "", paired: false });
    return s.chosen.get(t.guid);
  };
  const current = () => s.editing ? s.chosen.get(s.editing.guid) : null;
  const picked = () => current()?.picked || new Set();
  const found = () => current()?.found || new Set();

  const narrowUsenetTo = (t) => {
    const want = usenetFilterFor(t.title);
    // never overwrite a filter typed by hand - only one this put there, or an empty box
    if (s.ufilter.trim() && s.ufilter !== s.autoFilter) return;
    s.ufilter = want; s.autoFilter = want;
  };
  const loadEarlier = async (t) => {
    s.earlier = []; redraw();
    try {
      const r = await api("/api/previous", { title: t.title });
      if (s.editing !== t) return;
      s.earlier = r.previous; redraw();
    } catch { /* the list is a convenience; a failure here must not block building */ }
  };
  const edit = (t) => { entryFor(t); s.editing = t; narrowUsenetTo(t); redraw(); };

  async function selectTorrent(t) {
    edit(t); loadEarlier(t);
    const e = entryFor(t);
    if (e.paired) return;                     // its NZBs were matched when it was first ticked
    s.pairing = true; redraw();
    try {
      const r = await api("/api/pair", { torrent: t, usenet: s.usenet });
      if (!s.chosen.has(t.guid)) return;      // unticked while it was being matched
      s.usenet = r.usenet;
      e.paired = true;
      r.groups.flat().forEach(g => e.picked.add(g));
      const fallbacks = r.groups.length === 1 && r.groups[0].length > 1;
      e.how = r.groups.length
        ? (r.how.startsWith("exact") ? `Same name as the torrent on ${r.groups[0].length} indexer${r.groups[0].length > 1 ? "s" : ""}.` : `Season pack: ${r.groups.length} episode NZBs.`)
          + (fallbacks ? " nzb2seed looks inside them, skips any smaller than the torrent and downloads the most complete post first; the rest are backups." : "")
        : "Nothing is ticked, so nzb2seed finds the NZBs itself: an NZB of the whole torrent first, then per season, then per episode - always from the release group of the torrent's own files. Tick NZBs to have those tried first.";
    } catch (err) { e.how = "Could not match NZBs: " + err.message; }
    finally { s.pairing = false; redraw(); }
  }
  function dropTorrent(t) {
    s.chosen.delete(t.guid);
    if (s.editing?.guid === t.guid) {
      const next = [...s.chosen.values()][0];
      if (next) { edit(next.torrent); loadEarlier(next.torrent); return; }
      s.editing = null; s.earlier = [];
    }
    redraw();
  }

  async function search(e, again) {
    e.preventDefault();
    if (again) queryRef.current.value = again;
    const query = queryRef.current.value.trim();
    if (!query) return queryRef.current.focus();
    s.searching = true; redraw();
    try {
      const r = await api("/api/search", { query });
      // a new search: the torrents that were ticked are no longer on screen
      Object.assign(s, { torrents: r.torrents, usenet: r.usenet, chosen: new Map(), editing: null,
                         autoFilter: "", tfilter: "", tgroup: "", ufilter: "", earlier: [], searched: true,
                         tview: "digest" });
      const key = norm(query);
      s.torrents.sort((a, b) => (norm(b.title) === key) - (norm(a.title) === key) || a.title.localeCompare(b.title));
      redraw();
      const exact = s.torrents.filter(t => norm(t.title) === key);
      if (exact.length === 1) selectTorrent(exact[0]);
    } catch (err) { toast("Search failed: " + err.message); }
    finally { s.searching = false; redraw(); }
  }
  async function upload(e) {
    const f = e.target.files[0];
    e.target.value = "";                       // choosing the same file again still fires
    if (!f) return;
    s.upload = "Reading…"; redraw();
    try {
      const { torrent } = await api("/api/upload_torrent", { torrent_b64: await fileB64(f), torrent_name: f.name });
      s.upload = "Searching Usenet…"; redraw();
      let usenet = [];
      try { usenet = (await api("/api/search", { query: torrent.title, protocol: "usenet" })).usenet; }
      catch (err) { toast("Usenet search failed: " + err.message + ". The build can still find NZBs itself."); }
      Object.assign(s, { torrents: [torrent], usenet, chosen: new Map(), editing: null, tfilter: "", tgroup: "", ufilter: "",
                         autoFilter: "", earlier: [], searched: true });
      queryRef.current.value = torrent.title;
      selectTorrent(torrent);
    } catch (err) { toast("Could not use that file: " + err.message); }
    finally { s.upload = "Upload .torrent"; redraw(); }
  }
  async function findMore(e) {
    e.preventDefault();
    const query = s.ufilter.trim();
    if (!query) return;
    s.finding = true; redraw();
    try {
      const r = await api("/api/search", { query, protocol: "usenet" });
      const known = new Set(s.usenet.map(u => u.guid));
      const add = r.usenet.filter(u => !known.has(u.guid));
      s.usenet = s.usenet.concat(add);
      if (current()) current().found = new Set(add.map(u => u.guid));
      s.ufilter = "";                          // show the new results, not a filtered view
      if (usenetRows.current) usenetRows.current.scrollTop = 0;
      toast(add.length ? `${add.length} more NZB${add.length > 1 ? "s" : ""} found for “${query}”. Tick the ones that hold the torrent's files.`
                       : `No new NZBs for “${query}”.`);
    } catch (err) { toast("Usenet search failed: " + err.message); }
    finally { s.finding = false; redraw(); }
  }
  async function build() {
    const todo = [...s.chosen.values()].filter(e => !blockedFor(e.torrent, all));
    if (!todo.length) return;
    s.starting = true; redraw();
    const started = [], failed = [];
    try {
      for (const e of todo) {
        const nzbs = s.usenet.filter(u => e.picked.has(u.guid));
        try {
          const r = await api("/api/build", { torrent: e.torrent, nzbs, options: opts });
          started.push(r.id);
        } catch (err) {
          failed.push(`${e.torrent.title}: ${err.message}`);    // one refusal must not stop the rest
        }
      }
      await refreshJobs();
      if (started.length) openJob(started[0]);
      if (failed.length) toast((started.length ? `${started.length} started. ` : "") + "Could not start " + failed[0]);
      else if (started.length > 1) toast(`${started.length} builds started.`);
    } finally { s.starting = false; redraw(); }
  }

  const sortChange = (k) => (e) => { s[k] = e.target.value; keep(k, s[k]); redraw(); };
  // the result filters - a torrent you ticked always stays in view
  const hasNzbs = withNzbs(s.usenet);
  const shownT = s.torrents.filter(t => s.chosen.has(t.guid)
    || ((!s.whole || isWhole(t.title)) && (!s.withNzbs || hasNzbs(t)) && fitsLanguage(t.title, s.lang)));
  const hidden = s.torrents.length - shownT.length;
  // Usenet found nothing at all - usually the spelling: trackers forgive a typo, Usenet
  // indexers do not. Offered: the name the torrents themselves carry
  const asked = (queryRef.current?.value || "").trim();
  const better = !s.usenet.length && s.torrents.length ? namedAs(s.torrents, asked) : null;
  const tip = hidden > 0 && !s.usenet.length && s.withNzbs ? html`<p class="status rtip">
    No NZBs at all were found for “${asked}”, so “Only with NZBs found” hides every torrent - Usenet indexers
    match the spelling exactly, where trackers forgive a typo.
    ${better && html` The torrents are named like <b>${better}</b>:
      <button type="button" class="btn" onClick=${(e) => search(e, better)}>Search for “${better}”</button>`}</p>`
    : hidden > 0 && html`<p class="status rtip">${hidden} more torrent${hidden === 1 ? " is" : "s are"} not shown.
    For more results, untick ${[s.whole && "“Complete seasons or films only”", s.withNzbs && "“Only with NZBs found”"].filter(Boolean).join(" or ") || "nothing"}${s.lang !== "any" ? (s.whole || s.withNzbs ? ", or set the language to Any" : " - set the language to Any") : ""}.</p>`;
  const ts = sortBy(shownT.filter(t => filterMatch(t.title + " " + t.indexer, s.tfilter)
    && (!s.tgroup || groupOf(t.title).toLowerCase() === s.tgroup)), s.tsort);
  const pk = picked(), fd = found();
  // picked NZBs first, then the rest
  const us = s.usenet.filter(u => filterMatch(u.title + " " + u.indexer, s.ufilter) || pk.has(u.guid))
    .sort((a, b) => pk.has(b.guid) - pk.has(a.guid)          // ticked ones stay on top
      || (s.usort === "best" ? fd.has(b.guid) - fd.has(a.guid) || (b.grabs || 0) - (a.grabs || 0) : sortKey(a, b, s.usort)));
  const shownUsenet = s.usenet.filter(u => filterMatch(u.title + " " + u.indexer, s.ufilter));
  const how = s.pairing ? "Looking for the matching NZBs…" : current()?.how || "";
  const tickUsenet = (u, on) => {
    const e = current(); if (!e) return;
    on ? e.picked.add(u.guid) : e.picked.delete(u.guid);
    e.how = ""; redraw();
  };

  return html`
    <form class="search" onSubmit=${search}>
      <input type="text" ref=${queryRef} placeholder="Release name, e.g. Show.S01.1080p.BluRay.x264-GRP" autocomplete="off" aria-label="Search Prowlarr" />
      <button class="btn primary" disabled=${s.searching}>${s.searching ? "Searching…" : "Search"}</button>
      <label class="btn upload" title="Build a .torrent file you already have" aria-busy=${s.upload !== "Upload .torrent" ? "true" : null}>
        <input type="file" accept=".torrent,application/x-bittorrent" aria-label="Upload a .torrent file" onChange=${upload} />${s.upload}</label>
    </form>
    ${!s.searched ? html`
      <div class="intro">
        <h2>Build a torrent from Usenet</h2>
        <ol>
          <li>Search for a release or a season, or upload a .torrent file you already have.</li>
          <li>Pick the torrent on the left; the matching NZBs on the right are ticked for you.</li>
          <li>SABnzbd downloads the best-matching post: +Repair when the torrent holds RARs, +Repair/Unpack when it holds unpacked files, never +Delete.</li>
          <li>The files are arranged exactly like the torrent: Sample, Proof and Subs folders, text files only fixed when that makes their pieces verify, everything else removed.</li>
          <li>qBittorrent gets the torrent stopped, pointed at the files, and rechecked.</li>
          <li>You end with 100.0% and nothing downloaded over BitTorrent.</li>
        </ol>
      </div>` : html`
      <div class="pair">
        <div class="side torrent">
          <div class="head"><h2>Torrent</h2><span class="right"><small>${hidden ? `${shownT.length} of ${s.torrents.length} shown` : `${s.torrents.length} found`}</small>
            <${SortSelect} label="Sort torrents" value=${s.tsort} onChange=${sortChange("tsort")} /></span></div>
          ${s.torrents.length > 1 && html`<div class="tfilters">
            <label class="check"><input type="checkbox" checked=${s.whole} onChange=${(e) => { s.whole = e.target.checked; redraw(); }} /> Complete seasons or films only</label>
            <label class="check"><input type="checkbox" checked=${s.withNzbs} onChange=${(e) => { s.withNzbs = e.target.checked; redraw(); }} /> Only with NZBs found</label>
            <label class="lang">Language <select aria-label="Language" value=${s.lang} onChange=${(e) => { s.lang = e.target.value; redraw(); }}>
              <option value="any">Any</option><option value="none">None in the name (usually English)</option>
              ${LANGUAGES.map(([k, label]) => html`<option value=${k}>${label}</option>`)}</select></label>
          </div>`}
          ${s.tview === "digest" && s.torrents.length > 1 ? html`
          <${Digest} torrents=${shownT} usenet=${s.usenet} how=${s.dsort} chosen=${s.chosen} tip=${tip}
            setHow=${(v) => { s.dsort = v; keep("dsort", v); redraw(); }}
            pick=${(t, on) => on ? selectTorrent(t) : dropTorrent(t)}
            edit=${(t) => { if (s.editing?.guid !== t.guid) { edit(t); loadEarlier(t); } }}
            full=${() => { s.tview = "full"; redraw(); }} />` : html`
          ${s.torrents.length > 1 && html`<div class="dback"><button type="button" class="btn"
            onClick=${() => { s.tview = "digest"; redraw(); }}>← Summary by release group</button></div>`}
          <div class="filter"><input type="text" placeholder="Filter torrents" aria-label="Filter torrents"
            value=${s.tfilter} onInput=${(e) => { s.tfilter = e.target.value; redraw(); }} /></div>
          <${GroupChips} torrents=${s.torrents} value=${s.tgroup} onChange=${(g) => { s.tgroup = g; redraw(); }} />
          <div class="rows" role="radiogroup" aria-label="Torrent to build">
            ${ts.length ? ts.map(t => {
              const e = s.chosen.get(t.guid);
              const note = !e ? null : s.editing?.guid === t.guid ? html`<span class="new">picking its NZBs</span>`
                : html`<span>${e.picked.size ? `${e.picked.size} NZB${e.picked.size > 1 ? "s" : ""} picked` : "nzb2seed will find its NZBs"}</span>`;
              return html`<${ReleaseRow} key=${t.guid} r=${t} kind="torrent" on=${!!e} extra=${note}
                onPick=${(on) => on ? selectTorrent(t) : dropTorrent(t)}
                onRowClick=${(ev) => {
                  // clicking a ticked row (not its tick box) moves the Usenet side onto that torrent
                  if (ev.target.tagName === "INPUT" || !s.chosen.has(t.guid)) return;
                  if (s.editing?.guid !== t.guid) { ev.preventDefault(); edit(t); loadEarlier(t); }
                }} />`;
            }) : html`<div class="empty">${s.torrents.length ? "Nothing matches the filter." : "No torrents found. Try a shorter search."}</div>`}
          </div>
          ${tip}`}
        </div>
        <div class="side usenet">
          <div class="head"><h2>Usenet</h2><span class="right"><small>${s.usenet.length} found</small>
            <${SortSelect} label="Sort NZBs" value=${s.usort} onChange=${sortChange("usort")} /></span></div>
          ${how && html`<div class="hint">${how}</div>`}
          <form class="filter findrow" onSubmit=${findMore}>
            <input type="text" placeholder="Filter, or search Usenet for other NZBs" aria-label="Filter NZBs, or search Usenet"
              value=${s.ufilter} onInput=${(e) => { s.ufilter = e.target.value; redraw(); }} />
            <button class="btn" disabled=${s.finding}>${s.finding ? "Searching…" : "Search Usenet"}</button>
          </form>
          <div class="usel">
            <button type="button" class="btn" disabled=${!shownUsenet.length || shownUsenet.every(u => pk.has(u.guid)) || !current()}
              onClick=${() => { shownUsenet.forEach(u => pk.add(u.guid)); current().how = ""; redraw(); }}>Select all shown</button>
            <button type="button" class="btn" disabled=${!pk.size} onClick=${() => { pk.clear(); current().how = ""; redraw(); }}>Clear</button>
            <small class="status">${pk.size ? `${pk.size} ticked` : ""}</small>
          </div>
          <div class="rows" ref=${usenetRows} aria-label="NZBs to download">
            ${us.length ? us.map(u => html`<${ReleaseRow} key=${u.guid} r=${u} kind="usenet" on=${pk.has(u.guid)}
                extra=${[s.editing && norm(u.title) === norm(s.editing.title) && html`<span class="same">same name as the torrent</span>`,
                         fd.has(u.guid) && html`<span class="new">from your Usenet search</span>`]}
                onPick=${(on) => tickUsenet(u, on)} />`)
              : html`<div class="empty">${s.usenet.length ? "Nothing matches the filter." : "No Usenet results. Try a different search."}</div>`}
          </div>
          ${s.earlier.length > 0 && html`<${Earlier} list=${s.earlier} />`}
        </div>
      </div>`}
    ${active && s.chosen.size > 0 && html`<${BuildBar} s=${s} all=${all} picked=${pk} opts=${opts} setOpts=${setOpts} build=${build} />`}`;
}

function ReleaseRow({ r, kind, on, extra, onPick, onRowClick }) {
  const meta = [r.indexer, size(r.size), r.files ? r.files + " files" : null,
    kind === "torrent" ? (r.seeders != null ? r.seeders + " seeders" : null) : (r.grabs != null ? r.grabs + " grabs" : null),
    age(r.publish_date)].filter(Boolean);
  return html`
    <label class=${"row" + (on ? " on" : "")} onClick=${onRowClick}>
      <input type="checkbox" name=${kind} checked=${on} aria-label=${r.title} onChange=${(e) => onPick(e.target.checked)} />
      <div><div class="title">${kind === "torrent" ? pageLink(r, r.title) : r.title}</div>
        <div class="meta">${meta.map(m => html`<span>${m}</span>`)}${extra}</div></div>
    </label>`;
}

function Earlier({ list }) {
  const label = { finished: "Finished", failed: "Failed", downloading: "Downloading", used: "Used", gone: "Gone", unknown: "Unknown" };
  return html`
    <div class="earlier">
      <div class="head"><h2>Downloaded on earlier attempts</h2><small>${list.length} earlier download${list.length > 1 ? "s" : ""}</small></div>
      <p class="ehint">nzb2seed reuses finished ones automatically and skips ones that failed.</p>
      <div>${list.map(p => html`
        <div class="erow"><div class="title">${p.title}</div>
          <div class="meta"><span class=${"st " + p.state}>${label[p.state] || p.state}</span>
            ${p.indexer && html`<span>${p.indexer}</span>`}${p.size ? html`<span>${size(p.size)}</span>` : null}
            ${p.submitted ? html`<span>${age(new Date(p.submitted * 1000).toISOString())}</span>` : null}
            <span>${p.note}</span></div></div>`)}</div>
    </div>`;
}

function BuildBar({ s, all, picked, opts, setOpts, build }) {
  const t = s.editing;
  if (!t) return null;
  const entries = [...s.chosen.values()];
  const ready = entries.filter(e => !blockedFor(e.torrent, all));
  let what;
  if (entries.length === 1) {
    const chosen = s.usenet.filter(u => picked.has(u.guid));
    const groups = new Map(); chosen.forEach(u => { const k = norm(u.title); if (!groups.has(k)) groups.set(k, u); });
    const total = [...groups.values()].reduce((n, u) => n + u.size, 0);
    what = html`<b>${t.title}</b><small>${chosen.length
      ? `from ${groups.size} Usenet release${groups.size > 1 ? "s" : ""}: ${size(total)} of Usenet for ${size(t.size)} of torrent` + (total < t.size * 0.97 ? ". This looks too small; some files may be missing." : "")
      : "nzb2seed finds the NZBs itself, closest match first."}</small>`;
  } else {
    const bytes = entries.reduce((n, e) => n + (e.torrent.size || 0), 0);
    const withNzbs = entries.filter(e => e.picked.size).length;
    what = html`<b>${entries.length} torrents, ${size(bytes)}</b><small>${
      (withNzbs ? `${withNzbs} with NZBs picked; the rest are found by nzb2seed. ` : "nzb2seed finds the NZBs itself for each. ")
      + (ready.length < entries.length ? `${entries.length - ready.length} already built or building, and will be left alone. ` : "")
      + "Click a ticked torrent to pick its NZBs."}</small>`;
  }
  const one = entries.length === 1 ? blockedFor(t, all) : null;
  return html`
    <div class="buildbar"><div class="inner">
      <div class="what">${what}</div>
      <div class="opts"><${Options} value=${opts} onChange=${setOpts} /></div>
      <button class="btn primary" disabled=${s.pairing || s.starting || !ready.length} onClick=${build}
        title=${!one ? "" : one.status === "done" ? "This torrent was already built (see Jobs)"
          : "This torrent is being built (see Jobs); you can build it again if that build fails"}>
        ${entries.length > 1 ? `Build ${ready.length} torrents` : "Build"}</button>
    </div></div>`;
}

/* the name most of the torrents found go by (up to their year), when it is not what was
   searched for: "The Odyssey 2026" for a search of "Odessey 2026" */
function namedAs(torrents, asked) {
  const count = new Map();
  for (const t of torrents) {
    const m = t.title.replace(/[._]+/g, " ").match(/^(.+?)\s*\(?((?:19|20)\d\d)\)?(?![0-9])/);
    if (!m) continue;
    const name = `${m[1].trim()} ${m[2]}`;
    count.set(name, (count.get(name) || 0) + 1);
  }
  const best = [...count.entries()].sort((a, b) => b[1] - a[1])[0];
  return best && norm(best[0]) !== norm(asked) ? best[0] : null;
}
