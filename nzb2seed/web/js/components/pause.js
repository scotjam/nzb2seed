// Pause / Resume: holds every build and the Torrent list 2 Seed queue. While paused no build
// starts (automatic or by hand), a running build stops before its next big step, SABnzbd's
// queue is paused and no listed name is checked. qBittorrent is never touched: stopping a
// torrent mid-download can cost a private tracker's hit-and-run grace.
import { html, useEffect, useState } from "../lib.js";
import { api, onChange } from "../api.js";
import { toast } from "../store.js";
import { when } from "../util.js";

export function PauseButton() {
  const [st, setSt] = useState(null), [busy, setBusy] = useState(false);
  useEffect(() => {
    const load = () => api("/api/pause", {}).then(setSt).catch(() => {});
    load();
    const off = [onChange("pause", load), onChange("jobs", load)];
    return () => off.forEach(f => f());
  }, []);
  if (!st) return null;
  const flip = async () => {
    setBusy(true);
    try {
      const r = await api("/api/pause", { on: !st.paused });
      setSt(r);
      toast(r.paused ? "Paused: no build starts, running ones stop before their next step, and the list queue waits."
                     : "Resumed.");
    } catch (err) { toast("Could not change it: " + err.message); }
    finally { setBusy(false); }
  };
  return html`<div class=${"pausebar" + (st.paused ? " on" : "")}>
    <button type="button" class=${"btn" + (st.paused ? " primary" : "")} disabled=${busy} onClick=${flip}
      aria-pressed=${st.paused}>${st.paused ? "▶ Resume" : "❚❚ Pause"}</button>
    <span class="status">${st.paused
      ? `Paused${st.since ? " " + when(st.since) : ""}: nzb2seed does nothing by itself - builds, the list queue, `
        + "automatic builds, the Library, mend and the clean-ups are all on hold. Seeding carries on."
      : "Pause holds everything nzb2seed does by itself: builds, the list queue, automatic builds, the Library, "
        + "mend (it starts no stopped torrent) and the clean-ups. Torrents keep seeding."}</span>
  </div>`;
}
