// The Demand tab: what your seeding returns per GB stored, suggestions drawn from it, and
// the rules that decide what automatic builds do first - or never.
import { html, useEffect, useState } from "../lib.js";
import { api } from "../api.js";
import { loadSettings, settings, toast } from "../store.js";
import { FoundOnUsenet } from "./demand-usenet.js";

const LISTS = ["trackers", "types", "groups", "keywords", "not_keywords"];
const NUMS = { min_gb: "at least (GB)", max_gb: "at most (GB)", max_age_min: "newer than (min)",
               min_seeders: "seeders, at least", max_seeders: "seeders, at most",
               min_leechers: "leechers, at least", max_leechers: "leechers, at most",
               min_grabs: "grabs, at least", max_grabs: "grabs, at most" };
const LABELS = { trackers: "trackers (any)", types: "types (any)", groups: "groups (any)",
                 keywords: "name contains (any)", not_keywords: "name must not contain" };

function saysWhat(rule) {
  const bits = [];
  for (const k of LISTS) if ((rule[k] || []).length) bits.push(LABELS[k] + ": " + rule[k].join(", "));
  for (const k in NUMS) if (rule[k]) bits.push(NUMS[k] + " " + rule[k]);
  return bits.length ? bits.join(" · ") : "matches anything";
}
function blockFor(x) {
  const rule = { name: `${x.value} (${x.ratio.toFixed(2)}x)` };
  if (x.what === "tracker") rule.trackers = [x.value];
  else if (x.what === "type") rule.types = [x.value];
  else if (x.what === "group") rule.groups = [x.value];
  else if (x.what === "tracker+type") { const [tr, ...ty] = x.value.split(" "); rule.trackers = [tr]; rule.types = [ty.join(" ")]; }
  else if (x.what === "category") rule.categories = [x.value];
  return rule;
}
const copy = (x) => JSON.parse(JSON.stringify(x));

export function DemandTab({ active }) {
  const [r, setR] = useState(null);
  const [status, setStatus] = useState("");
  const [rules, setRules] = useState([]), [block, setBlock] = useState([]), [only, setOnly] = useState(false);
  const [dirty, setDirty] = useState(false);

  async function load() {
    setStatus("Reading qBittorrent…");
    try {
      const d = await api("/api/demand");
      if (d.error) { setStatus("Could not read qBittorrent: " + d.error); return; }
      setStatus(""); setR(d);
      setRules(copy(d.rules || [])); setBlock(copy(d.block || [])); setOnly(!!d.only_rules); setDirty(false);
    } catch (err) { setStatus(""); toast("Could not load: " + err.message); }
  }
  useEffect(() => { if (active) load(); }, [active]);

  const change = (setList) => (list) => { setList(list); setDirty(true); };
  async function save() {
    const clean = (list) => list.filter(x => Object.keys(x).some(k => k !== "name" && k !== "enabled"
      && (Array.isArray(x[k]) ? x[k].length : x[k])) || x.name);
    try {
      const s = settings.get() || await loadSettings();
      await api("/api/settings", { settings: { ...s.settings,
        demand: { ...s.settings.demand, rules: clean(rules), block: clean(block), only_rules: only } } });
      await loadSettings();
      toast("Rules saved.");
      load();
    } catch (err) { toast("Could not save: " + err.message); }
  }
  async function forget() {
    try {
      const x = await api("/api/demand/forget", {});
      setStatus(x.removed ? `Forgot ${x.removed} cached lookup(s); they will be asked for again when a rule needs them.` : "Nothing was cached.");
    } catch (err) { toast("Could not clear: " + err.message); }
  }
  const add = (rule, list, setList) => { setList([...list, { enabled: true, ...rule }]); setDirty(true); };

  const card = (big, small, tone) => html`<div class=${"dmcard" + (tone ? " " + tone : "")}><div class="big">${big}</div><div class="small">${small}</div></div>`;
  return html`
    <div class="head"><h2>What your seeding returns</h2><small>${r ? `${r.torrents} torrents older than ${r.age_days} days` : ""}</small></div>
    <p class="status">Measured from your own torrents in qBittorrent - nothing is sent anywhere. The number is <b>upload per GB stored</b>: 1.00 means a torrent has given back as much as it takes up. Only torrents older than the settling time count, because a new one has not had its chance yet.</p>
    ${r && html`
      <div class="dmcards">
        ${card(r.overall_ratio.toFixed(2) + "x", "uploaded per GB stored, overall", r.overall_ratio >= 1 ? "good" : r.overall_ratio < 0.5 ? "poor" : "")}
        ${card(r.uploaded_gb.toLocaleString() + " GB", `uploaded from ${r.stored_gb.toLocaleString()} GB stored`)}
        ${card(r.dead.toLocaleString(), "have never uploaded a byte", r.dead ? "poor" : "")}
        ${card(r.dead_gb.toLocaleString() + " GB", "of disk they hold", r.dead_gb ? "poor" : "")}
      </div>
      ${r.nzb2seed && !r.nzb2seed.torrents && html`
        <div class="head"><h3>nzb2seed's automatic builds</h3></div>
        <p class="status">${`${r.nzb2seed.built} in qBittorrent, none older than ${r.age_days} days yet - they are measured here once they are.`}</p>`}
      ${r.nzb2seed?.torrents > 0 && html`
        <div class="head"><h3>nzb2seed's automatic builds</h3><small>${`${r.nzb2seed.torrents} of them older than ${r.age_days} days`
          + (r.nzb2seed.cross_seeds ? `, with ${r.nzb2seed.cross_seeds} cross-seed${r.nzb2seed.cross_seeds === 1 ? "" : "s"} of their files` : "")}</small></div>
        <div class="dmcards">
          ${card(r.nzb2seed.overall_ratio.toFixed(2) + "x", "uploaded per GB stored, overall", r.nzb2seed.overall_ratio >= 1 ? "good" : r.nzb2seed.overall_ratio < 0.5 ? "poor" : "")}
          ${card(r.nzb2seed.uploaded_gb.toLocaleString() + " GB", `uploaded from ${r.nzb2seed.stored_gb.toLocaleString()} GB stored`)}
          ${card(r.nzb2seed.dead.toLocaleString(), "have never uploaded a byte", r.nzb2seed.dead ? "poor" : "")}
          ${card(r.nzb2seed.dead_gb.toLocaleString() + " GB", "of disk they hold", r.nzb2seed.dead_gb ? "poor" : "")}
        </div>`}
      <div class="dmrec">
        <h3>Worth chasing</h3>
        <p class="status">${(r.chase || []).length
          ? "Kinds of release that pay back well and rarely sit idle. Add one as a priority rule, then narrow it with a size, an age or a seeder count. These are deliberately not about trackers: the ratio worth building is the one on the trackers you are weakest on, so a tracker rule is yours to write by hand."
          : "Nothing stands out yet: no group has enough torrents behind it that also returns well."}</p>
        ${(r.chase || []).map(x => html`<div class="dmrule">
          <span><b>${x.what + ": " + x.value}</b><span class="why">${`${x.n} torrents · returns ${x.ratio.toFixed(2)}x · ${x.dead}% never uploaded`}</span></span>
          <button type="button" class="btn" onClick=${() => add(x.rule, rules, setRules)}>Chase this</button></div>`)}
        ${(r.avoid || []).length > 0 && html`<h3 style="margin-top:14px">Worth blocking</h3>`}
        ${(r.avoid || []).slice(0, 8).map(x => html`<div class="dmrule">
          <span><b>${x.what + ": " + x.value}</b><span class="why">${`${x.n} torrents · returns ${x.ratio.toFixed(2)}x · ${x.dead}% never uploaded · holds ${x.saves_gb.toLocaleString()} GB`}</span></span>
          <button type="button" class="btn" onClick=${() => add(blockFor(x), block, setBlock)}>Block this</button></div>`)}
      </div>
      <div class="dmrec">
        <h3>What to build first</h3>
        <p class="status">Checked from the top down: the first rule a release matches decides how far up the queue it goes. A release no rule matches is still built, at the back. Age, seeders, leechers and grabs are what the tracker says, asked through Prowlarr: one search per release, remembered for two hours.</p>
        <${RuleList} list=${rules} setList=${change(setRules)} ordered />
        <div class="actions">
          <button type="button" class="btn" onClick=${() => add({ name: "" }, rules, setRules)}>Add a rule</button>
          <label class="check"><input type="checkbox" checked=${only} onChange=${(e) => { setOnly(e.target.checked); setDirty(true); }} /> Build only what a rule matches</label>
          <button type="button" class="btn primary" disabled=${!dirty} onClick=${save}>Save rules</button>
        </div>
      </div>
      <div class="dmrec">
        <h3>Never build</h3>
        <p class="status">Checked before anything else: a release matching any of these is not built at all, whatever the rules above say.</p>
        <${RuleList} list=${block} setList=${change(setBlock)} />
        <div class="actions"><button type="button" class="btn" onClick=${() => add({ name: "" }, block, setBlock)}>Add a block</button></div>
      </div>
      <${FoundOnUsenet} rows=${r.usenet} never=${r.never_after} />
      <div>${r.by.map(g => {
        // alongside: the same for nzb2seed's automatic builds, where it has any in that row
        const mine = r.nzb2seed?.torrents > 0 && Object.fromEntries(((r.nzb2seed.by.find(b => b.what === g.what) || {}).rows || []).map(x => [x.where, x]));
        return html`<div class="dmtable"><h3>By ${g.what}</h3>
        <table><thead><tr><th>${g.what}</th><th>torrents</th><th>upload per GB</th><th>never uploaded</th>
          ${mine && html`<th title="nzb2seed's automatic builds only">nzb2seed builds</th><th title="nzb2seed's automatic builds only">their upload per GB</th>`}</tr></thead>
          <tbody>${g.rows.map(x => html`<tr class=${x.ratio < 0.25 ? "poor" : x.ratio >= 1 ? "good" : ""}>
            <td>${x.where}</td><td>${String(x.n)}</td><td>${x.ratio.toFixed(2)}x</td><td>${x.dead}%</td>
            ${mine && html`<td>${mine[x.where] ? String(mine[x.where].n) : "-"}</td><td>${mine[x.where] ? mine[x.where].ratio.toFixed(2) + "x" : "-"}</td>`}</tr>`)}</tbody></table></div>`; })}</div>`}
    <div class="actions"><button type="button" class="btn" onClick=${load}>Refresh</button>
      <button type="button" class="btn" onClick=${forget} title="Forget the posting times, seeder counts and grab counts asked of Prowlarr. They are asked for again only when a rule needs them.">Forget what trackers said</button>
      <small class="status">${status}</small></div>`;
}

/* an ordered list of rules (the priority list) or an unordered one (the blocklist) */
function RuleList({ list, setList, ordered = false }) {
  const update = (i, patch) => setList(list.map((x, k) => k === i ? { ...x, ...patch } : x));
  const move = (i, by) => {
    const j = i + by; if (j < 0 || j >= list.length) return;
    const next = [...list]; next.splice(j, 0, next.splice(i, 1)[0]); setList(next);
  };
  return list.map((rule, i) => html`
    <div class=${"rulecard" + (rule.enabled === false ? " off" : "")}>
      <div class="top">
        ${ordered && html`<span class="ruleno">${String(i + 1)}</span>`}
        <input type="checkbox" title="use this rule" checked=${rule.enabled !== false} onChange=${(e) => update(i, { enabled: e.target.checked })} />
        <input type="text" value=${rule.name || ""} placeholder="what this rule is for" onInput=${(e) => update(i, { name: e.target.value })} />
        ${ordered && html`<button type="button" class="btn" title="higher priority" onClick=${() => move(i, -1)}>↑</button>
          <button type="button" class="btn" title="lower priority" onClick=${() => move(i, 1)}>↓</button>`}
        <button type="button" class="btn" title="remove this rule" onClick=${() => setList(list.filter((_, k) => k !== i))}>×</button>
      </div>
      <div class="conds">
        ${LISTS.map(k => html`<label>${LABELS[k]}
          <${ListInput} value=${rule[k] || []} placeholder=${k === "trackers" ? "tracker.example.org" : ""}
            onChange=${(v) => update(i, { [k]: v })} /></label>`)}
        ${Object.entries(NUMS).map(([k, label]) => html`<label>${label}
          <input type="number" min="0" step="1" value=${rule[k] || ""} onInput=${(e) => update(i, { [k]: Number(e.target.value) || 0 })} /></label>`)}
      </div>
      <div class="says">${saysWhat(rule)}</div>
    </div>`);
}

/* a comma-separated list, kept as typed while you type (so "a, " keeps its comma) */
function ListInput({ value, placeholder, onChange }) {
  const [text, setText] = useState(value.join(", "));
  const joined = value.join(", ");
  useEffect(() => {                         // changed from outside (e.g. "Chase this")
    const mine = text.split(",").map(x => x.trim()).filter(Boolean).join(", ");
    if (mine !== joined) setText(joined);
  }, [joined]);
  return html`<input type="text" value=${text} placeholder=${placeholder}
    onInput=${(e) => { setText(e.target.value); onChange(e.target.value.split(",").map(x => x.trim()).filter(Boolean)); }} />`;
}
