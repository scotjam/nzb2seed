// "Found on Usenet": how often each release group, at each resolution, turned out to be on
// Usenet when nzb2seed tried to build it. Groups never found are stopped on arrival by the
// Automatic tab - and can be kept out at the source by excluding them in autobrr.
import { html, useState } from "../lib.js";
import { api } from "../api.js";
import { toast } from "../store.js";

export function FoundOnUsenet({ rows, never }) {
  const [done, setDone] = useState({});              // group -> what excluding it did
  if (!rows?.length) {
    return html`<div class="dmtable"><h3>Found on Usenet</h3>
      <p class="status">Nothing yet: every build that ends adds to this.</p></div>`;
  }
  const exclude = async (group) => {
    if (!confirm(`Exclude ${group} in autobrr?\n\nIts releases have never been found on Usenet. `
        + `"${group}" is added to "except release groups" in every autobrr filter that sends torrents to nzb2seed, `
        + `so autobrr stops sending its releases at all. Nothing else in those filters is changed; remove it there to undo.`)) return;
    try {
      const r = await api("/api/auto/exclude_group", { group });
      setDone(d => ({ ...d, [group]: r.filters ? `excluded in ${r.filters} filter${r.filters === 1 ? "" : "s"}` : "already excluded" }));
    } catch (err) { toast("Could not exclude it: " + err.message); }
  };
  return html`
    <div class="dmtable">
      <h3>Found on Usenet</h3>
      <p class="status">How often each release group, at each resolution, was on Usenet when a build looked.
        A group and resolution never found in ${never} tries is stopped as it arrives on the Automatic tab (without
        spending a search), and the ones most likely to be found are built first.</p>
      <table><thead><tr><th>group</th><th>resolution</th><th>found</th><th>not found</th><th>found %</th><th></th></tr></thead>
        <tbody>${rows.map(r => html`<tr class=${r.never ? "poor" : r.rate >= 0.75 ? "good" : ""}>
          <td>${r.group.toUpperCase()}</td><td>${r.res}</td><td>${r.found}</td><td>${r.missing}</td>
          <td>${Math.round(r.rate * 100)}%</td>
          <td>${r.never && (done[r.group]
            ? html`<small class="status">${done[r.group]}</small>`
            : html`<button type="button" class="btn" onClick=${() => exclude(r.group)}>Exclude in autobrr</button>`)}</td>
        </tr>`)}</tbody></table>
    </div>`;
}
