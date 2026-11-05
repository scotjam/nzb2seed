// The Library tab: seed the TV library from one tracker, a season at a time. Seasons the
// tracker has as the very same release are linked into a torrent (nothing downloaded);
// the rest are built from Usenet within a daily budget and then offered as a swap, which
// only ever happens when you approve it.
import { html, useEffect, useState } from "../lib.js";
import { api, onChange } from "../api.js";
import { openJob, toast } from "../store.js";
import { plural } from "../util.js";
import { Folders } from "../components/folders.js";

const LABEL = {
  waiting: "Waiting its turn", searching: "Searching", no_pack: "No season pack", matching: "Linking the library's files",
  building: "Building from Usenet", budget: "Waiting for download budget", seeding: "Seeding (library's own files)",
  swap: "Ready to swap", swapped: "Swapped", kept: "Kept library files", failed: "Stopped", skipped: "Skipped",
  already_seeded: "Already seeding", complete_stopped: "Complete in qBittorrent (stopped)", in_client: "Already in the torrent client", short: "Short of 100% - handed on",
  moving: "Moving into the library (cross-eyed)", moved: "In the library - original can be deleted",
  original_deleted: "In the library - original deleted",
};
const FILTERS = [["all", "All"], ["swap", "Ready to swap"], ["moved", "Original can be deleted"], ["moving", "Moving (cross-eyed)"],
                 ["open", "To do"], ["seeding", "Seeding"],
                 ["no_pack", "No pack"], ["short", "Short of 100%"], ["failed", "Stopped"], ["already_seeded", "Already seeding"], ["complete_stopped", "Complete (stopped)"],
                 ["in_client", "Already in the torrent client"]];
const gb = (n) => `${((n || 0) / 1024 ** 3).toFixed(1)} GB`;

function formFrom(s) {
  return { indexer: s.indexer || "", roots: [...(s.roots || [])], output_dir: s.output_dir || "",
           daily_gb: s.daily_gb ?? 200, keep_replaced_days: s.keep_replaced_days ?? 14, redownload: !!s.redownload,
           cross_eyed_url: s.cross_eyed_url || "", auto_move: !!s.auto_move, auto_delete: !!s.auto_delete, hold_cross_seed: s.hold_cross_seed !== false };
}
function toSettings(f) {
  return { indexer: f.indexer, roots: f.roots, output_dir: f.output_dir,
           daily_gb: Math.max(0, Number(f.daily_gb) || 0),
           keep_replaced_days: Math.max(0, Number(f.keep_replaced_days) || 0), redownload: !!f.redownload,
           cross_eyed_url: (f.cross_eyed_url || "").trim(), auto_move: !!f.auto_move, auto_delete: !!f.auto_delete, hold_cross_seed: !!f.hold_cross_seed };
}

export function LibraryTab({ active }) {
  const [state, setState] = useState(null);
  const [form, setForm] = useState(formFrom({}));
  const [indexers, setIndexers] = useState([]);
  const [filter, setFilter] = useState("all");
  const [saved, setSaved] = useState("");
  const [browsing, setBrowsing] = useState(null);       // { for: "root" | "output", at: listing }

  // the torrents' folder, and any further library folder, must be on the first library
  // folder's drive (hard links do not cross drives): the chooser stays on that drive
  async function browse(path, purpose = browsing?.for) {
    const within = form.roots[0] || "";
    if (purpose === "output" && !within) return toast("Choose a TV library folder first - the torrents go on its drive.");
    try { setBrowsing({ for: purpose, at: await api("/api/folders", { path, within }) }); }
    catch (err) {
      if (path && within) return browse("", purpose);        // not on that drive: start at its top
      toast("Could not list folders: " + err.message);
    }
  }
  function use(p) {
    if (browsing.for === "root") setForm(f => ({ ...f, roots: f.roots.includes(p) ? f.roots : [...f.roots, p] }));
    else setForm(f => ({ ...f, output_dir: p }));
    setBrowsing(null); setSaved("Press Save to keep it.");
  }

  const load = async (fields) => {
    try { const r = await api("/api/library/state", {}); setState(r); if (fields) setForm(formFrom(r.settings)); }
    catch (err) { toast("Could not load: " + err.message); }
  };
  useEffect(() => {
    if (!active) return;
    load(true);
    api("/api/library/indexers", {}).then(r => setIndexers(r.indexers)).catch(err => toast(err.message));
    return onChange("library", () => load(false));
  }, [active]);

  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });
  const s = state?.settings || {};
  // ``now``: how the season looks at once, before the server answers - a click shows at once
  // even on a slow phone connection. When a request fails, the page reloads what the server
  // has: the action may have gone through even though its answer never arrived.
  const act = async (what, key, now) => {
    if (now) setState(st => st && { ...st, seasons: (st.seasons || []).map(x => x.key === key ? { ...x, ...now } : x) });
    try { setState(await api("/api/library/" + what, { key })); }
    catch (err) { toast(err.message); load(false); }
  };

  async function toggle(e) {
    try { setState(await api("/api/library/save", { ...toSettings(form), enabled: e.target.checked })); }
    catch (err) { toast(err.message); setState({ ...state }); }
  }
  // the two automatic ticks take effect at once - no Save needed
  async function autoTick(k, on) {
    if (k === "auto_delete" && on && !confirm("Delete original library folders by themselves?\n\nWARNING - MAY CAUSE DATA LOSS.\n\n"
        + "Once a season's torrent seeds at 100.0% from its new folder in the library, its original season folder is deleted without asking you. This cannot be undone.")) return;
    try { const r = await api("/api/library/save", { [k]: on }); setState(r); setForm(f => ({ ...f, [k]: on })); }
    catch (err) { toast(err.message); }
  }
  async function save() {
    try { const r = await api("/api/library/save", toSettings(form)); setState(r); setForm(formFrom(r.settings)); setSaved("Saved."); }
    catch (err) { toast(err.message); }
  }
  async function scan() {
    try { setState(await api("/api/library/scan", {})); toast("Library scanned."); } catch (err) { toast(err.message); }
  }
  async function approve(it) {
    if (s.cross_eyed_url) {
      if (!confirm(`Move ${it.torrent} into the library?\n\ncross-eyed moves the torrent's folder, and every cross-seed copy linked to it, into\n${(it.dir || "").replace(/\/[^/]*\/?$/, "")}\nunder its own name, next to your season folder, and rechecks each torrent.\n\nYour season folder stays as it is; it is then listed as one you can delete.`)) return;
      return act("approve", it.key, { status: "moving", move_started: false, why: "approved - handing it to cross-eyed" });
    }
    if (!confirm(`Swap ${it.show}, season ${it.season}?\n\nThe library's files for the episodes in\n${it.torrent}\nare moved aside and replaced by hard links to the torrent's files.\n\nThe old files are kept for ${s.keep_replaced_days} days in the ".nzb2seed-replaced" folder next to the library, then deleted.`)) return;
    act("approve", it.key, { running: true, why: "swapping…" });
  }

  const counts = state?.counts || {};
  const seasons = state?.seasons || [];
  const shown = seasons.filter(it => filter === "all" || it.status === filter
    || (filter === "open" && ["waiting", "budget", "searching", "matching", "building"].includes(it.status)));
  const swaps = counts.swap || 0;
  const moved = counts.moved || 0;
  async function deleteOriginal(it) {
    if (!confirm(`Delete your original folder of ${it.show}, season ${it.season}?\n\n${it.dir}\n${gb(it.size)}\n\nIts torrent seeds at 100.0% from\n${it.root}\nThis cannot be undone.`)) return;
    act("delete_original", it.key);
  }
  return html`
    <div class="autotop">
      <div>
        <h1 style="margin:0">Library</h1>
        <p class="lede" style="margin:.3em 0 0">Seeds your TV library from one tracker, a season at a time. Where the tracker has the very release you have, your files are linked into its torrent - nothing is downloaded. Otherwise its season pack is built from Usenet, and once it seeds at 100.0% you decide whether it replaces your files.</p>
        <p class="lede" style="margin:.3em 0 0">Useful if your library came from other sources - Usenet,
          Sonarr/Radarr, a tracker that has since closed - and you want it to boost your torrent tracker stats
          (seeding size, ratio, bonus points, seeding time) without downloading it again over BitTorrent.</p>
      </div>
      <label class="switch"><input type="checkbox" role="switch" checked=${!!s.enabled} onChange=${toggle} /> <span>${s.enabled ? "On" : "Off"}</span></label>
    </div>
    <p class="status" role="status">${!state ? "" : s.enabled
      ? (state.current ? "Working on a season now - see Jobs for its log." : `On: the next season starts within a minute. Downloaded today: ${gb(state.spent_today)} of ${s.daily_gb} GB.`)
      : "Off: nothing new is started (a season already under way carries on)."}</p>
    ${state && state.hook && state.hook.held ? html`<p class="status">${state.hook.held} cross-seed call(s) held - run a few a minute once the library is idle.</p>` : null}
    <div class="autoticks">
      <label class="check"><input type="checkbox" checked=${s.hold_cross_seed !== false}
          onChange=${(e) => autoTick("hold_cross_seed", e.target.checked)} />
        <span>Hold back cross-seed while a season is being built or moved
          <small class="status" style="display:block">qBittorrent's on-finished program (cross-seed's search) goes through nzb2seed while the Library is on, and waits while it is busy - then runs, a few a minute. Its own setting is saved and put back when this or the Library is switched off.</small></span></label>
      <label class="check"><input type="checkbox" checked=${!!s.auto_move} disabled=${!s.cross_eyed_url}
          onChange=${(e) => autoTick("auto_move", e.target.checked)} />
        <span>Auto move new torrents to the existing library location using cross-eyed
          <small class="status" style="display:block">${s.cross_eyed_url ? "Each season ready to swap is moved into its show's folder by itself, one at a time." : "Needs cross-eyed's address (below)."}</small></span></label>
      <label class="check danger"><input type="checkbox" checked=${!!s.auto_delete} disabled=${!s.cross_eyed_url}
          onChange=${(e) => autoTick("auto_delete", e.target.checked)} />
        <span>Auto delete the existing library files once moved using <a href="https://codeberg.org/cross-eyed/cross-eyed" target="_blank" rel="noopener noreferrer">cross-eyed</a>: <b>WARNING - MAY CAUSE DATA LOSS</b>
          <small class="status" style="display:block">The original season folder is deleted once the moved torrent seeds at 100.0% from the library and nothing in qBittorrent uses the old folder. cross-eyed: <a href="https://codeberg.org/cross-eyed/cross-eyed" target="_blank" rel="noopener noreferrer">codeberg.org/cross-eyed/cross-eyed</a></small></span></label>
    </div>
    <div class="autogrid">
      <div class="panel">
        <h2>Tracker and folders</h2>
        <label class="field"><span>Tracker every season pack comes from</span>
          <select value=${form.indexer} onChange=${set("indexer")}>
            <option value="">- choose -</option>
            ${[...new Set([form.indexer, ...indexers].filter(Boolean))].map(n => html`<option value=${n}>${n}</option>`)}
          </select></label>
        <div class="field" style="margin-top:8px"><span>TV library folders (each holding Show/Season N/...)</span>
          ${form.roots.length ? html`<ul class="pathlist">${form.roots.map(r => html`<li key=${r}><code>${r}</code>
              <button type="button" class="btn" onClick=${() => setForm(f => ({ ...f, roots: f.roots.filter(x => x !== r) }))}>Remove</button></li>`)}</ul>`
            : html`<small class="status">None yet.</small>`}
          <div class="actions"><button type="button" class="btn" onClick=${() => browse("", "root")}>${form.roots.length ? "Add a folder on the same drive…" : "Add a folder…"}</button></div>
          ${browsing?.for === "root" && html`<${Folders} at=${browsing.at} browse=${(p) => browse(p)} use=${use} />`}
        </div>
        <div class="field" style="margin-top:8px"><span>Where the torrents go (on the same disk as the library)</span>
          <div class="actions"><code>${form.output_dir || (state?.output_used
              ? `not chosen - the usual torrents folder ${state.output_used} is on this drive, so it is used`
              : `not chosen${state?.default_output ? ` - the usual torrents folder (${state.default_output}) is not on the library's drive` : ""}`)}</code>
            <button type="button" class="btn" disabled=${!form.roots.length} title=${form.roots.length ? "" : "Choose a TV library folder first"}
              onClick=${() => browse(form.output_dir || "", "output")}>Choose…</button></div>
          ${browsing?.for === "output" && html`<${Folders} at=${browsing.at} browse=${(p) => browse(p)} use=${use} />`}
        </div>
        <div class="two" style="margin-top:8px">
          <label class="field"><span>Usenet downloads a day (GB)</span>
            <input type="number" min="0" step="10" value=${form.daily_gb} onInput=${set("daily_gb")}
              title="For seasons the tracker only has as another release: each is built from Usenet in full. Seasons that link to your own files do not count." /></label>
          <label class="field"><span>Keep replaced files (days)</span>
            <input type="number" min="0" step="1" value=${form.keep_replaced_days} onInput=${set("keep_replaced_days")}
              title="After a swap your old files wait in .nzb2seed-replaced next to the library this long, then are deleted. 0 keeps them until you delete them." /></label>
        </div>
        <label class="field" style="margin-top:8px"><span>cross-eyed address (blank = swap by hard links)</span>
          <input type="text" placeholder="http://127.0.0.1:8765" value=${form.cross_eyed_url} onInput=${set("cross_eyed_url")}
            title="With cross-eyed, approving a season moves its torrent (and its cross-seed copies) into the show's folder under its own name; your season folder is kept until you delete it." /></label>
        <label class="check" style="display:flex;gap:8px;align-items:baseline;margin-top:10px">
          <input type="checkbox" checked=${form.redownload} onChange=${(e) => { setForm({ ...form, redownload: e.target.checked }); setSaved("Press Save to keep it."); }} />
          <span>Redownload for this tracker (${form.indexer || "the chosen tracker"}) even if already in torrent client
            <small class="status" style="display:block">Off: a season qBittorrent already has a pack of (another release, or from another tracker) is left alone. A season this tracker already seeds is always left alone.</small></span></label>
        <div class="actions" style="margin-top:12px"><button type="button" class="btn primary" onClick=${save}>Save</button>
          <button type="button" class="btn" onClick=${scan}>Scan the library now</button>
          <small class="status">${saved}</small></div>
      </div>
      <div class="panel">
        <h2>Progress</h2>
        <div class="state" style="display:flex;flex-wrap:wrap;gap:6px 14px">
          ${Object.entries(LABEL).filter(([k]) => counts[k]).map(([k, v]) => html`<span>${v}: <b>${counts[k]}</b></span>`)}
          ${!seasons.length && html`<span class="status">Nothing scanned yet - add the library's folders and Save.</span>`}
        </div>
        ${moved > 0 && html`<p class="status" style="margin-top:10px"><b>${plural(moved, "season")} seed from the library</b> - their original folders (${gb(state.deletable_bytes)}) can be deleted: filter "Original can be deleted".</p>`}
        ${swaps > 0 && html`<p class="status" style="margin-top:10px"><b>${plural(swaps, "season")} ready to swap</b> - they seed at 100.0% now; your files stay as they are until you approve each one.</p>`}
        <small class="status" style="display:block;margin-top:10px">Only single-season packs with every episode you have are used. Seasons are taken in alphabetical order; Prowlarr searches count against the Automatic tab's hourly limit.</small>
      </div>
    </div>
    <div class="panel" style="margin-top:20px">
      <div class="ahead"><h2>Seasons</h2>
        <select value=${filter} onChange=${(e) => setFilter(e.target.value)} aria-label="Show">
          ${FILTERS.map(([k, v]) => html`<option value=${k}>${v}</option>`)}
        </select></div>
      <div class="alist">
        ${shown.length ? shown.map(it => html`<${SeasonRow} key=${it.key} it=${it} act=${act} approve=${approve} deleteOriginal=${deleteOriginal} tracker=${s.indexer} moveWith=${!!s.cross_eyed_url} />`)
          : html`<div class="empty">Nothing here.</div>`}
      </div>
    </div>`;
}

function SeasonRow({ it, act, approve, deleteOriginal, tracker, moveWith }) {
  const offer = it.offer && it.offer.indexer === tracker ? it.offer : null;
  return html`
    <div class="arow">
      <div class="title">${it.show} - season ${it.season}</div>
      <div class="meta">
        <span class=${"st " + (["swap", "complete_stopped"].includes(it.status) ? "waiting" : it.status === "failed" ? "failed" : ["seeding", "swapped", "moved", "original_deleted"].includes(it.status) ? "done" : "")}>${LABEL[it.status] || it.status}</span>
      </div>
      <div class="meta"><span><b>Original${it.status === "original_deleted" ? " (deleted)" : ""}:</b> ${it.release || (it.dir || "").split("/").pop()}</span>
        <span>${plural(it.episodes || 0, "episode")}, ${gb(it.size)}</span>
        ${it.dir ? html`<span>in ${it.dir.split("/").pop()}</span>` : null}</div>
      ${it.torrent ? html`<div class="meta"><span><b>New:</b> ${it.torrent}</span>
        ${it.new_size != null ? html`<span>${it.new_files != null ? `${plural(it.new_files, "file")}, ` : ""}${gb(it.new_size)}</span>` : null}
        ${it.tracker ? html`<span>${it.tracker}</span>` : null}
        ${it.qbit ? html`<span class=${"qb " + (it.qbit === "seeding" ? "ok" : /^stopped|missing|not in qB/.test(it.qbit) ? "warn" : "")}>qBittorrent: ${it.qbit}</span>` : null}
        ${it.moved_to ? html`<span>in ${it.moved_to.split("/").pop()}</span>` : null}</div>` : null}
      ${it.why && html`<div class="status">${it.why}</div>`}
      ${!it.running && html`<div class="actions">
        ${it.status === "swap" && html`<button type="button" class="btn primary" onClick=${() => approve(it)}>${moveWith ? "Move into the library" : "Approve swap"}</button>
          <button type="button" class="btn" onClick=${() => act("keep", it.key)} title="Keep your files; the torrent goes on seeding its own copy">Keep mine</button>`}
        ${it.status === "moved" && html`<button type="button" class="btn" onClick=${() => deleteOriginal(it)}>Delete original (${gb(it.size)})</button>`}
        ${it.status === "moving" && html`<span class="status"><b>Approved</b>${it.move_started ? " - cross-eyed is moving it; it can no longer be cancelled" : ""}</span>
          ${!it.move_started && html`<button type="button" class="btn" title="Possible until cross-eyed starts the move"
            onClick=${() => act("cancel_move", it.key)}>Cancel</button>`}`}
        ${["failed", "no_pack", "skipped", "kept", "budget", "short"].includes(it.status) && html`<button type="button" class="btn" onClick=${() => act("retry", it.key)}>Try again</button>`}
        ${["in_client", "already_seeded", "complete_stopped"].includes(it.status) && tracker && !offer && html`<button type="button" class="btn"
            title="Search ${tracker} for this season once and show its pack - nothing is downloaded" onClick=${() => act("check", it.key)}>Check ${tracker}'s pack</button>`}
        ${["in_client", "already_seeded", "complete_stopped"].includes(it.status) && tracker && !(offer && offer.none) && html`<button type="button" class="btn"
            title="Build or link this tracker's season pack even though qBittorrent has this season already" onClick=${() => act("again", it.key)}>Download again for this tracker (${tracker})</button>`}
        ${["waiting", "budget", "no_pack", "failed", "short"].includes(it.status) && html`<button type="button" class="btn" onClick=${() => act("skip", it.key)}>Skip</button>`}
      </div>`}
      ${["in_client", "already_seeded", "complete_stopped"].includes(it.status) && tracker && html`<div class="status">${!offer
        ? `${tracker} is not searched for seasons qBittorrent has already by default - "Check ${tracker}'s pack" looks once`
        : offer.none ? (offer.why || `${tracker} has no single-season pack of it`)
        : html`${tracker}'s pack: <b>${offer.title}</b> - ${gb(offer.size)}${offer.files != null ? `, ${plural(offer.files, "file")}` : ""}${offer.seeders != null && offer.seeders >= 0 ? `, ${plural(offer.seeders, "seeder")}` : ""}${offer.same ? " - the same release as yours (linked, nothing downloaded)" : ""}`}</div>`}
    </div>`;
}
