// A folder chooser over the whole server: the folders you picked recently (one click picks
// one again), the configured data folders as shortcuts, Up, and the folders inside.
import { html } from "../lib.js";
import { keep, remember } from "../util.js";

const RECENT_KEY = "folders.recent";
const RECENT_MAX = 8;

export function recentFolders() {
  try { return JSON.parse(remember(RECENT_KEY, "[]")).filter(x => typeof x === "string"); } catch { return []; }
}
function noteRecent(path) {
  keep(RECENT_KEY, JSON.stringify([path, ...recentFolders().filter(x => x !== path)].slice(0, RECENT_MAX)));
}

/* at: the server's listing ({path, parent, dirs, roots}); browse(path) lists another
   folder; use(path) takes one. */
export function Folders({ at, browse, use }) {
  const take = (p) => { noteRecent(p); use(p); };
  // limited to one drive: only what is on it is offered
  const top = (at.drive || "").replace(/\/+$/, "");
  const onDrive = (p) => !at.drive || p === at.drive || p.startsWith(top + "/");
  const recent = recentFolders().filter(onDrive);
  return html`
    <div class="folders">
      ${recent.length > 0 && html`<div class="recent"><small class="status">Recently chosen</small>
        ${recent.map(p => html`<button type="button" class="chip" title="Use this folder" onClick=${() => take(p)}>${p}</button>`)}</div>`}
      ${(at.roots || []).length > 0 && html`<div class="recent"><small class="status">Data folders</small>
        ${at.roots.map(p => html`<button type="button" class="chip" title="Go to this folder" onClick=${() => browse(p)}>${p}</button>`)}</div>`}
      ${at.drive && html`<div class="recent"><small class="status">Only folders on the drive ${at.drive}</small></div>`}
      <div class="here"><span>${at.path || "/"}</span>
        <span class="actions"><button type="button" class="btn" disabled=${!at.parent} onClick=${() => browse(at.parent)}>Up</button>
          <button type="button" class="btn primary" disabled=${!at.path} onClick=${() => take(at.path)}>Use this folder</button></span></div>
      <div class="list" role="list">
        ${at.dirs.length ? at.dirs.map(d => html`<button type="button" role="listitem" onClick=${() => browse(d.path)}>${d.name + "/"}</button>`)
          : html`<div class="empty" style="padding:8px 12px">No folders inside.</div>`}
      </div>
    </div>`;
}
