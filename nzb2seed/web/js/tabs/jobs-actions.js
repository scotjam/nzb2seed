// What the job buttons do: each asks first where something is changed for good, then
// tells the server and refreshes the list. The list stays on screen throughout.
import { api } from "../api.js";
import { jobs, loadSettings, nearlyLimitText, openJobId, jobScreen, refreshJobs, settings, toast } from "../store.js";
import { defaultOptions } from "../components/options.js";
import { gb, missText, shortSize, size, trackerName } from "../util.js";
import { canAdd, canAbandon } from "./jobs-rules.js";

async function each(list, path, done, failed, ok) {
  let n = 0; const bad = [];
  for (const j of list) {
    try { await api(path, { id: j.id }); n++; } catch (err) { bad.push(`${j.title}: ${err.message}`); }
  }
  await refreshJobs();
  toast(`${n} ${done}` + (bad.length ? `; ${failed} ${bad.length}: ${bad[0]}` : "."));
  ok?.();
}

export async function retryJob(j) {
  try {
    await api("/api/jobs/retry", { id: j.id });
    await refreshJobs();                       // stay on the list: the new job shows at the top
    toast("Started again.");
  } catch (err) { toast("Could not start it again: " + err.message); }
}

/* what trying whole posts may cost: one whole post for each part that settled for less */
export function wholePostsText(x) {
  const parts = x.settled || [];
  const miss = parts.reduce((n, p) => n + (p.missing || 0), 0), post = parts.reduce((n, p) => n + (p.post || 0), 0);
  // what is missing can be a 1 KB .nfo: shown in KB, rounded up, never as "0.0 MB"
  return { parts: parts.length, miss: shortSize(miss), post: size(post), names: parts.map(p => p.label).join(", ") };
}

export async function wholePosts(j) {
  const w = wholePostsText(j.extra || {});
  if (!confirm(`Try whole posts for ${j.title}?

${w.parts} part(s) stopped with only small files missing `
      + `(${w.miss} in all: ${w.names}), without downloading another whole post for them. This builds it again, `
      + `trying whole posts for those parts - about ${w.post} or more of downloads for ${w.miss} of files, `
      + `which may still not be there. Everything already downloaded is reused.`)) return;
  try {
    const r = await api("/api/jobs/whole_posts", { id: j.id });
    await refreshJobs();
    toast(`Building it again, trying whole posts - job ${r.id}.`);
  } catch (err) { toast("Could not start it: " + err.message); }
}

export async function abandonJob(j) {
  if (!confirm(`Abandon ${j.title}?\n\nThis deletes its Usenet downloads, and the files it placed unless `
      + `its torrent is in qBittorrent. qBittorrent is not touched: a torrent there stays, with its files - `
      + `remove it there yourself if you no longer want it.`)) return;
  try {
    const r = await api("/api/jobs/abandon", { id: j.id });
    toast(r.result); await refreshJobs();
  } catch (err) { toast("Could not abandon it: " + err.message); }
}

export async function addToClient(j, where, always = false) {
  // "always" also hands over the ones from that tracker already waiting (the server does the same)
  const key = (t) => trackerName(t).trim().toLowerCase();
  const all = jobs.get();
  const waiting = always ? all.filter(o => o.id !== j.id && canAdd(o, all) && key(o.extra.tracker) === key(j.extra.tracker)) : [];
  const ask = always
    ? `Always add builds from ${where} that stop less than ${nearlyLimitText(j.extra.tracker)} short?\n\n`
      + `This one is ${missText(j.extra)} short. It, and every one after it, goes to qBittorrent and downloads the missing part over `
      + `BitTorrent without asking you. Only do this if ${where} lets you download that much without `
      + `a hit-and-run. You can undo it in Settings.`
      + (waiting.length ? `\n\nAlready waiting from ${where}, added now as well:\n`
        + waiting.map(o => `${o.title}: ${missText(o.extra)} missing`).join("\n") : "")
    : `Add this to qBittorrent and download the missing ${missText(j.extra)} over BitTorrent?\n\n`
      + `Only do this if ${where} lets you download that much without a hit-and-run.`;
  if (!confirm(ask)) return;
  try {
    await api("/api/jobs/add_to_client", { id: j.id, always });
    if (always) { try { await loadSettings(); } catch { /* shown next load */ } }
    await refreshJobs();
    toast(waiting.length ? `Handed ${1 + waiting.length} builds to qBittorrent.` : "Handed to qBittorrent.");
  } catch (err) { toast("Could not add it: " + err.message); }
}

/* outside the limit, or no seeders reported: added only because you say so, knowing why not */
export async function overrideAdd(j, where) {
  const x = j.extra;
  const why = x.seeders === 0
    ? `Prowlarr reported no seeders for it on ${where}, so the missing part may never arrive.`
    : `That is more than ${where} allows: ${nearlyLimitText(x.tracker)}.`;
  if (!confirm(`Override: add this to qBittorrent anyway?\n\nIt is missing ${missText(x)}. ${why}\n\n`
      + `All of the missing part would be downloaded from ${where} over BitTorrent - downloading that much `
      + `may count towards a hit-and-run there. Only do this if you are sure ${where} allows it.`)) return;
  try {
    await api("/api/jobs/add_to_client", { id: j.id, override: true });
    await refreshJobs();
    toast("Handed to qBittorrent (override).");
  } catch (err) { toast("Could not add it: " + err.message); }
}

export function retryMany(list, done) {
  return each(list, "/api/jobs/retry", "started again", "could not start", done);
}

export function addMany(list, done) {
  const where = [...new Set(list.map(j => trackerName(j.extra.tracker || "tracker unknown")))];
  const lines = list.slice(0, 15).map(j => `${j.title} (${trackerName(j.extra.tracker || "tracker unknown")}): ${missText(j.extra)} missing`)
    .concat(list.length > 15 ? [`...and ${list.length - 15} more`] : []).join("\n");
  if (!confirm(`Add ${list.length} build(s) to qBittorrent and download the missing part of each over BitTorrent?\n\n`
      + `${lines}\n\n`
      + `Each is within what its tracker lets you download, as you set it.`)) return;
  return each(list, "/api/jobs/add_to_client", "handed to qBittorrent", "could not add", done);
}

export function abandonMany(list, done) {
  if (!confirm(`Abandon ${list.length} build(s)?\n\nThis deletes the files each one placed and its `
      + `Usenet downloads, and removes it from qBittorrent if it is there and not running. `
      + `Only nzb2seed's own files are touched.`)) return;
  return each(list, "/api/jobs/abandon", "abandoned", "could not abandon", done);
}

export async function removeMany(list, done) {
  const all = jobs.get();
  const open = list.filter(j => canAdd(j, all) || canAbandon(j, all)).length;
  if (!confirm(`Clear ${list.length} job(s) from the list and keep their files?\n\nNo file is deleted: not the torrents' files, `
      + `and not their downloads in your Usenet downloads folder - temporary files may be left there for you to delete `
      + `(Clear from list, delete files removes those). Nothing in qBittorrent is touched. `
      + `Automatic torrents stay on the Automatic tab, where they can still be tried again.`
      + (open ? `\n\n${open} of them still have Add to torrent client or Abandon on them - those buttons go with them, `
        + `and anything such a build left behind stays where it is.` : ""))) return;
  try {
    const r = await api("/api/jobs/remove", { ids: list.map(j => j.id) });
    if (list.some(j => j.id === openJobId.get())) { openJobId.set(null); jobScreen.set(false); }
    done?.();
    await refreshJobs();
    toast(`Removed ${r.removed} job(s)` + (r.kept ? `; ${r.kept} still running were left` : "") + ".");
  } catch (err) { toast("Could not remove them: " + err.message); }
}

/* the list entries and their Usenet downloads - but no download a torrent depends on */
export async function removeAndDelete(list, done) {
  if (!confirm(`Remove ${list.length} job(s) from the list and delete their Usenet downloads?\n\n`
      + `The downloads in SABnzbd's folders that these builds made are deleted - part downloads and finished ones - `
      + `unless a torrent in qBittorrent uses them, a running build is using them, or they are shared with another download. `
      + `The files a build placed for its torrent are not touched, and nothing in qBittorrent is changed. This cannot be undone.`)) return;
  try {
    const r = await api("/api/jobs/remove", { ids: list.map(j => j.id), delete_downloads: true });
    if (list.some(j => j.id === openJobId.get())) { openJobId.set(null); jobScreen.set(false); }
    done?.();
    await refreshJobs();
    toast(`Removed ${r.removed} job(s) and deleted ${r.deleted || 0} download(s), freeing ${gb(r.bytes || 0)}`
      + (r.kept?.length ? `; kept ${r.kept.length}: ${r.kept[0]}` : "") + ".");
  } catch (err) { toast("Could not remove them: " + err.message); }
}

/* Add to torrent client for the ticked: jobs as themselves (each within its tracker's limit)
   and jobs ticked via another tracker - whose torrent is built, as Build from this tracker
   on the job does: the downloads already made are reused */
export async function addFromTrackers(own, via, done) {
  if (!via.length) return addMany(own, done);           // only jobs as themselves: as ever
  const lines = [...own.map(j => `${j.title} (${trackerName(j.extra.tracker || "tracker unknown")}): ${missText(j.extra)} missing`),
                 ...via.map(([j, r]) => `${j.title}: ${trackerName(r.indexer)}'s torrent, from what is already here`)];
  if (!confirm(`Add to torrent client?\n\n${lines.slice(0, 15).join("\n")}${lines.length > 15 ? `\n...and ${lines.length - 15} more` : ""}\n\n`
      + (own.length ? "Each is within what its tracker lets you download, as you set it. " : "")
      + (via.length ? "A job ticked via another tracker goes to qBittorrent as that tracker's torrent, laid out from the files already here; "
        + "qBittorrent downloads the rest from that tracker - however much it is. More than that tracker allows is tagged "
        + "after it (it may bring seeding obligations there). Nothing is built from Usenet." : ""))) return;
  let n = 0; const bad = [];
  for (const j of own) {
    try { await api("/api/jobs/add_to_client", { id: j.id }); n++; } catch (err) { bad.push(`${j.title}: ${err.message}`); }
  }
  for (const [j, r] of via) {
    try { await api("/api/jobs/add_via", { id: j.id, guid: r.guid }); n++; } catch (err) { bad.push(`${j.title}: ${err.message}`); }
  }
  done?.();
  await refreshJobs();
  toast(`${n} handed over` + (bad.length ? `; could not ${bad.length}: ${bad[0]}` : "."));
}

/* Clear from list, delete files: what each ticked build placed (where nothing in qBittorrent
   uses it) and its Usenet downloads go, then the jobs leave the list */
export async function clearAndDelete(list, done) {
  const all = jobs.get();
  if (!confirm(`Clear ${list.length} job(s) from the list and delete their files?\n\n`
      + `Their Usenet downloads are deleted, and the files the builds placed for their torrents - unless a torrent in `
      + `qBittorrent uses them, a running build is using them, or they are shared with another download. `
      + `A torrent in qBittorrent and its files are never deleted. This cannot be undone.`)) return;
  for (const j of list.filter(j => canAbandon(j, all))) {
    try { await api("/api/jobs/abandon", { id: j.id }); } catch { /* still cleared below */ }
  }
  try {
    const r = await api("/api/jobs/remove", { ids: list.map(j => j.id), delete_downloads: true });
    if (list.some(j => j.id === openJobId.get())) { openJobId.set(null); jobScreen.set(false); }
    done?.();
    await refreshJobs();
    toast(`Cleared ${r.removed} job(s) and deleted ${r.deleted || 0} download(s), freeing ${gb(r.bytes || 0)}`
      + (r.kept?.length ? `; kept ${r.kept.length}: ${r.kept[0]}` : "") + ".");
  } catch (err) { toast("Could not clear them: " + err.message); }
}

/* stop the ticked running builds - nothing is deleted, and each can be tried again */
export async function cancelMany(list, done) {
  if (!confirm(`Cancel ${list.length} running build(s)?

Each stops after its current step. Nothing is deleted: `
      + `a cancelled build can be tried again (reusing what it downloaded), or removed with its downloads.`)) return;
  let n = 0; const bad = [];
  for (const j of list) {
    try { await api(`/api/jobs/${j.id}/cancel`, {}); n++; } catch (err) { bad.push(`${j.title}: ${err.message}`); }
  }
  done?.();
  await refreshJobs();
  toast(`${n} cancelling after their current step` + (bad.length ? `; could not cancel ${bad.length}: ${bad[0]}` : "."));
}

export async function clearAutoQueue() {
  let waiting = 0;
  try { waiting = (await api("/api/auto/state", {})).items.filter(it => ["queued", "waiting"].includes(it.status)).length; }
  catch { waiting = -1; }                               // could not tell: ask anyway
  if (!waiting) return toast("No automatic jobs are queued - nothing to clear.");
  if (!confirm("Clear all queued automatic jobs?\n\nEvery automatic build that has not started building yet is stopped. "
      + "They stay in the list and can be tried again. Builds already running carry on.")) return;
  try {
    const r = await api("/api/auto/clear", {});
    await refreshJobs();
    toast(r.stopped ? `Stopped ${r.stopped} queued automatic job(s).` : "No automatic jobs were queued.");
  } catch (err) { toast("Could not clear them: " + err.message); }
}
