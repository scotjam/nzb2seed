// The Automatic tab: autobrr drops torrents into a folder and nzb2seed builds them by
// itself. The switch, which autobrr filters feed it, how it behaves, and what arrived.
import { html, useEffect, useState } from "../lib.js";
import { api, onChange } from "../api.js";
import { autoOn, openJob, toast, useStore } from "../store.js";
import { plural } from "../util.js";

const FIELDS = { autobrr_url: "", folder: "", autobrr_folder: "", wait_hours: "", retry_first_minutes: "",
                 retry_minutes: "", parallel: "", queue_max: "", searches_per_hour: "", max_age_days: "" };

/* the form as the server has it: filled when the tab opens and after saving - never while
   you type, so a live update cannot overwrite what you are changing */
function formFrom(st) {
  const s = st.settings;
  return { autobrr_url: s.autobrr_url || "", autobrr_key: "", folder: s.folder || "", autobrr_folder: s.autobrr_folder || "",
           wait_hours: s.wait_hours, retry_minutes: s.retry_minutes, retry_first_minutes: s.retry_first_minutes,
           parallel: s.parallel, queue_max: s.queue_max ?? 10, queue_keep_older: !!s.queue_keep_older,
           searches_per_hour: s.searches_per_hour, start: !!s.start, skip_unposted: s.skip_unposted !== false,
           max_age_days: s.max_age_days ?? 2 };
}
function toSettings(f) {
  return { folder: f.folder.trim(), autobrr_folder: f.autobrr_folder.trim(),
    wait_hours: Number(f.wait_hours) || 1, retry_minutes: Number(f.retry_minutes) || 15,
    retry_first_minutes: Number(f.retry_first_minutes) || 15,
    parallel: Number(f.parallel) || 1, searches_per_hour: Number(f.searches_per_hour) || 40,
    queue_max: f.queue_max === "" ? 10 : Math.max(0, Math.floor(Number(f.queue_max) || 0)),
    queue_keep_older: f.queue_keep_older, start: f.start, skip_unposted: f.skip_unposted,
    max_age_days: f.max_age_days === "" ? 2 : Math.max(0, Number(f.max_age_days) || 0),
    autobrr_url: f.autobrr_url.trim(), autobrr_key: f.autobrr_key.trim() };
}

export function AutoTab({ active }) {
  const [state, setState] = useState(null);
  const [form, setForm] = useState({ ...FIELDS, autobrr_key: "", queue_keep_older: false, start: false, skip_unposted: true });
  const [saved, setSaved] = useState("");
  const [abr, setAbr] = useState({ status: "", busy: false, filters: null, applied: "" });

  const paint = (r, fields = false) => {
    setState(r); autoOn.set(!!r.settings.enabled);
    if (fields) setForm(formFrom(r));
  };
  const load = async (fields) => {
    try { paint(await api("/api/auto/state", {}), fields); } catch (err) { toast("Could not load: " + err.message); }
  };
  useEffect(() => {
    if (!active) return;
    load(true);
    return onChange("auto", () => load(false));
  }, [active]);

  const set = (k) => (e) => setForm({ ...form, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value });
  const s = state?.settings || {};
  const act = async (what, infohash) => { try { paint(await api("/api/auto/" + what, { infohash })); } catch (err) { toast(err.message); } };

  async function toggle(e) {
    const on = e.target.checked;
    try { paint(await api("/api/auto/save", { ...toSettings(form), enabled: on })); }
    catch (err) { toast(err.message); setState({ ...state }); }
  }
  async function save() {
    try { paint(await api("/api/auto/save", toSettings(form)), true); setSaved("Saved."); }
    catch (err) { toast(err.message); }
  }
  async function detect() {
    try { const r = await api("/api/auto/detect", {}); setForm({ ...form, folder: r.folder, autobrr_folder: r.autobrr_folder });
      setSaved("Found - press Save to keep it."); }
    catch (err) { toast(err.message); }
  }
  async function listFilters() {
    setAbr({ ...abr, busy: true, status: "Connecting…" });
    try {
      const r = await api("/api/auto/filters", { autobrr_url: form.autobrr_url.trim(), autobrr_key: form.autobrr_key.trim(),
        autobrr_folder: form.autobrr_folder.trim() });
      setAbr({ busy: false, applied: abr.applied, status: `autobrr ${r.version}: ${plural(r.filters.length, "filter")}.`,
        filters: r.filters.map(f => ({ ...f, pick: f.ours, replace: true })) });
    } catch (err) { setAbr({ ...abr, busy: false, status: err.message }); }
  }
  async function apply() {
    const ids = abr.filters.filter(f => f.pick).map(f => f.id);
    const replace = Object.fromEntries(abr.filters.map(f => [f.id, f.replace]));
    try {
      await api("/api/auto/save", toSettings(form));           // the folder and key first
      const r = await api("/api/auto/apply", { filters: ids, replace,
        autobrr_url: form.autobrr_url.trim(), autobrr_key: form.autobrr_key.trim() });
      const applied = `Saved to autobrr: ${r.added} added, ${r.removed} removed`
        + (r.fixed ? `, ${r.fixed} pointed at the inbox` : "")
        + (r.on ? `, ${r.on} filter${r.on > 1 ? "s" : ""} enabled` : "")
        + (r.stopped ? `, ${r.stopped} filter${r.stopped > 1 ? "s" : ""} disabled in autobrr` : "")
        + (r.off ? `, ${r.off} download action${r.off > 1 ? "s" : ""} switched off` : "")
        + (r.back ? `, ${r.back} switched back on` : "") + ".";
      setAbr(a => ({ ...a, applied }));
      listFilters();
    } catch (err) { toast(err.message); }
  }
  async function clearQueue() {
    const n = (state?.items || []).filter(it => ["queued", "waiting"].includes(it.status)).length;
    if (!n) return toast("Nothing is queued.");
    if (!confirm(`Clear all queued automatic jobs?\n\nThe ${n} torrent(s) not building yet are stopped.\n\nThey stay in the list and can be tried again. Builds already running carry on.`)) return;
    try { paint(await api("/api/auto/clear", {})); toast(`Stopped ${n}.`); } catch (err) { toast(err.message); }
  }
  const setFilter = (id, patch) => setAbr(a => ({ ...a, filters: a.filters.map(f => f.id === id ? { ...f, ...patch } : f) }));

  const num = (k, label, attrs = {}) => html`<label class="field"><span>${label}</span>
    <input type="number" ...${attrs} value=${form[k]} onInput=${set(k)} /></label>`;
  return html`
    <div class="autotop">
      <div>
        <h1 style="margin:0">Automatic builds</h1>
        <p class="lede" style="margin:.3em 0 0">autobrr drops torrents from the filters you pick into a folder; nzb2seed builds each one from Usenet by itself - it waits for the post, never asks, never downloads over BitTorrent, and starts seeding only at a 100.0% recheck.</p>
      </div>
      <label class="switch"><input type="checkbox" role="switch" checked=${!!s.enabled} onChange=${toggle} /> <span>${s.enabled ? "On" : "Off"}</span></label>
    </div>
    <p class="status" role="status">${!state ? "" : s.enabled
      ? `Watching ${s.folder || "(no folder)"} - new torrents are picked up within half a minute.`
      : "Off: nothing in the inbox is picked up (builds already running carry on; stop them in Jobs)."}</p>
    <div class="autogrid">
      <div class="panel">
        <h2>autobrr</h2>
        <div class="two">
          <label class="field"><span>URL</span><input type="text" placeholder="http://nas.local:7474" value=${form.autobrr_url} onInput=${set("autobrr_url")} /></label>
          <label class="field"><span>API key</span><input type="password" autocomplete="off" value=${form.autobrr_key} onInput=${set("autobrr_key")}
            placeholder=${state?.has_key ? "saved - leave empty to keep it" : "autobrr: Settings → API keys"} /></label>
        </div>
        <div class="actions" style="margin-top:10px">
          <button type="button" class="btn primary" disabled=${abr.busy} onClick=${listFilters}>Connect and list filters</button>
          <small class="status">${abr.status}</small>
        </div>
        ${abr.filters && html`<div>
          <p class="status" style="margin:10px 0 6px">Tick the filters whose torrents nzb2seed should build. Ticking adds a watch-folder action called "nzb2seed" to the filter and enables the filter in autobrr; <b>unticking removes that action and disables the filter in autobrr</b>, so nothing acts on its releases until you enable it there again. The filter's other actions (qBittorrent, SABnzbd…) are not removed.</p>
          <div class="picklist flist" role="group" aria-label="autobrr filters">
            ${abr.filters.length ? abr.filters.map(f => html`<${FilterRow} key=${f.id} f=${f} set=${(p) => setFilter(f.id, p)} />`)
              : html`<div class="empty">autobrr has no filters yet.</div>`}
          </div>
          <small class="status" style="display:block;margin:8px 0 0">A ticked filter is enabled in autobrr and its releases come to nzb2seed. Unticking puts that filter and its actions back exactly as they were.</small>
          <div class="actions" style="margin-top:10px">
            <button type="button" class="btn primary" onClick=${apply}>Save to autobrr</button>
            <small class="status">${abr.applied}</small>
          </div>
        </div>`}
      </div>
      <div class="panel">
        <h2>Inbox and behaviour</h2>
        <label class="field"><span>Inbox folder (as the NAS sees it)</span><input type="text" value=${form.folder} onInput=${set("folder")} /></label>
        <label class="field" style="margin-top:8px"><span>The same folder as autobrr sees it</span>
          <input type="text" placeholder="/config/nzb2seed-inbox" value=${form.autobrr_folder} onInput=${set("autobrr_folder")} /></label>
        <div class="actions" style="margin-top:8px"><button type="button" class="btn" onClick=${detect}>Find autobrr's folder</button>
          <small class="status">Uses autobrr's own config folder, which it can already write to - no change to autobrr's container needed.</small></div>
        <hr />
        <div class="two">
          ${num("wait_hours", "Keep trying for (hours)", { min: "0", step: "0.05", title: "How long to keep looking for the Usenet post before giving up. 1 hour by default: a release that is posted at all usually appears within the hour (a fresh one can take half an hour or more), and one still missing after that is usually a tracker-internal encode that never will be." })}
          ${num("retry_first_minutes", "First retry after (minutes)", { min: "1", step: "1", title: "How long after the first try to look again. 15 minutes by default." })}
          ${num("retry_minutes", "…backing off to at most (minutes)", { min: "1", step: "1", title: "The gap doubles after each try up to this - 15 minutes by default, the same as the first, so every 15 minutes. A release still missing after an hour is usually a tracker's own encode that will never be posted, and every look costs indexer searches." })}
          ${num("parallel", "Builds at once", { min: "1", max: "8", step: "1" })}
          ${num("queue_max", "Queued at once (0 = no cap)", { min: "0", step: "1", title: "The most torrents waiting their turn at a time, not counting builds already running. When the queue is full, the oldest waiting one is stopped to make room for a new arrival (or the newcomer is, with the box below ticked). Stopped ones stay in the list and can be tried again." })}
          ${num("searches_per_hour", "Prowlarr searches an hour (automatic only)", { min: "1", step: "1" })}
          ${num("max_age_days", "Only if the tracker posted it within (days, 0 = any age)", { min: "0", step: "0.5", title: "autobrr can send an old release - a re-announce, a freeleech. A torrent the tracker posted longer ago than this is skipped. Finding out costs one Prowlarr search per torrent (remembered for two hours); when the posting time cannot be found it is built anyway." })}
        </div>
        <label class="check" style="margin-top:10px"><input type="checkbox" checked=${form.queue_keep_older} onChange=${set("queue_keep_older")} /> When the queue is full, keep the older queued torrents and stop the new arrival instead</label>
        <label class="check" style="margin-top:10px"><input type="checkbox" checked=${form.skip_unposted} onChange=${set("skip_unposted")} /> Don't try groups that are never on Usenet: a torrent whose release group and resolution were never found on Usenet in 5 tries is stopped as it arrives, without a search (see Demand → Found on Usenet; it can still be tried again)</label>
        <label class="check" style="margin-top:10px"><input type="checkbox" checked=${form.start} onChange=${set("start")} /> Start seeding when the recheck is exactly 100.0% (below that a torrent always stays stopped)</label>
        <div class="actions" style="margin-top:12px"><button type="button" class="btn primary" onClick=${save}>Save</button>
          <small class="status">${saved}</small></div>
      </div>
    </div>
    <div class="panel" style="margin-top:20px">
      <div class="ahead"><h2>Activity</h2>
        <button type="button" class="btn" onClick=${clearQueue} title="Stop every torrent that has not started building. They stay in the list and can be tried again; builds already running carry on.">Clear queue</button></div>
      <small class="status">Anything not building within 24 hours of arriving is stopped by itself, and can still be tried again.</small>
      <div class="alist">
        ${state?.items.length ? state.items.map(it => html`<${Arrival} key=${it.infohash} it=${it} act=${act} />`)
          : html`<div class="empty">Nothing has arrived yet.</div>`}
      </div>
    </div>`;
}

const LABEL = { queued: "Queued", waiting: "Waiting for the Usenet post", building: "Building", done: "Done", failed: "Stopped" };
function Arrival({ it, act }) {
  return html`
    <div class="arow">
      <div class="title">${it.name || it.infohash}</div>
      <div class="meta">
        <span class=${"st " + it.status}>${LABEL[it.status] || it.status}</span>
        ${it.first_seen ? html`<span>arrived ${new Date(it.first_seen * 1000).toLocaleString([], { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })}</span>` : null}
        ${it.attempts ? html`<span>${plural(it.attempts, "attempt")}</span>` : null}
        ${it.status === "waiting" && it.next_try ? html`<span>next try ${new Date(it.next_try * 1000).toLocaleTimeString()}</span>` : null}
        ${it.job ? html`<a href="#jobs" onClick=${(e) => { e.preventDefault(); openJob(it.job); }}>log</a>` : null}
      </div>
      ${it.why && html`<div class="status">${it.why}</div>`}
      ${!it.running && ["failed", "done"].includes(it.status) && html`<div class="actions">
        ${it.status === "failed" && html`<button type="button" class="btn" onClick=${() => act("retry", it.infohash)}>Try again</button>`}
        <button type="button" class="btn" onClick=${() => act("forget", it.infohash)}>Remove from the list</button>
      </div>`}
    </div>`;
}

/* one row per autobrr filter: what it does now, and whether nzb2seed replaces its download */
function FilterRow({ f, set }) {
  const grabbers = f.grabbers || [];
  const on = grabbers.filter(a => a.enabled);
  const pick = (e) => {
    // unticking here disables the filter in autobrr: nothing acts on the release afterwards
    if (!e.target.checked && f.enabled && !confirm(
      `"${f.name}": disable it in autobrr too?\n\n` +
      `Unticking it here switches the filter off in autobrr, so nothing will act on its ` +
      `releases at all - neither nzb2seed nor autobrr's own download actions.\n\n` +
      `OK to switch it off, Cancel to leave it ticked.`)) { e.target.checked = true; return; }
    set({ pick: e.target.checked });
  };
  const replace = (e) => {
    if (!e.target.checked && !confirm(
      `"${f.name}": leave its own download actions on?\n\n` +
      `${(on.map(a => a.name).join(", ") || "They")} will download the release over BitTorrent while ` +
      `nzb2seed builds the same release from Usenet - the two race, and the torrent download is what ` +
      `costs you ratio and risks a hit-and-run.\n\nOK to leave them on, Cancel to let nzb2seed replace them.`)) { e.target.checked = true; return; }
    set({ replace: e.target.checked });
  };
  return html`
    <div class=${"row" + (f.pick ? " on" : "")}>
      <input type="checkbox" checked=${f.pick} aria-label=${f.name} onChange=${pick} />
      <div>
        <label class="title" style="cursor:pointer">${f.name}</label>
        <div class="meta">${f.indexers.length ? html`<span>${f.indexers.join(", ")}</span>` : null}
          ${f.actions.length ? html`<span>other actions: ${f.actions.join(", ")}</span>` : null}</div>
        <div class="state">
          <span>filter: <b class=${f.enabled ? "on-b" : "off-b"}>${f.enabled ? "enabled" : "disabled"}</b></span>
          <span>nzb2seed: <b class=${f.ours ? "on-b" : "off-b"}>${f.ours ? "on" : "off"}</b></span>
          ${grabbers.map(a => html`<span>${a.name}: <b class=${a.enabled ? "race" : "off-b"}>${a.enabled ? "downloads it itself" : "off"}</b></span>`)}
          ${!grabbers.length && html`<span class="off-b">no download action of its own</span>`}
          ${f.ours && !f.folder_ok && html`<span class="new">its nzb2seed action points elsewhere - saving fixes it</span>`}
        </div>
        <label class="replace"><input type="checkbox" checked=${f.replace} aria-label=${`nzb2seed download replaces the torrent download for ${f.name}`} onChange=${replace} />
          <span>nzb2seed dl replaces torrent dl<br />
            <small class="status">switches this filter's own download actions off while nzb2seed builds its releases</small>
            ${!f.replace && html`<span class="warn">both will run: qBittorrent downloads the release while nzb2seed builds it from Usenet</span>`}</span></label>
      </div>
    </div>`;
}
