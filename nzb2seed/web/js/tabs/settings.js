// The Settings tab: the whole config as a form. Each field names its place in the config
// ("sabnzbd.api_key") and how its text becomes a value; saving sends it all back.
import { html, useEffect, useState } from "../lib.js";
import { api } from "../api.js";
import { loadSettings, settings as store, toast, useStore } from "../store.js";
import { gb } from "../util.js";

const get = (obj, path) => path.split(".").reduce((o, k) => (o || {})[k], obj);
function put(obj, path, v) { const ks = path.split("."); const last = ks.pop(); ks.reduce((o, k) => (o[k] ??= {}), obj)[last] = v; }

/* the text a field shows for a config value, and the value its text stands for */
const show = (v) => Array.isArray(v) ? v.join(", ") : v ?? "";
function parse(text, type) {
  const v = typeof text === "string" ? text.trim() : text;
  if (type === "ints") return v ? v.split(/[,\s]+/).filter(Boolean).map(Number) : [];
  if (type === "strs") return v ? v.split(/[,\s]+/).filter(Boolean) : [];
  if (type === "names") return v ? v.split(",").map(x => x.trim()).filter(Boolean) : [];   // names may hold spaces
  if (type === "int" || type === "float") return Number(v);
  return v;
}
const SEASON_PACKING_HELP = "Keep scene RARs: releases that were RAR'd end up as their original scene RAR set. A post that is the scene RARs is kept as it is; an obfuscated, re-packed or encrypted post is unpacked, and when its video matches srrDB's CRC the original RARs are rebuilt byte for byte from srrDB's .srr. If that is not possible the release stays unpacked and the report says why. Unpack all: every release is left unpacked.";

export function SettingsTab({ active }) {
  const loaded = useStore(store);
  const [form, setForm] = useState(null);       // key -> text (or true/false for a tick box)
  const [maps, setMaps] = useState({ sab: [], qb: [] });
  const [status, setStatus] = useState({});
  const [tests, setTests] = useState(null);
  const [ret, setRet] = useState(null);
  const [trackers, setTrackers] = useState(null);  // Prowlarr's torrent indexers: {trackers, error}

  const fill = (s) => {
    const f = {};
    for (const [k] of FIELDS) f[k] = get(s.settings, k);
    setForm(f);
    setMaps({ sab: (s.settings.paths.sab_to_local || []).map(p => [...p]), qb: (s.settings.paths.local_to_qbit || []).map(p => [...p]) });
  };
  useEffect(() => {
    if (!active) return;
    loadSettings().then(fill).catch(err => toast(err.message));
    api("/api/trackers").then(setTrackers).catch(err => setTrackers({ trackers: [], error: err.message }));
  }, [active]);

  if (!form || !loaded) return html`<h1>Settings</h1>`;
  const say = (k, v) => setStatus(st => ({ ...st, [k]: v }));
  const field = (k) => ({ value: show(form[k]), onInput: (e) => setForm({ ...form, [k]: e.target.value }) });
  const tick = (k) => ({ checked: !!form[k], onChange: (e) => setForm({ ...form, [k]: e.target.checked }) });
  const T = (k, label, attrs = {}, small = null) => html`<label class="field"><span>${label}</span>
    <input type="text" ...${attrs} ...${field(k)} />${small && html`<small>${small}</small>`}</label>`;
  const P = (k, label, attrs = {}, small = null) => html`<label class="field"><span>${label}</span>
    <input type="password" autocomplete="off" ...${attrs} ...${field(k)} />${small && html`<small>${small}</small>`}</label>`;
  const N = (k, label, attrs = {}) => html`<label class="field"><span>${label}</span><input type="number" ...${attrs} ...${field(k)} /></label>`;
  const C = (k, label) => html`<label class="check"><input type="checkbox" ...${tick(k)} /> ${label}</label>`;
  const S = (k, options, attrs = {}) => html`<select ...${attrs} value=${String(show(form[k]))} onChange=${(e) => setForm({ ...form, [k]: e.target.value })}>
    ${options.map(([v, l]) => html`<option value=${v}>${l}</option>`)}</select>`;

  function readSettings() {
    const out = JSON.parse(JSON.stringify(loaded.settings));
    for (const [k, type] of FIELDS) put(out, k, type === "bool" ? !!form[k] : parse(show(form[k]), type));
    const clean = (rows) => rows.map(r => r.map(x => x.trim())).filter(([a, b]) => a && b);
    out.paths.sab_to_local = clean(maps.sab); out.paths.local_to_qbit = clean(maps.qb);
    return out;
  }
  async function save(e) {
    e.preventDefault();
    if (FIELDS.filter(([, t]) => t === "ints").some(([k]) => show(form[k]).split(/[,\s]+/).filter(Boolean).some(x => isNaN(Number(x)))))
      return toast("Indexer ids and categories are numbers, separated by commas.");
    try { await api("/api/settings", { settings: readSettings() }); say("save", "Saved."); fill(await loadSettings()); }
    catch (err) { say("save", "Not saved: " + err.message); }
  }
  async function test() {
    setTests([["", "Testing…", true]]);
    try {
      const r = await api("/api/test", {});
      const names = { prowlarr: "Prowlarr", sabnzbd: "SABnzbd", qbittorrent: "qBittorrent" };
      setTests(Object.entries(r).map(([k, v]) => [names[k], v.text, v.ok]));
    } catch (err) { setTests([["", err.message, false]]); }
  }
  async function clearCache() {
    try {
      const r = await api("/api/metadata/clear_cache", {});
      say("cache", r.removed ? `Cleared ${r.removed} cached lookup${r.removed > 1 ? "s" : ""}.` : "The cache was already empty.");
    } catch (err) { toast("Could not clear the cache: " + err.message); }
  }
  async function checkSpace() {
    say("space", "Checking…");
    try {
      const r = await api("/api/space");
      if (!r.disks.length) return say("space", r.warning || "No folder to check yet - set the output folder first.");
      const one = (d) => `${d.role || "disk"} ${d.path}: ${(d.free / 1073741824).toFixed(1)} GB free (${d.percent}%`
        + (d.min_gb || d.min_percent
            ? ", limit " + [d.min_gb ? d.min_gb + " GB" : null, d.min_percent ? d.min_percent + "%" : null].filter(Boolean).join(" / ")
            : ", no limit") + ")";
      say("space", r.disks.map(one).join("  ·  ")
        + (!r.limits_set ? "  ·  no limit set" : r.low ? `  ·  BELOW THE LIMIT: ${r.why}` : "  ·  above the limit")
        + (r.holding ? "  ·  downloads are paused waiting for room" : ""));
    } catch (err) { say("space", ""); toast("Could not check: " + err.message); }
  }
  async function previewRetention() {
    say("ret", "Checking…"); setRet(null);
    try {
      // answers for the age on screen, saved or not, switched on or not - nothing is changed
      // 0 is a real answer ("everything"), so only an empty box falls back to the saved age
      const typed = show(form["retention.days"]).toString().trim();
      const days = typed === "" || isNaN(Number(typed)) ? undefined : Number(typed);
      const r = await api("/api/retention/sweep", { dry_run: true, days });
      const total = r.removed.reduce((n, x) => n + x.bytes, 0);
      setRet(r);
      say("ret", r.removed.length
        ? `${r.removed.length} would go at ${r.days} days, freeing ${gb(total)} - nothing has been removed.`
        : `Nothing would go at ${r.days} days (${r.builds} build${r.builds === 1 ? "" : "s"} held, oldest ${r.oldest_days} days).`);
    } catch (err) { say("ret", ""); toast("Could not check: " + err.message); }
  }
  async function removeNow(r) {
    const total = r.removed.reduce((n, x) => n + x.bytes, 0);
    if (!confirm(`Remove ${r.removed.length} build(s) now, freeing ${gb(total)}?\n\n` +
                 `They stop seeding and the files nzb2seed placed are deleted. This cannot be undone.`)) return;
    try {
      const done = await api("/api/retention/sweep", { dry_run: false, days: r.days });
      setRet(null);
      say("ret", `Removed ${done.removed.length} build(s), freeing ${gb(done.bytes)}.`);
    } catch (err) { toast("Could not remove: " + err.message); }
  }

  const two = (...kids) => html`<div class="two">${kids}</div>`;
  const grid = (...kids) => html`<div class="grid">${kids}</div>`;
  return html`
    <h1>Settings</h1>
    <p class="lede">${loaded.exists ? `Saved in ${loaded.path}` : `Nothing saved yet. Saving creates ${loaded.path}.`}</p>
    <form class="form" novalidate onSubmit=${save}>
      <fieldset><legend>Prowlarr</legend>
        ${two(T("prowlarr.url", "URL", { placeholder: "http://nas.local:9696" }), P("prowlarr.api_key", "API key"))}
        ${two(T("prowlarr.indexer_ids", "Only these indexer ids", { placeholder: "All indexers" }),
              T("prowlarr.categories", "Only these categories", { placeholder: "All categories, e.g. 5000, 2000" }))}
      </fieldset>
      <hr />
      <fieldset><legend>SABnzbd</legend>
        ${two(T("sabnzbd.url", "URL", { placeholder: "http://nas.local:8080" }), P("sabnzbd.api_key", "API key", {}, "The full API key, not the NZB key."))}
        ${two(T("sabnzbd.category", "Category", {}, "Use one Sonarr and Radarr do not watch."),
              html`<label class="field"><span>Priority</span>${S("sabnzbd.priority", [["-1", "Low"], ["0", "Normal"], ["1", "High"], ["2", "Force"]])}</label>`)}
        ${C("sabnzbd.delete_history", "Remove finished jobs from SABnzbd history (files are kept)")}
      </fieldset>
      <hr />
      <fieldset><legend>qBittorrent</legend>
        ${two(T("qbittorrent.url", "URL", { placeholder: "http://nas.local:8081" }),
              T("qbittorrent.category", "Category", { placeholder: "nzb2seed", title: "Torrents nzb2seed adds are filed under this category in qBittorrent (created if it does not exist). Empty means nzb2seed; put a single - here to add them with no category at all." }))}
        ${two(T("qbittorrent.username", "User name", { autocomplete: "off" }), P("qbittorrent.password", "Password"))}
        ${T("qbittorrent.tags", "Tags", { placeholder: "nzb2seed" })}
        ${T("qbittorrent.file_owner", "Files belong to", { placeholder: "user:group, e.g. 1000:100", title: "The user and group qBittorrent runs as (PUID:PGID in Docker). Files nzb2seed places are handed to them, so qBittorrent can repair, move and delete them as if it had downloaded them itself. Empty: files are only made writable by the folder's group." })}
        ${C("qbittorrent.start_when_complete", "Start seeding automatically after a 100% recheck")}
      </fieldset>
      <hr />
      <fieldset><legend>Folders</legend>
        <p class="status" style="margin:0">SABnzbd, this machine and qBittorrent can see the same folder under different names (Docker, network shares). Translate the start of each path; the longest match wins.</p>
        <${Maps} label="SABnzbd path → path on this machine" rows=${maps.sab} set=${(sab) => setMaps({ ...maps, sab })} />
        <${Maps} label="Path on this machine → qBittorrent path" rows=${maps.qb} set=${(qb) => setMaps({ ...maps, qb })} />
        ${two(T("paths.output_dir", "Put finished torrents in", { placeholder: "Next to where SABnzbd put the download" }, "Same drive as SABnzbd's complete folder keeps moves instant."),
              T("paths.torrent_dir", "Keep downloaded .torrent files in"))}
      </fieldset>
      <hr />
      <fieldset><legend>FlareSolverr</legend>
        ${T("flaresolverr.url", "URL", { placeholder: "http://nas.local:8191" }, "Only used when a site (e.g. predb.me, xrel.to) answers with a Cloudflare challenge. Leave empty to not use it.")}
      </fieldset>
      <hr />
      <fieldset><legend>Outbound proxy</legend>
        ${T("network.proxy", "Proxy URL", { placeholder: "http://127.0.0.1:8888" }, "nzb2seed never contacts an indexer or tracker: Prowlarr searches and fetches .torrent files, SABnzbd fetches .nzb files. Its only own internet use - the srrDB / predb / xrel.to / TVmaze lookups - goes through this proxy (e.g. the VPN container's HTTP proxy, bound to 127.0.0.1). Empty: those lookups are skipped rather than made from this machine's own address.")}
      </fieldset>
      <hr />
      <fieldset><legend>GUI login</legend>
        <p class="status" style="margin:0">${loaded.login_from_command_line
          ? "The login is currently set on the command line (--username/--password), which overrides these fields."
          : "Asked for by your browser when you open this page. The default is admin / nzb2seed. Leave the password empty to switch the login off; then only this machine and your local network can open the GUI."}</p>
        ${two(T("gui.username", "User name", { autocomplete: "off" }), P("gui.password", "Password", { autocomplete: "new-password" }))}
      </fieldset>
      <hr />
      <fieldset><legend>Defaults for new builds</legend>
        <label class="field"><span>SABnzbd post-processing</span>${S("behaviour.post_processing", [
          ["auto", "Match the torrent: +Repair when it holds RARs, +Repair/Unpack when it holds unpacked files"],
          ["repair", "+Repair: keep archives packed, for torrents that contain the RARs"],
          ["unpack", "+Repair/Unpack: for torrents that contain the unpacked files"]])}</label>
        ${C("behaviour.cleanup", "Delete files that are not part of the torrent (.nzb, .par2 and extras)")}
        ${C("behaviour.local_verify", "Hash every piece here before handing the torrent to qBittorrent")}
        <label class="field"><span>Season grabs: RARs</span>${S("behaviour.season_packing", [
          ["scene", "Keep scene RARs (rebuilt from srrDB when a post is obfuscated or re-packed)"], ["unpack", "Unpack all"]], { title: SEASON_PACKING_HELP })}</label>
        <label class="field"><span>Season grabs: episode list</span>${S("behaviour.episode_source", [
          ["all", "All sources - per season the longest list"], ["tvmaze", "TVmaze"], ["tmdb", "TMDB"], ["tvdb", "TheTVDB"], ["imdb", "IMDb (official dataset)"]])}
          <small>Where a show's episode list comes from. TVmaze's lists are often incomplete for small shows; "All sources" takes, per season, whichever lists the most. Lookups are cached for a day.</small></label>
        <div class="actions"><button type="button" class="btn" onClick=${clearCache}>Clear metadata cache</button>
          <small class="status">${status.cache || "Forgets cached episode lists and show lookups, so the next grab asks TVmaze, TMDB, TheTVDB and IMDb again."}</small></div>
        ${C("behaviour.retry_bad_pieces", "If pieces fail, download other posts of the same release until every piece verifies (switches hashing on)")}
        ${C("behaviour.peek_archives", "Look inside a RAR post before downloading all of it (fetches its first volume only)")}
      </fieldset>
      <fieldset>
        <h2>Only build what pays for its space</h2>
        <p class="status">Automatic builds can be weighed before they start, against what your own torrents have actually returned. The rules themselves live on the <a href="#demand">Demand</a> tab, where each one is shown with the evidence behind it; these two settings decide how much evidence a rule needs.</p>
        ${grid(N("demand.min_sample", "Never refuse on fewer than", { min: "1", step: "1" }),
               N("demand.age_days", "Ignore torrents younger than (days)", { min: "0", step: "1" }))}
      </fieldset>
      <fieldset>
        <h2>Builds that stop a few percent short</h2>
        <p class="status">A build missing less than this much can be handed to qBittorrent to download the rest - many trackers let you download a few percent without it counting towards a hit-and-run. For the trackers listed below it is done without asking; everywhere else you are asked each time.</p>
        ${grid(N("behaviour.nearly_complete_percent", "Offer it when less than (%) is missing", { min: "0", max: "50", step: "0.5" }),
               N("behaviour.nearly_complete_mb", "…and less than (MB) - whichever is less", { min: "1", step: "1", title: "On a big torrent a percentage is a lot of data: 5% of a 20 GB season is 1 GB. The build has to be under both limits." }),
)}
        <${Trackers} prowlarr=${trackers} chosen=${form["behaviour.nearly_auto_trackers"] || []}
          set=${(v) => setForm({ ...form, "behaviour.nearly_auto_trackers": v })} />
      </fieldset>
      <fieldset>
        <h2>Free space</h2>
        <p class="status">Off unless you set a limit. When the disk builds are written to falls below it, SABnzbd's queue is paused, no new automatic build is started, and a build already running stops before its next big write instead of filling the disk - until there is room again - freed by the sweep below, or by you. Only what nzb2seed paused is started again, and seeding is never touched.</p>
        <p class="status">The two disks do different jobs, so each can have its own limit. <b>Downloads</b> is where SABnzbd stages a release: it needs room for whatever is being fetched and unpacked, and empties again afterwards. <b>Output</b> keeps everything being seeded and only grows. A disk left at 0 falls back to the general limit below it.</p>
        ${grid(N("space.downloads_min_percent", "Downloads disk: below (% free)", { min: "0", max: "99", step: "0.5", placeholder: "0 = use the general limit" }),
               N("space.downloads_min_gb", "Downloads disk: below (GB free)", { min: "0", step: "1", placeholder: "0 = use the general limit" }),
               N("space.output_min_percent", "Output disk: below (% free)", { min: "0", max: "99", step: "0.5", placeholder: "0 = use the general limit" }),
               N("space.output_min_gb", "Output disk: below (GB free)", { min: "0", step: "1", placeholder: "0 = use the general limit" }),
               N("space.min_percent", "Any other disk: below (% free)", { min: "0", max: "99", step: "0.5", placeholder: "0 = off" }),
               N("space.min_gb", "Any other disk: below (GB free)", { min: "0", step: "1", placeholder: "0 = off" }))}
        <label class="check"><input type="checkbox" ...${tick("space.pause_torrents")} /> Also stop torrents that are still downloading <small class="status">— pausing a torrent mid-download can cost you a private tracker's hit-and-run grace, so this is off by default. Finished torrents keep seeding either way.</small></label>
        <div class="actions"><button type="button" class="btn" onClick=${checkSpace}>Check free space now</button>
          <small class="status">${status.space || ""}</small></div>
      </fieldset>
      <fieldset>
        <h2>Removing builds again</h2>
        <p class="status">Off unless you switch it on. When it is on, a torrent nzb2seed built is removed from qBittorrent once it is older than the age below, and the files nzb2seed placed for it are deleted. <b>Only files nzb2seed put there go</b> - it deletes from the record each build writes, so anything you added yourself, a metadata folder, or another torrent's data in the same place is left alone. Seeding stops when the torrent is removed, so set an age your trackers are happy with.</p>
        ${C("retention.enabled", "Remove builds after a while")}
        <label>Remove builds older than
          <input type="number" min="0" max="3650" step="1" title="0 means every automatic build, however new - useful for seeing the full list." ...${field("retention.days")} /> days</label>
        <div class="actions"><button type="button" class="btn" onClick=${previewRetention}>Show what would go</button>
          <small class="status">${status.ret || ""}</small></div>
        ${ret && (ret.removed.length || ret.kept.length) > 0 && html`<${Retention} r=${ret} remove=${removeNow} />`}
      </fieldset>
      <div class="actions">
        <button class="btn primary">Save settings</button>
        <button type="button" class="btn" onClick=${test}>Test connections</button>
        <span class="status">${status.save || ""}</span>
      </div>
      ${tests && html`<div class="tests">${tests.map(([name, text, ok]) => html`<div><b>${name}</b><span class=${ok ? "ok" : "bad"}>${text}</span></div>`)}</div>`}
    </form>`;
}

/* every field: its place in the config, and how its text becomes the value */
const FIELDS = [
  ["prowlarr.url"], ["prowlarr.api_key"], ["prowlarr.indexer_ids", "ints"], ["prowlarr.categories", "ints"],
  ["sabnzbd.url"], ["sabnzbd.api_key"], ["sabnzbd.category"], ["sabnzbd.priority", "int"], ["sabnzbd.delete_history", "bool"],
  ["qbittorrent.url"], ["qbittorrent.category"], ["qbittorrent.username"], ["qbittorrent.password"],
  ["qbittorrent.tags", "strs"], ["qbittorrent.file_owner"], ["qbittorrent.start_when_complete", "bool"],
  ["paths.output_dir"], ["paths.torrent_dir"], ["flaresolverr.url"], ["network.proxy"],
  ["gui.username"], ["gui.password"],
  ["behaviour.post_processing"], ["behaviour.cleanup", "bool"], ["behaviour.local_verify", "bool"], ["behaviour.season_packing"],
  ["behaviour.episode_source"], ["behaviour.retry_bad_pieces", "bool"], ["behaviour.peek_archives", "bool"],
  ["demand.min_sample", "int"], ["demand.age_days", "float"],
  ["behaviour.nearly_complete_percent", "float"], ["behaviour.nearly_complete_mb", "float"], ["behaviour.nearly_auto_trackers", "names"],
  ["space.downloads_min_percent", "float"], ["space.downloads_min_gb", "float"], ["space.output_min_percent", "float"],
  ["space.output_min_gb", "float"], ["space.min_percent", "float"], ["space.min_gb", "float"], ["space.pause_torrents", "bool"],
  ["retention.enabled", "bool"], ["retention.days", "int"],
];

/* "SomeTracker (API)" and "sometracker" are the same tracker - as the server has it */
const trackerKey = (n) => { const k = (n || "").trim().toLowerCase(); return k.endsWith("(api)") ? k.slice(0, -5).trim() : k; };

/* the trackers nearly complete builds are always added for: every torrent indexer in
   Prowlarr, ticked if it is approved - plus any approved name Prowlarr does not list */
function Trackers({ prowlarr, chosen, set }) {
  const known = prowlarr && (prowlarr.trackers || []), err = prowlarr?.error || "";
  const on = new Set(chosen.map(trackerKey));
  const listed = new Set((known || []).map(trackerKey));
  const names = [...(known || []), ...chosen.filter(n => !listed.has(trackerKey(n)))];
  const flip = (name, yes) => set(yes ? [...chosen, name] : chosen.filter(n => trackerKey(n) !== trackerKey(name)));
  return html`<div class="field"><span>Always add, for these trackers</span>
    ${known === null ? html`<small class="status">Asking Prowlarr for its trackers…</small>`
      : html`<div class="ticks">${names.map(n => html`<label class="check"><input type="checkbox"
          checked=${on.has(trackerKey(n))} onChange=${(e) => flip(n, e.target.checked)} /> ${n}</label>`)}</div>`}
    <small class="status">${err ? `Prowlarr's trackers could not be listed (${err}) - only the approved ones are shown. `
      : ""}Ticked: a build from that tracker that stops this close is added without asking. Unticked: you are asked each time.</small></div>`;
}

function Maps({ label, rows, set }) {
  const edit = (i, j, v) => set(rows.map((r, k) => k === i ? (j ? [r[0], v] : [v, r[1]]) : r));
  return html`<div class="field"><span>${label}</span>
    <div class="maps">${rows.map((r, i) => html`<div class="maprow">
      <input type="text" value=${r[0]} placeholder="/downloads" aria-label="From" onInput=${(e) => edit(i, 0, e.target.value)} />
      <span class="arrow">→</span>
      <input type="text" value=${r[1]} placeholder="\\\\NAS\\downloads" aria-label="To" onInput=${(e) => edit(i, 1, e.target.value)} />
      <button type="button" class="btn" onClick=${() => set(rows.filter((_, k) => k !== i))}>Remove</button></div>`)}</div>
    <div><button type="button" class="btn" onClick=${() => set([...rows, ["", ""]])}>Add translation</button></div></div>`;
}

function Retention({ r, remove }) {
  const total = r.removed.reduce((n, x) => n + x.bytes, 0);
  // the preview is a list on the page, not a dialog: reading it changes nothing, and
  // removing is a separate button you have to reach for
  return html`<div class="picklist flist" id="retlist" role="group" aria-label="builds that would be removed">
    ${r.removed.map(x => html`<div class="retrow go"><div class="title">${x.name}</div>
      <div class="meta"><span>${x.files} file${x.files === 1 ? "" : "s"}</span><span>${gb(x.bytes)}</span><span>would be removed</span></div></div>`)}
    ${r.kept.map(x => html`<div class="retrow"><div class="title">${x.name}</div><div class="why">kept: ${x.skipped}</div></div>`)}
    ${r.removed.length > 0 && html`<div class="actions"><button type="button" class="btn" onClick=${() => remove(r)}>
      Remove these ${r.removed.length} now and free ${gb(total)}</button></div>`}
  </div>`;
}
