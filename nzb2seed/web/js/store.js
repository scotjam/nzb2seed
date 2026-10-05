// State that several tabs share: the settings, the job list (kept live), which job is
// open, the tab on screen, and the toast. Each piece is a tiny store a component can
// subscribe to; only components that use a piece redraw when it changes.
import { useEffect, useState } from "./lib.js";
import { api, onChange } from "./api.js";

export function createStore(value) {
  const subs = new Set();
  return {
    get: () => value,
    set(next) {
      value = typeof next === "function" ? next(value) : next;
      subs.forEach(fn => fn(value));
    },
    subscribe(fn) { subs.add(fn); return () => subs.delete(fn); },
  };
}

export function useStore(store) {
  const [value, setValue] = useState(store.get());
  useEffect(() => {
    setValue(store.get());                // it may have changed before this subscribed
    return store.subscribe(setValue);
  }, [store]);
  return value;
}

/* ---- the tab on screen: the page's address (#jobs, #settings, ...) - and an open job is
   #jobs/<id>, so the browser's (or Android's) Back goes from the job to the jobs list */
export const TABS = ["build", "jobs", "seasons", "assemble", "auto", "demand", "settings"];
const hashParts = () => location.hash.slice(1).split("/");
const tabOf = () => TABS.includes(hashParts()[0]) ? hashParts()[0] : "build";
const jobOf = () => hashParts()[0] === "jobs" && /^\d+$/.test(hashParts()[1] || "") ? Number(hashParts()[1]) : null;
export const tab = createStore(tabOf());
window.addEventListener("hashchange", () => {
  tab.set(tabOf());
  if (tabOf() !== "jobs") return;
  const id = jobOf();
  if (id != null) { openJobId.set(id); jobScreen.set(true); } else jobScreen.set(false);
});
export function go(name) { if (location.hash !== "#" + name) location.hash = "#" + name; }

/* ---- the toast: a short message in the corner, gone after a few seconds */
export const toastMsg = createStore("");
let toastTimer;
export function toast(msg) {
  toastMsg.set(msg);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastMsg.set(""), 6000);
}

/* ---- whether automatic builds are switched on (the dot on the Automatic tab) */
export const autoOn = createStore(false);
export async function refreshAuto() {
  try { autoOn.set(!!(await api("/api/auto/state", {})).settings.enabled); } catch { /* shown next time */ }
}
onChange("auto", refreshAuto);

/* ---- the settings, as the server has them */
export const settings = createStore(null);
export async function loadSettings() {
  const s = await api("/api/settings");
  settings.set(s);
  return s;
}
/* how much each tracker lets you download - as the server keeps it (config.tracker_limit):
   [percent, mb] (0 = that one does not limit), or null when none is set: nothing allowed */
const trackerKey = (n) => { const k = (n || "").trim().toLowerCase(); return k.endsWith("(api)") ? k.slice(0, -5).trim() : k; };
export function trackerLimit(tracker) {
  const want = trackerKey(tracker);
  const row = (settings.get()?.settings?.behaviour?.nearly_limits || []).find(r => want && trackerKey(r.tracker) === want);
  const pct = Number(row?.percent) || 0, mb = Number(row?.mb) || 0;
  return pct > 0 || mb > 0 ? [pct, mb] : null;
}
export function nearlyLimitText(tracker) {
  const lim = trackerLimit(tracker);
  if (!lim) return "nothing (no limit set for this tracker)";
  const [pct, mb] = lim;
  return [pct && `${pct}%`, mb && `${mb} MB`].filter(Boolean).join(" or ") + (pct && mb ? " - whichever is less" : "");
}
/* little enough to download from its tracker: under that tracker's limits (bytes unknown: the %) */
export function shortEnough(x) {
  const lim = x && trackerLimit(x.tracker);
  if (!lim || x.have == null) return false;
  const [pct, mb] = lim;
  return (!pct || (1 - x.have) * 100 < pct) && (!mb || x.short == null || x.short < mb * 1024 ** 2);
}
/* set (or, both blank, clear) how much a tracker lets you download */
export async function setTrackerLimit(tracker, percent, mb) {
  const r = await api("/api/trackers/limit", { tracker, percent, mb });
  settings.set(r.settings);
  return r;
}

/* ---- the jobs: the list stays live, and one of them can be open */
export const jobs = createStore([]);
let fetching = null, again = false;
export async function refreshJobs() {
  if (fetching) { again = true; return fetching; }     // one request at a time, the last one wins
  fetching = (async () => {
    try { jobs.set(await api("/api/jobs")); } catch { /* shown stale until the next change */ }
  })();
  await fetching;
  fetching = null;
  if (again) { again = false; return refreshJobs(); }
}
onChange("jobs", refreshJobs);

export const openJobId = createStore(jobOf());
export const jobScreen = createStore(jobOf() != null);     // on a phone: the job instead of the list
let listBehind = false;                          // the jobs list is the page before this job's
export function openJob(id, navigate = true) {
  openJobId.set(id);
  if (navigate) {
    const target = "#jobs/" + id;
    if (jobOf() != null) location.replace(target);          // one job to another: Back still goes to the list
    else {
      if (location.hash !== "#jobs") location.hash = "#jobs"; // from another tab: the list goes in between
      location.hash = target;
      listBehind = true;
    }
    jobScreen.set(true); window.scrollTo({ top: 0, behavior: "auto" });
  }
  refreshJobs();
}
/* "All jobs": the same as Back when the list is the page behind */
export function backToJobs() {
  if (listBehind && jobOf() != null) { listBehind = false; window.history.back(); }
  else { location.replace("#jobs"); jobScreen.set(false); }
  window.scrollTo({ top: 0 });
}
