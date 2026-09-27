// The release groups among the torrents found, as buttons to filter by: the groups with
// the most seasons first ("GRPH · S01–S05 · 5 torrents"), so a whole series from one group
// is a tap away.
import { html } from "../lib.js";
import { groupOf, norm } from "../util.js";

/* the season numbers a name covers: S02, S02E05, and ranges like S01-S03 */
export function seasonsOf(title) {
  const n = norm(title);
  const out = new Set();
  for (const m of n.matchAll(/(?<![a-z0-9])s(\d{1,3})(?:e\d{1,4})?(?:-s?(\d{1,3}))?(?![0-9])/g)) {
    const a = Number(m[1]), b = m[2] ? Number(m[2]) : a;
    for (let s = a; s <= Math.min(b, a + 60); s++) out.add(s);
  }
  return out;
}

const span = (nums) => {
  const list = [...nums].sort((a, b) => a - b);
  if (!list.length) return "";
  const p = (s) => "S" + String(s).padStart(2, "0");
  // a run without gaps reads as a range; otherwise the seasons one by one
  return list.length > 2 && list.at(-1) - list[0] === list.length - 1 ? `${p(list[0])}–${p(list.at(-1))}` : list.map(p).join(", ");
};

export function groupsOf(torrents) {
  const by = new Map();
  for (const t of torrents) {
    const g = groupOf(t.title);
    if (!g) continue;
    const key = g.toLowerCase();
    const e = by.get(key) || { key, name: g, seasons: new Set(), count: 0 };
    seasonsOf(t.title).forEach(s => e.seasons.add(s));
    e.count++;
    by.set(key, e);
  }
  return [...by.values()].sort((a, b) => b.seasons.size - a.seasons.size || b.count - a.count || a.name.localeCompare(b.name));
}

export function GroupChips({ torrents, value, onChange }) {
  const groups = groupsOf(torrents);
  if (groups.length < 2) return null;              // nothing to choose between
  return html`
    <div class="groupchips" role="group" aria-label="Filter torrents by release group">
      <button type="button" class=${"chip" + (value ? "" : " on")} aria-pressed=${String(!value)} onClick=${() => onChange("")}>All groups</button>
      ${groups.map(g => html`
        <button type="button" class=${"chip" + (value === g.key ? " on" : "")} aria-pressed=${String(value === g.key)}
          title=${`Only ${g.name}'s torrents`} onClick=${() => onChange(value === g.key ? "" : g.key)}>
          <b>${g.name}</b>${g.seasons.size ? ` · ${span(g.seasons)}` : ""} · ${g.count} torrent${g.count === 1 ? "" : "s"}</button>`)}
    </div>`;
}
