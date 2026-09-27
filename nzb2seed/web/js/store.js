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

/* ---- the tab on screen: the page's address (#jobs, #settings, ...) */
export const TABS = ["build", "jobs", "seasons", "assemble", "auto", "demand", "settings"];
const tabOf = () => TABS.includes(location.hash.slice(1)) ? location.hash.slice(1) : "build";
export const tab = createStore(tabOf());
window.addEventListener("hashchange", () => tab.set(tabOf()));
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
/* how short a failed build may be and still be offered to the torrent client */
/* the megabytes limit that goes with it: whichever of the two is reached first */
export function nearlyMb() {
  const v = Number(settings.get()?.settings?.behaviour?.nearly_complete_mb);
  return Number.isFinite(v) && v > 0 ? v : 200;
}
export const nearlyLimitText = () => `${nearlyLimit()}% or ${nearlyMb()} MB`;
/* little enough to fetch over BitTorrent: under both limits (bytes unknown: the percentage) */
export function shortEnough(x) {
  return x.have != null && (1 - x.have) * 100 < nearlyLimit() && (x.short == null || x.short < nearlyMb() * 1024 ** 2);
}
export function nearlyLimit() {
  const v = Number(settings.get()?.settings?.behaviour?.nearly_complete_percent);
  return Number.isFinite(v) && v > 0 ? v : 5;
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

export const openJobId = createStore(null);
export const jobScreen = createStore(false);     // on a phone: the job instead of the list
export function openJob(id, navigate = true) {
  openJobId.set(id);
  if (navigate) { go("jobs"); jobScreen.set(true); window.scrollTo({ top: 0, behavior: "auto" }); }
  refreshJobs();
}
