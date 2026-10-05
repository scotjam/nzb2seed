// The torrents found, digested: each release group, the resolutions it has, and under each
// resolution the seasons (or, for a film, the torrents) with their sizes. A season is
// ticked by tapping it, just as in the full list.
import { html, useState } from "../lib.js";
import { groupOf, norm, pageLink, size } from "../util.js";

const RES = /(?<![a-z0-9])(2160|1080|720|576|480)p(?![a-z0-9])/;
const RES_ORDER = ["2160p", "1080p", "720p", "576p", "480p", "other"];
const pad = (n) => String(n).padStart(2, "0");

/* what one torrent is of: "S02", "S02E05", "S01–S03" - or null for a film */
export function partOf(title) {
  const n = norm(title);
  const range = n.match(/(?<![a-z0-9])s(\d{1,3})-s?(\d{1,3})(?![0-9e])/);
  if (range) return { label: `S${pad(range[1])}–S${pad(range[2])}`, order: Number(range[1]) * 1000 };
  const m = n.match(/(?<![a-z0-9])s(\d{1,3})(?:e(\d{1,4}))?(?![0-9])/);
  if (!m) return null;
  return m[2] ? { label: `S${pad(m[1])}E${pad(m[2])}`, order: Number(m[1]) * 1000 + Number(m[2]), season: Number(m[1]) }
              : { label: `S${pad(m[1])}`, order: Number(m[1]) * 1000, season: Number(m[1]) };
}

/* "grpa|1080p|s2" (or "grpa|1080p|film"): a group, resolution and season to match Usenet by */
function nzbKey(group, title) {
  const res = (norm(title).match(RES) || [null, null])[1];
  const part = partOf(title);
  const season = part?.season ?? (part ? Math.floor(part.order / 1000) : null);
  return `${group.toLowerCase()}|${res ? res + "p" : "other"}|${season != null ? "s" + season : "film"}`;
}

/* a whole season (or several), or a film - not a single episode */
export const isWhole = (title) => !/E\d/.test(partOf(title)?.label || "");

/* (torrent) => did Usenet turn up the same release group, resolution and season (or film)?
   Such a torrent is one nzb2seed can build with some confidence */
export function withNzbs(usenet = []) {
  const posted = new Set(usenet.map(u => groupOf(u.title)).map((g, i) => g && nzbKey(g, usenet[i].title)).filter(Boolean));
  return (t) => { const g = groupOf(t.title); return !!g && posted.has(nzbKey(g, t.title)); };
}

export function digest(torrents, usenet = []) {
  const hasNzbs = withNzbs(usenet);
  const groups = new Map();
  for (const t of torrents) {
    const name = groupOf(t.title) || "no group in the name";
    const key = name.toLowerCase();
    const g = groups.get(key) || { key, name, res: new Map(), seasons: new Set(), tv: false, count: 0, nzb: 0 };
    const res = (norm(t.title).match(RES) || [null, null])[1];
    const r = res ? res + "p" : "other";
    if (!g.res.has(r)) g.res.set(r, new Map());
    const part = partOf(t.title);
    if (part) { g.tv = true; if (part.season != null) g.seasons.add(part.season); }
    const itemKey = part ? part.label : t.guid;
    const items = g.res.get(r);
    let item = items.get(itemKey);
    if (!item) {
      item = { label: part?.label || null, order: part ? part.order : 0, torrents: [], nzb: hasNzbs(t) };
      if (item.nzb) g.nzb++;
    }
    item.torrents.push(t);
    items.set(itemKey, item);
    g.count++;
    groups.set(key, g);
  }
  return [...groups.values()].map(g => ({
    ...g,
    resolutions: RES_ORDER.filter(r => g.res.has(r)).map(r => ({
      res: r, items: [...g.res.get(r).values()].sort((a, b) => a.order - b.order || (b.torrents[0].size - a.torrents[0].size)),
    })),
  }));
}

const best = (g) => RES_ORDER.indexOf(g.resolutions[0]?.res ?? "other");
export const DIGEST_SORTS = {
  nzb: ["Most with NZBs", (a, b) => b.nzb - a.nzb || b.seasons.size - a.seasons.size || b.resolutions.length - a.resolutions.length],
  seasons: ["Most seasons", (a, b) => b.seasons.size - a.seasons.size || b.resolutions.length - a.resolutions.length],
  resolutions: ["Most resolutions", (a, b) => b.resolutions.length - a.resolutions.length || b.seasons.size - a.seasons.size],
  best: ["Highest resolution", (a, b) => best(a) - best(b) || b.seasons.size - a.seasons.size],
  name: ["Group name", () => 0],
};

/* first the groups with the most found on Usenet; then series groups by their seasons, film
   groups by their resolutions (unless you choose) */
export function sortDigest(groups, how) {
  const byName = (a, b) => a.name.localeCompare(b.name);
  if (how && how !== "auto") return [...groups].sort((a, b) => DIGEST_SORTS[how][1](a, b) || b.count - a.count || byName(a, b));
  const tv = groups.some(g => g.tv);
  return [...groups].sort((a, b) => b.nzb - a.nzb
    || (tv ? DIGEST_SORTS.seasons[1](a, b) : DIGEST_SORTS.resolutions[1](a, b)) || b.count - a.count || byName(a, b));
}

export function Digest({ torrents, usenet, how, setHow, chosen, pick, edit, full, tip }) {
  const groups = sortDigest(digest(torrents, usenet), how);
  const [shut, setShut] = useState(() => new Set());        // groups (and group|resolution) folded away
  const fold = (key) => setShut(s => { const n = new Set(s); n.has(key) ? n.delete(key) : n.add(key); return n; });
  const tv = groups.some(g => g.tv);
  return html`
    <div class="digest">
      <div class="dhead"><small class="status">${groups.length} release group${groups.length === 1 ? "" : "s"}</small>
        <select class="sortsel" aria-label="Sort release groups" value=${how} onChange=${(e) => setHow(e.target.value)}>
          <option value="auto">NZBs found first, then ${tv ? "most seasons" : "most resolutions"} (default)</option>
          ${Object.entries(DIGEST_SORTS).map(([k, [label]]) => html`<option value=${k}>${label}</option>`)}
        </select></div>
      ${groups.map(g => html`
        <section class="dgroup" key=${g.key}>
          <h3><button type="button" class="dtoggle" aria-expanded=${String(!shut.has(g.key))}
              title=${shut.has(g.key) ? `Show ${g.name}'s torrents` : `Hide ${g.name}'s torrents`} onClick=${() => fold(g.key)}>
            <span class="arrow" aria-hidden="true">${shut.has(g.key) ? "▸" : "▾"}</span>${g.name}</button> <small>${[g.tv ? `${g.seasons.size} season${g.seasons.size === 1 ? "" : "s"}` : null,
            `${g.resolutions.length} resolution${g.resolutions.length === 1 ? "" : "s"}`,
            `${g.count} torrent${g.count === 1 ? "" : "s"}`,
            g.nzb ? `NZBs found for ${g.nzb}` : "no matching NZBs found"].filter(Boolean).join(" · ")}</small></h3>
          ${!shut.has(g.key) && g.resolutions.map(r => {
            const rk = `${g.key}|${r.res}`, label = r.res === "other" ? "resolution not in the name" : r.res;
            const n = r.items.reduce((k, item) => k + item.torrents.length, 0);
            return html`
            <div class="dres">
              <h4><button type="button" class="dtoggle" aria-expanded=${String(!shut.has(rk))}
                  title=${shut.has(rk) ? `Show ${g.name}'s ${label} torrents` : `Hide ${g.name}'s ${label} torrents`} onClick=${() => fold(rk)}>
                <span class="arrow" aria-hidden="true">${shut.has(rk) ? "▸" : "▾"}</span>${label}</button>
                ${shut.has(rk) && html` <small>${n} torrent${n === 1 ? "" : "s"}</small>`}</h4>
              ${!shut.has(rk) && r.items.map(item => item.torrents.map(t => html`
                <label class=${"ditem" + (chosen.has(t.guid) ? " on" : "")} key=${t.guid} title=${t.title}
                  onClick=${(ev) => { if (ev.target.tagName !== "INPUT" && chosen.has(t.guid)) { ev.preventDefault(); edit(t); } }}>
                  <input type="checkbox" checked=${chosen.has(t.guid)} aria-label=${t.title}
                    onChange=${(e) => pick(t, e.target.checked)} />
                  ${pageLink(t, item.label || t.title, "dwhat")}
                  <span class="dsize">${size(t.size)}</span>
                  ${item.nzb && html`<span class="dnzb" title="An NZB of the same release group, resolution and season was found on Usenet">NZBs found</span>`}
                  <small class="status">${[t.indexer, t.seeders != null ? `${t.seeders} seeders` : null].filter(Boolean).join(" · ")}</small>
                </label>`))}
            </div>`; })}
        </section>`)}
      ${tip}
      <button type="button" class="btn dfull" onClick=${full}>Full list</button>
    </div>`;
}
