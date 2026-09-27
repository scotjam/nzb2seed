// The sort menu over a list of releases (Build tab, the NZB pick list).
import { html } from "../lib.js";

export function SortSelect({ label, value, onChange, grabs = false }) {
  return html`<select class="sortsel" aria-label=${label} value=${value} onChange=${onChange}>
    <option value="best">Best match</option><option value="new">Newest first</option><option value="old">Oldest first</option>
    <option value="big">Largest first</option><option value="small">Smallest first</option>
    ${grabs && html`<option value="grabs">Most grabbed</option>`}</select>`;
}
