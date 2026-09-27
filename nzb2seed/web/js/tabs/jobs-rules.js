// What can be done with a job, and what kind of job it is - pure functions of the job
// list, shared by the list, the bar and the job's own buttons.
import { shortEnough } from "../store.js";

export const BADLY_ENDED = ["failed", "cancelled", "interrupted"];
export const isLive = (j) => j.status === "running" || j.status === "waiting";

/* the job a retry chain ended in: a failed build tried again until one worked is done with */
export function finalOf(j, jobs) {
  const seen = new Set([j.id]);
  let f = j;
  while (f.retried_as) {
    const next = jobs.find(x => x.id === f.retried_as);
    if (!next || seen.has(next.id)) break;
    seen.add(next.id); f = next;
  }
  return f;
}
export function builtLater(j, jobs) { const f = finalOf(j, jobs); return f !== j && f.status === "done"; }

/* what kind of job it is, for the filter - the first that fits */
export const JOB_KINDS = [
  ["running", "Running", j => isLive(j)],
  ["inclient", "Added to qBittorrent", j => !!(j.extra || {}).added],
  ["retried", "Tried again", j => BADLY_ENDED.includes(j.status) && !!j.retried_as],
  ["nearly", "Nearly complete", j => BADLY_ENDED.includes(j.status) && (j.extra || {}).have != null && !(j.extra || {}).abandoned],
  ["nousenet", "No Usenet post", j => BADLY_ENDED.includes(j.status) && /no Usenet post could supply|: none found|no post of this group/i.test(j.result || "")],
  ["refused", "NZB not handed over", j => BADLY_ENDED.includes(j.status) && /refused the NZB|did not get the NZB|could not get the NZB/i.test(j.result || "")],
  ["stopped", "Stopped / cancelled", j => j.status === "cancelled"],
  ["interrupted", "Interrupted", j => j.status === "interrupted"],
  ["failed", "Failed, other", j => j.status === "failed"],
  ["done", "Done", j => j.status === "done"],
];
export function kindOf(j) { const k = JOB_KINDS.find(([, , fits]) => fits(j)); return k ? k[0] : "done"; }

export const canRetry = (j) => BADLY_ENDED.includes(j.status) && j.can_retry;
export function canAdd(j, jobs) {
  const x = j.extra || {};
  return BADLY_ENDED.includes(j.status) && x.have != null && !x.added && !x.abandoned
    && shortEnough(x) && x.seeders !== 0 && !builtLater(j, jobs);
}
export function canAbandon(j, jobs) {
  const x = j.extra || {};
  // never once handed to qBittorrent, or once a retry built it: its files are in use then
  return BADLY_ENDED.includes(j.status) && !!x.infohash && !x.abandoned && !x.added && !builtLater(j, jobs);
}
export const canRemove = (j) => !isLive(j);
