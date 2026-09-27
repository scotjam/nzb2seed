// Small helpers shared by the tabs: formatting, matching and sorting release names.
import { html } from "./lib.js";

export const size = (n) => n >= 1024 ** 3 ? (n / 1024 ** 3).toFixed(2) + " GB" : (n / 1024 ** 2).toFixed(1) + " MB";
export const gb = (n) => (n / 1073741824).toFixed(2) + " GB";
export const age = (d) => {
  const t = Date.parse(d);
  if (!t) return "";
  const days = Math.floor((Date.now() - t) / 864e5);
  return days < 1 ? "today" : days + " days old";
};
/* a release name compared loosely: case, dots, spaces and underscores do not matter */
export const norm = (s) => s.trim().toLowerCase().replace(/[\s._]+/g, ".").replace(/\.?-\.?/g, "-").replace(/^\.|\.$/g, "");
export const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
export const pad2 = (n) => String(n).padStart(2, "0");

/* text with its web addresses made into links (e.g. an NZB's page on its indexer) */
export function linked(text) {
  return String(text).split(/(https?:\/\/[^\s<>"']+[^\s<>"'.,;:!?)\]])/).map((part, i) =>
    i % 2 ? html`<a href=${part} target="_blank" rel="noopener noreferrer">${part}</a>` : part);
}

/* every word of the filter must appear, in any order; dots, dashes and underscores in
   release names count as spaces ("show grp" finds "The.Show.S01...-GRP") */
export function filterMatch(text, filter) {
  const words = (filter || "").toLowerCase().split(/[\s._-]+/).filter(Boolean);
  const hay = " " + text.toLowerCase().replace(/[._-]+/g, " ") + " ";
  return words.every(w => hay.includes(w));
}

/* sorting the result lists: "best" keeps the order they came in */
export function sortKey(a, b, how) {
  const t = r => Date.parse(r.publish_date) || 0;
  if (how === "new") return t(b) - t(a);
  if (how === "old") return t(a) - t(b);
  if (how === "big") return (b.size || 0) - (a.size || 0);
  if (how === "small") return (a.size || 0) - (b.size || 0);
  if (how === "grabs") return (b.grabs || 0) - (a.grabs || 0);
  return 0;
}
export const sortBy = (list, how) => how === "best" ? list : [...list].sort((a, b) => sortKey(a, b, how));

/* the release group: the tail after the last dash, which is how scene names end - or,
   "Show (2022) S02 (1080p ... English - GRPH)", the end of a bracketed description. A
   poster's "-xpost" after it is not the group. */
export function groupOf(title) {
  const t = title.trim();
  const b = t.match(/[\s._]-[\s._]([A-Za-z0-9]{2,20})\)\s*(?:\.[A-Za-z0-9]{2,4})?(?:-x?(?:post|repo))?\s*$/i);
  if (b) return b[1];
  const m = t.replace(/\s+/g, ".").replace(/-x?(?:post|repo)$/i, "").match(/-([A-Za-z0-9_]{2,20})$/);
  return m ? m[1] : "";
}

/* completeness rounds down and a shortfall rounds up, so a gap is never shown away */
export const havePct = (f) => (Math.floor(f * 1000000) / 10000).toFixed(4);
export const missPct = (m) => (Math.ceil(m * 10000) / 10000).toFixed(4);
export const shortSize = (n) => n >= 1024 ** 3 ? `${(Math.ceil(n / 1024 ** 3 * 100) / 100).toFixed(2)} GB`
  : n >= 1024 ** 2 ? `${(Math.ceil(n / 1024 ** 2 * 10) / 10).toFixed(1)} MB` : `${Math.ceil(n / 1024 * 10) / 10} KB`;
/* "0.0001% (13.2 KB)": the size says what the percentage cannot, however small */
export const missText = (x) => `${missPct((1 - x.have) * 100)}%` + (x.short ? ` (${shortSize(x.short)})` : "");
export const trackerName = (t) => (t || "").replace(/\s*\(API\)$/, "");

/* remembered per browser; storage can be switched off, which must never break the page */
export function remember(key, fallback) {
  try { const v = localStorage.getItem("nzb2seed." + key); return v == null ? fallback : v; } catch { return fallback; }
}
export function keep(key, value) {
  try { localStorage.setItem("nzb2seed." + key, value); } catch { /* storage may be off */ }
}

/* a file as base64, for the endpoints that take an uploaded .torrent */
export async function fileB64(f) {
  const buf = new Uint8Array(await f.arrayBuffer());
  let bin = "";
  for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode(...buf.subarray(i, i + 0x8000));
  return btoa(bin);
}
