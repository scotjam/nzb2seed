// "Look on other trackers": a build that stopped nearly complete on a tracker you have not
// pre-approved, found again on your other trackers. Building it from a pre-approved one
// lets the missing bit come over BitTorrent without asking; the Usenet downloads already
// made are reused, so nothing is downloaded twice. When a pre-approved tracker has the same
// files with someone seeding them, that is done at once - nothing to choose.
import { html, useState } from "../lib.js";
import { api } from "../api.js";
import { refreshJobs, settings, toast } from "../store.js";
import { plural, shortSize, size } from "../util.js";
import { defaultOptions } from "../components/options.js";

export const trackerKey = (name) => { const n = (name || "").trim().toLowerCase(); return n.endsWith("(api)") ? n.slice(0, -5).trim() : n; };
export const approved = (tracker) => (settings.get()?.settings?.behaviour?.nearly_auto_trackers || [])
  .some(t => trackerKey(t) === trackerKey(tracker));

export function OtherTrackers({ j, Act }) {
  const [found, setFound] = useState(null);       // null: not looked yet
  const [busy, setBusy] = useState(false);
  const [went, setWent] = useState(null);         // the pre-approved release built from at once
  const look = async () => {
    setBusy(true);
    try {
      const r = await api("/api/jobs/other_trackers", { id: j.id });
      // pre-approved, seeded, and the same files (same size, when the size is known)
      const pick = r.releases.find(x => x.approved && (x.seeders || 0) > 0 && (r.size == null || x.same_size || x.near_size));
      if (pick) { setWent(pick); await build(pick); }
      else setFound(r);
    }
    catch (err) { toast("Could not look: " + err.message); }
    finally { setBusy(false); }
  };
  const build = async (r) => {
    const { approved: _a, same_size: _s, near_size: _n, size_diff: _d, ...torrent } = r;
    try {
      await api("/api/build", { torrent, nzbs: [], options: defaultOptions(settings.get()), other_tracker: j.id });
      await refreshJobs();                           // stay on the list: the new job shows at the top
      toast(`Building it from ${r.indexer} - the Usenet downloads already made are reused.`);
    } catch (err) { toast("Could not start it: " + err.message); }
  };
  if (went) {
    return html`<small class="status">Building it from ${went.indexer} - pre-approved, ${plural(went.seeders, "seeder")}</small>`;
  }
  if (!found) {
    return html`<${Act} label=${busy ? "Looking…" : "Look on other trackers"}
      title="Find this release on your other trackers: built from one you have pre-approved, the missing bit comes over BitTorrent without asking. The Usenet downloads already made are reused."
      onPress=${() => !busy && look()} />`;
  }
  return html`
    <div class="others" style="flex-basis:100%">
      <small class="status">${found.releases.length
        ? `${plural(found.releases.length, "other tracker")} ${found.releases.length === 1 ? "has" : "have"} it:`
        : "None of your other trackers has this release."}</small>
      ${found.releases.map(r => html`
        <div class="other">
          <b>${r.indexer}</b>
          <small class="status">${[r.approved ? "pre-approved" : "not pre-approved",
            r.seeders != null ? plural(r.seeders, "seeder") : null, size(r.size),
            found.size == null ? null : r.same_size ? "same size"
              : r.near_size ? `same video - ${shortSize(Math.abs(r.size_diff))} ${r.size_diff < 0 ? "smaller" : "larger"} (a small file such as an .nfo)`
              : "different size - not the same files"].filter(Boolean).join(" · ")}</small>
          <${Act} label="Build from this tracker" title=${`Build ${r.title} from ${r.indexer}, reusing the Usenet downloads already made`}
            onPress=${() => build(r)} />
        </div>`)}
    </div>`;
}
