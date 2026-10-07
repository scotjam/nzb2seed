// The Seasons tab: find a show, pick a season (or all), pick the release groups and
// resolutions to take it from, in order, and grab. Grabbed seasons are listed with their
// reports.
import { html, useEffect, useReducer, useRef } from "../lib.js";
import { api } from "../api.js";
import { openJob, settings, toast, useStore } from "../store.js";
import { keep, pad2, plural, remember, size } from "../util.js";

const SOURCES = [["all", "All sources - per season the longest list"], ["tvmaze", "TVmaze"], ["tmdb", "TMDB"],
                 ["tvdb", "TheTVDB"], ["imdb", "IMDb (official dataset)"]];
const SOURCE_NAMES = { tvmaze: "TVmaze", tmdb: "TMDB", tvdb: "TheTVDB", imdb: "IMDb" };
const PACKING_HELP = "Keep scene RARs: releases that were RAR'd end up as their original scene RAR set. A post that is the scene RARs is kept as it is; an obfuscated, re-packed or encrypted post is unpacked, and when its video matches srrDB's CRC the original RARs are rebuilt byte for byte from srrDB's .srr. If that is not possible the release stays unpacked and the report says why. Unpack all: every release is left unpacked.";

export function SeasonsTab({ active }) {
  const cfg = useStore(settings);
  const behaviour = cfg?.settings?.behaviour || {};
  const s = useRef({
    working: "", shows: null, show: null, source: null, matchNames: false, seasons: null, season: "",
    skip: false, library: "", browsing: null, panel: null, finding: false,
    options: null, picked: [], packing: null, packing2: null, res: "",
    serOptions: null, serSeasons: [], serPicked: [], serFinding: false, serInfo: "",
    reports: [], report: null,
  }).current;
  const [, redraw] = useReducer(n => n + 1, 0);
  const qRef = useRef(null), reportRef = useRef(null);
  const source = s.source ?? (behaviour.episode_source || "all");

  const loadReports = async () => {
    try { s.reports = await api("/api/season/reports"); redraw(); } catch { /* the list is a convenience */ }
  };
  useEffect(() => { if (active) loadReports(); }, [active]);

  const skipBody = () => s.skip ? { skip_owned: true, library: s.library } : {};
  const namesBody = () => ({ match_names: s.matchNames });

  async function findShows(e) {
    e.preventDefault();
    const q = qRef.current.value.trim(); if (!q) return;
    s.working = `Looking for "${q}" on TVmaze…`; s.searching = true; redraw();
    try { s.shows = await api("/api/season/shows", { query: q }); }
    catch (err) { toast(err.message); }
    finally { s.working = ""; s.searching = false; redraw(); }
  }
  async function pickShow(show, src = source) {
    s.show = show;
    s.matchNames = remember("matchNames." + show.id, "0") === "1";
    const where = src === "all" ? "TVmaze, TMDB, TheTVDB and IMDb" : SOURCE_NAMES[src];
    s.working = `Reading the seasons of ${show.name} from ${where}…`; s.readingSeasons = true; s.seasons = null; redraw();
    try {
      const seasons = await api("/api/season/seasons", { show_id: show.id, source: src });
      s.seasons = seasons;
      s.season = seasons.length ? String(seasons[0].number) : "all";
      s.panel = null; s.serPicked = []; s.serOptions = null;
    } catch (err) { toast("Could not read the seasons: " + err.message); }
    finally { s.working = ""; s.readingSeasons = false; redraw(); }
  }
  async function browse(path) {
    try { s.browsing = await api("/api/folders", { path }); redraw(); }
    catch (err) { toast("Could not list folders: " + err.message); }
  }
  async function findReleases() {
    if (!s.show) return;
    if (s.season === "all") { s.panel = "series"; s.packing2 = behaviour.season_packing || "scene"; redraw(); return; }
    s.finding = true; redraw();
    try {
      const r = await api("/api/season/options", { show_id: s.show.id, season: Number(s.season), source, ...namesBody() });
      Object.assign(s, { options: r, picked: [], panel: "season", packing: behaviour.season_packing || "scene" });
    } catch (err) { toast("Search failed: " + err.message); }
    finally { s.finding = false; redraw(); }
  }
  async function grab() {
    if (!s.picked.length) return;
    const first = s.picked[0], season = Number(s.season);
    try {
      const r = await api("/api/season/grab", { show_id: s.show.id, season, keys: s.picked.map(o => o.key), ...namesBody(),
        packing: s.packing, source, ...skipBody(),
        label: s.picked.length > 1 ? `${s.show.name} S${pad2(season)} (${s.picked.length} releases)`
                                   : (first.name || `${s.show.name} S${season}`) });
      openJob(r.id);
    } catch (err) { toast("Could not start the grab: " + err.message); }
  }
  async function findSeries() {
    if (!s.show) return;
    s.serFinding = true; redraw();
    try {
      const r = await api("/api/series/options", { show_id: s.show.id, source, ...skipBody(), ...namesBody() });
      const missing = r.seasons.reduce((n, x) => n + x.missing, 0);
      Object.assign(s, { serOptions: r.options, serSeasons: r.seasons, serPicked: [],
        serInfo: r.seasons.map(x => `S${pad2(x.season)}: ${x.missing} of ${x.episodes} missing`).join(" · ")
          + ` - ${missing} missing in all. ` + (r.options.length ? `${r.options.length} release group/resolution combinations on Usenet.` : "Nothing on Usenet.") });
    } catch (err) { toast("Search failed: " + err.message); }
    finally { s.serFinding = false; redraw(); }
  }
  async function seriesJob(planOnly) {
    if (!s.show) return;
    try {
      const r = await api("/api/series/grab", { show_id: s.show.id, packing: s.packing2, res: s.res, source, ...namesBody(),
        priority: s.serPicked.map(o => o.key), plan_only: planOnly, label: s.show.name, ...skipBody() });
      openJob(r.id);
    } catch (err) { toast("Could not start: " + err.message); }
  }
  async function showReport(name) {
    try { s.report = { name, ...(await api("/api/season/report?name=" + encodeURIComponent(name))) }; redraw(); }
    catch (err) { toast(err.message); return; }
    reportRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  const opts = s.options;
  return html`
    <h1>Grab a season, or the whole series, from Usenet</h1>
    <p class="lede">One release group, one resolution, every episode. nzb2seed tries a season NZB first, then the missing episodes on their own, checks every episode against TVmaze and - for scene releases - against srrDB's CRC of the original video, and puts MediaInfo, screenshots and links in a separate <code>-metadata</code> folder next to the season, without searching for or downloading any torrent files.</p>
    <div class="sgrid">
      <div>
        <div class="panel">
          <h2>Find the season</h2>
          <form class="search" style="margin:0" onSubmit=${findShows}>
            <input type="text" ref=${qRef} placeholder="Show name, e.g. Doctor Who" aria-label="Show name" />
            <button class="btn primary" disabled=${s.searching}>Search</button>
          </form>
          ${s.shows && html`<div class="picklist" role="radiogroup" aria-label="Shows">
            ${s.shows.length ? s.shows.map(sh => html`
              <label class=${"row" + (s.show?.id === sh.id ? " on" : "")}>
                <input type="radio" name="sshow" aria-label=${sh.name} checked=${s.show?.id === sh.id} onChange=${() => pickShow(sh)} />
                <div><div class="title">${sh.name}</div>
                  <div class="meta"><span>${sh.premiered ? sh.premiered.slice(0, 4) : "year unknown"}</span>
                    ${sh.network && html`<span>${sh.network}</span>`}${sh.imdb && html`<span>IMDb ${sh.imdb}</span>`}</div></div>
              </label>`) : html`<div class="empty">No show found on TVmaze.</div>`}
          </div>`}
          <div class=${"working" + (s.working ? " on" : "")} role="status" aria-live="polite">
            <span class="spin" aria-hidden="true"></span><span>${s.working}</span></div>
          ${s.seasons && html`<div style="margin-top:12px">
            <label class="field"><span>Episode list from</span>
              <select style="width:auto" value=${source} disabled=${s.readingSeasons}
                onChange=${(e) => { s.source = e.target.value; pickShow(s.show, s.source); }}>
                ${SOURCES.map(([v, l]) => html`<option value=${v}>${l}</option>`)}</select></label>
            <label class="check" style="margin-top:8px"><input type="checkbox" checked=${s.matchNames}
              onChange=${(e) => { s.matchNames = e.target.checked; keep("matchNames." + s.show.id, s.matchNames ? "1" : "0"); redraw(); }} /> Also find posts named only by episode title</label>
            <small class="status" style="display:block;margin:2px 0 0 26px">For shows posted as "Show De Dierenwinkel - GRP" instead of S01E14: matched by the episode names on TMDB, TheTVDB and TVmaze (only when a name belongs to exactly one episode); files named that way in your folder count as yours. Takes more indexer API searches. Remembered per show.</small>
            <label class="field" style="margin-top:8px"><span>Season</span>
              <select style="width:auto" value=${s.season} onChange=${(e) => { s.season = e.target.value; redraw(); }}>
                <option value="all">All seasons (${s.seasons.length})</option>
                ${s.seasons.map(x => html`<option value=${String(x.number)}>${`Season ${x.number}`
                  + (x.episodes ? ` (${x.episodes} episodes` + (x.source ? `, ${x.source}` : "") + ")" : "")
                  + (x.premiere ? `, ${x.premiere.slice(0, 4)}` : "")}</option>`)}</select></label>
            <div class="owned">
              <label class="check"><input type="checkbox" checked=${s.skip}
                onChange=${(e) => { s.skip = e.target.checked; if (!s.skip) s.browsing = null; redraw(); }} /> Skip episodes I already have</label>
              <fieldset disabled=${!s.skip}>
                <small class="status">Episodes in this show's folder (any layout underneath) and in qBittorrent are not downloaded - any copy counts, whatever its group or resolution.</small>
                <div class="chosen"><span>Show folder:</span> <code>${s.library || "none chosen - only qBittorrent is checked"}</code>
                  <button type="button" class="btn" onClick=${() => browse(s.library || "")}>Choose folder…</button></div>
                ${s.browsing && html`<${Folders} at=${s.browsing} browse=${browse}
                  use=${() => { s.library = s.browsing.path; s.browsing = null; redraw(); }} />`}
              </fieldset>
            </div>
            <div class="actions" style="margin-top:10px">
              <button class="btn primary" disabled=${s.finding} onClick=${findReleases}>${s.finding ? "Searching…" : "Find releases"}</button>
              <span class="status">${opts && s.panel === "season" && Object.entries(opts.links).map(([k, u]) =>
                html`<a href=${u} target="_blank" rel="noopener" style="margin-right:10px">${k}</a>`)}</span></div>
          </div>`}
        </div>
        ${s.panel === "season" && opts && html`<${SeasonPanel} s=${s} opts=${opts} redraw=${redraw} grab=${grab} />`}
        ${s.panel === "series" && html`<${SeriesPanel} s=${s} redraw=${redraw} find=${findSeries} run=${seriesJob} />`}
        ${s.report && html`<div class="panel report" ref=${reportRef}><${Report} r=${s.report} /></div>`}
      </div>
      <div class="panel">
        <h2>Grabbed seasons</h2>
        <div class="replist">
          ${s.reports.length ? s.reports.map(r => html`<button onClick=${() => showReport(r.name)}>
              <div class="t">${r.name}</div>
              <div class="s">${`${r.episodes}/${r.total} episodes · ` + (r.created ? new Date(r.created * 1000).toLocaleDateString() : "")}</div></button>`)
            : html`<div class="empty">Nothing grabbed yet.</div>`}
        </div>
      </div>
    </div>`;
}

function Folders({ at, browse, use }) {
  return html`
    <div class="folders">
      <div class="here"><span>${at.path || "Data folders"}</span>
        <span class="actions"><button type="button" class="btn" disabled=${at.parent === null} onClick=${() => browse(at.parent || "")}>Up</button>
          <button type="button" class="btn primary" disabled=${!at.path} onClick=${use}>Use this folder</button></span></div>
      <div class="list" role="list">
        ${at.dirs.length ? at.dirs.map(d => html`<button type="button" role="listitem" onClick=${() => browse(d.path)}>${d.name + "/"}</button>`)
          : html`<div class="empty" style="padding:8px 12px">${at.path ? "No folders inside." : "No data folders configured (Settings → Paths)."}</div>`}
      </div>
    </div>`;
}

/* picked options, highest priority first, with up / down / remove */
function Priority({ list, setList, label, small }) {
  const move = (i, d) => {
    const j = i + d; if (j < 0 || j >= list.length) return;
    const next = [...list]; [next[i], next[j]] = [next[j], next[i]]; setList(next);
  };
  return html`<ol>${list.map((o, i) => {
    const name = label(o);
    return html`<li key=${o.key}>
      <div class="n">${name}<small>${small(o)}</small></div>
      <div class="b">
        <button type="button" class="btn up" aria-label=${`Move ${name} up`} disabled=${i === 0} onClick=${() => move(i, -1)}>↑</button>
        <button type="button" class="btn down" aria-label=${`Move ${name} down`} disabled=${i === list.length - 1} onClick=${() => move(i, 1)}>↓</button>
        <button type="button" class="btn" aria-label=${`Remove ${name}`} onClick=${() => setList(list.filter(x => x.key !== o.key))}>✕</button>
      </div></li>`;
  })}</ol>`;
}

function SeasonPanel({ s, opts, redraw, grab }) {
  const all = opts.episodes.length;
  const keys = new Set(s.picked.map(o => o.key));
  const setPicked = (list) => { s.picked = list; redraw(); };
  const covered = new Set(); let seasonNzb = false;
  s.picked.forEach(o => { o.episodes_found.forEach(e => covered.add(e)); seasonNzb ||= o.season_nzbs > 0; });
  const nameOf = (o) => o.name || `${o.group} ${o.res || ""}`;
  return html`
    <div class="panel">
      <h2>${opts.show.name} season ${s.season} on Usenet</h2>
      <p class="status" style="margin:0">${opts.source || "The episode list"} lists ${all} episodes. Pick a release group and resolution.</p>
      <div class="actions" style="margin-top:8px">
        <button type="button" class="btn" onClick=${() => setPicked([...s.picked, ...opts.options.filter(o => !keys.has(o.key))])}>Select all</button>
        <button type="button" class="btn" onClick=${() => setPicked([])}>Clear</button>
        <small class="status">Tick one release, or several: episodes come from the first in the priority list that has them, then the next.</small>
      </div>
      <div class="picklist" role="group" aria-label="Release groups and resolutions">
        ${opts.options.length ? opts.options.map(o => {
          const got = o.episodes_found.length;
          return html`<label class=${"row opt" + (keys.has(o.key) ? " on" : "")}>
            <input type="checkbox" name="sopt" aria-label=${o.name} checked=${keys.has(o.key)}
              onChange=${(e) => setPicked(e.target.checked ? [...s.picked.filter(x => x.key !== o.key), o] : s.picked.filter(x => x.key !== o.key))} />
            <div><div class="title">${nameOf(o)}</div>
              <div class="meta"><span>${o.group + (o.res ? " · " + o.res : "")}</span>
                <span class=${"cov " + (o.complete ? "full" : "part")}>${o.season_nzbs
                  ? `${o.season_nzbs} season NZB${o.season_nzbs > 1 ? "s" : ""}` + (got ? ` + ${got}/${all} episodes` : "") : `${got}/${all} episodes`}</span>
                ${o.size ? html`<span>${size(o.size)}</span>` : null}</div>
              <div class="eplist">${opts.episodes.map(e => html`<span class=${"ep" + (o.season_nzbs || o.episodes_found.includes(e.number) ? "" : " no")}>E${pad2(e.number)}</span>`)}</div></div>
          </label>`;
        }) : html`<div class="empty">No NZBs for this season on your indexers.</div>`}
      </div>
      ${s.picked.length > 1 && html`<div class="prio">
        <h3>Priority <small>${seasonNzb ? `together: a season NZB + ${covered.size}/${all} episodes` : `together: ${covered.size}/${all} episodes`}</small></h3>
        <${Priority} list=${s.picked} setList=${setPicked} label=${nameOf}
          small=${(o) => `${o.group}${o.res ? " · " + o.res : ""} · ` + (o.season_nzbs ? "season NZB" : `${o.episodes_found.length}/${all} episodes`)} />
      </div>`}
      <div class="actions">
        <label title=${PACKING_HELP}>RARs <select style="width:auto" value=${s.packing} onChange=${(e) => { s.packing = e.target.value; redraw(); }}>
          <option value="scene">Keep scene RARs</option><option value="unpack">Unpack all</option></select></label>
        <small class="status" style="flex-basis:100%">${PACKING_HELP}</small>
        <button class="btn primary" disabled=${!s.picked.length} onClick=${grab}>
          ${s.picked.length > 1 ? `Grab season from ${s.picked.length} releases` : "Grab season"}</button>
      </div>
    </div>`;
}

function SeriesPanel({ s, redraw, find, run }) {
  const keys = new Set(s.serPicked.map(o => o.key));
  const setPicked = (list) => { s.serPicked = list; redraw(); };
  const got = new Set();
  s.serPicked.forEach(o => Object.entries(o.episodes).forEach(([n, eps]) => eps.forEach(e => got.add(`${n}x${e}`))));
  const missing = s.serSeasons.reduce((n, x) => n + x.missing, 0);
  const nameOf = (o) => `${o.group}${o.res ? " · " + o.res : ""}`;
  return html`
    <div class="panel">
      <h2>${s.show.name}: all seasons</h2>
      <p class="status" style="margin:0">Every season TVmaze lists, one after another. One release group and resolution for the series - the one covering the most missing episodes - and, for a season without it, that season's most complete option at the same resolution. <b>Show the plan</b> lists each season's choice without downloading anything.</p>
      <div class="actions">
        <label>Resolution <select style="width:auto" value=${s.res} disabled=${s.serPicked.length > 0}
          onChange=${(e) => { s.res = e.target.value; redraw(); }}>
          <option value="">Most complete</option><option value="2160p">2160p</option><option value="1080p">1080p</option>
          <option value="720p">720p</option><option value="576p">576p</option><option value="480p">480p</option></select></label>
        <label>RARs <select style="width:auto" value=${s.packing2} onChange=${(e) => { s.packing2 = e.target.value; redraw(); }}>
          <option value="scene">Keep scene RARs</option><option value="unpack">Unpack all</option></select></label>
        <button class="btn" onClick=${() => run(true)}>Show the plan</button>
        <button class="btn primary" onClick=${() => run(false)}>Grab all seasons</button>
      </div>
      <div class="actions" style="margin-top:10px">
        <button type="button" class="btn" disabled=${s.serFinding} onClick=${find}>${s.serFinding ? "Searching all seasons…" : "Find releases for all seasons"}</button>
        <small class="status">Optional: list every release group and resolution over all seasons, tick the ones to use and put them in order. Without a list, nzb2seed picks per season and fills gaps from other releases at the same resolution.</small>
      </div>
      ${s.serOptions && html`<div>
        <p class="status" style="margin:8px 0 0">${s.serInfo}</p>
        <div class="actions" style="margin-top:8px">
          <button type="button" class="btn" onClick=${() => setPicked([...s.serPicked, ...s.serOptions.filter(o => !keys.has(o.key))])}>Select all</button>
          <button type="button" class="btn" onClick=${() => setPicked([])}>Clear</button>
        </div>
        <div class="picklist" role="group" aria-label="Release groups and resolutions over all seasons">
          ${s.serOptions.length ? s.serOptions.map(o => html`<label class=${"row opt" + (keys.has(o.key) ? " on" : "")}>
            <input type="checkbox" aria-label=${`${o.group} ${o.res || ""}`} checked=${keys.has(o.key)}
              onChange=${(e) => setPicked(e.target.checked ? [...s.serPicked.filter(x => x.key !== o.key), o] : s.serPicked.filter(x => x.key !== o.key))} />
            <div><div class="title">${nameOf(o)}</div>
              <div class="meta"><span class="cov part">${plural(o.total, "missing episode")}</span>
                ${o.season_nzbs ? html`<span>${o.season_nzbs} season NZB${o.season_nzbs > 1 ? "s" : ""}</span>` : null}
                <span>${Object.entries(o.seasons).map(([n, c]) => `S${pad2(n)} ${c}`).join(" · ")}</span></div></div>
          </label>`) : html`<div class="empty">No release of this show on your indexers.</div>`}
        </div>
        ${s.serPicked.length > 0 && html`<div class="prio">
          <h3>Priority <small>together: ${got.size} of ${missing} missing episodes</small></h3>
          <${Priority} list=${s.serPicked} setList=${setPicked} label=${nameOf}
            small=${(o) => `${plural(o.total, "missing episode")} in ${Object.keys(o.seasons).length} season(s)`} />
        </div>`}
      </div>`}
    </div>`;
}

function Report({ r }) {
  const file = (p) => `/api/season/file?name=${encodeURIComponent(r.name)}&path=${encodeURIComponent(p)}`;
  const lookup = (c) => (c.predb && c.predb.pretime
      ? `predb.net: pre ${new Date(c.predb.pretime * 1000).toISOString().slice(0, 16).replace("T", " ")} UTC, ${c.predb.section}` + (c.predb.nuked ? ` · NUKED: ${c.predb.reason}` : "")
      : "predb.net: not found")
    + [["predb.me", c.predb_me], ["xrel.to", c.xrel]].map(([l, x]) => ` · ${l}: ` + (!x ? "not checked" : !x.available ? x.why
      : x.found ? "found" + (x.nuked ? " · NUKED" + (x.reason ? `: ${x.reason}` : "") : "") : "not found")).join("");
  return html`
    <h2>${r.name}</h2>
    <p class="links">${`${r.show.name} season ${r.season} · ${r.group} ${r.res || ""} · ${r.packing || ""} · `}
      ${Object.entries(r.links).map(([k, u]) => html`<a href=${u} target="_blank" rel="noopener">${k}</a>`)}
      <a href=${file("report.html")} target="_blank">report.html</a></p>
    <h3>Episodes (TVmaze)</h3>
    <table><tr><th></th><th>Title</th><th>Aired</th><th></th><th>File</th></tr>
      ${r.episodes.map(e => html`<tr><td>E${pad2(e.number)}</td><td>${e.name}</td><td>${e.airdate}</td>
        <td class=${e.present || e.owned ? "good" : "flag"}>${e.present ? "present" : e.owned ? "already yours" : "MISSING"}</td><td>${e.file || ""}</td></tr>`)}</table>
    ${(r.notes || []).length > 0 && html`<h3>Notes</h3><ul>${r.notes.map(n => html`<li class="flag">${n}</li>`)}</ul>`}
    ${r.releases.map(c => html`
      <h3>${c.release}</h3>
      <p>${lookup(c)}${c.srrdb && html`<a href=${c.srrdb} target="_blank" rel="noopener" style="margin-left:10px">srrDB</a>`}</p>
      ${c.rars && html`<p class=${/^(left|could)/.test(c.rars) ? "flag" : "good"}>RARs: ${c.rars}</p>`}
      ${c.video.map(v => html`<p class=${v.genuine === false ? "flag" : v.genuine ? "good" : ""}>${v.name}: ${v.why}</p>`)}
      <table><tr><th>File of the original release</th><th>Size</th><th>State</th></tr>
        ${c.files.map(f => html`<tr><td>${f.name}</td><td>${f.size ? size(f.size) : ""}</td>
          <td class=${f.state.startsWith("missing") ? "flag" : "good"}>${f.state}</td></tr>`)}</table>
      ${c.nfo && html`<details><summary>${c.nfo.name}</summary><pre class="nfo">${c.nfo.text}</pre></details>`}`)}
    ${r.mediainfo && html`<h3>MediaInfo (${r.mediainfo_of || ""})</h3><pre>${r.mediainfo}</pre>`}
    ${(r.screenshots || []).length > 0 && html`<h3>Screenshots</h3>`}
    <div class="shots">${(r.screenshots || []).map(p => html`<a href=${file(p)} target="_blank"><img src=${file(p)} alt=${p} loading="lazy" /></a>`)}</div>`;
}
