// The options a build starts with - shared by the build bar and the Assemble tab. They
// start from the saved settings and can be changed for one build.
import { html, useEffect, useState } from "../lib.js";
import { settings, useStore } from "../store.js";

export function defaultOptions(s, assemble = false) {
  const b = s?.settings?.behaviour || {};
  const q = s?.settings?.qbittorrent || {};
  return {
    pp: b.post_processing || "auto", local_verify: !!b.local_verify, retry_bad: b.retry_bad_pieces !== false,
    start: !!q.start_when_complete, no_cleanup: b.cleanup === false,
    ...(assemble ? { fetch_missing: true } : {}),
  };
}

/* [options, setOptions]: reset to the saved settings whenever those change */
export function useOptions(assemble = false) {
  const s = useStore(settings);
  const [opts, setOpts] = useState(() => defaultOptions(s, assemble));
  useEffect(() => setOpts(defaultOptions(s, assemble)), [s]);
  return [opts, setOpts];
}

export function Options({ value, onChange, assemble = false }) {
  const set = (k) => (e) => onChange({ ...value, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value });
  const check = (k, label, title) => html`
    <label class="check" title=${title}><input type="checkbox" checked=${!!value[k]} onChange=${set(k)} />${label}</label>`;
  return html`
    <label>SABnzbd <select aria-label="SABnzbd post-processing" value=${value.pp} onChange=${set("pp")}>
      <option value="auto">Match the torrent</option>
      <option value="repair">+Repair</option>
      <option value="unpack">+Repair/Unpack</option>
    </select></label>
    ${check("local_verify", "Hash pieces here first")}
    ${check("retry_bad", "Try other posts if pieces fail",
      "If pieces fail the check, download other posts of the same release until every piece verifies (needs hashing, which it switches on)")}
    ${check("start", "Start seeding at 100%")}
    ${check("no_cleanup", "Keep extra files")}
    ${assemble && check("fetch_missing", "Download missing files from Usenet",
      "Files the torrent needs that are in none of the folders are downloaded from Usenet, like a build does - only those files")}`;
}
