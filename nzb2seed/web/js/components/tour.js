// The tour: a few cards on first launch, each pointing at a tab, saying what it is for.
// Seen once, it stays away; Help -> "Take the tour again" brings it back.
import { html, useEffect } from "../lib.js";
import { createStore, go, useStore } from "../store.js";

const SEEN = "nzb2seed.tour.v1";

export const STEPS = [
  { tab: null, title: "Welcome to nzb2seed",
    text: "nzb2seed builds a torrent's files from Usenet, so the torrent can be seeded at 100% without " +
          "downloading it over BitTorrent. Nothing comes over BitTorrent unless you allow it, and only " +
          "within the limit you set for each tracker. This tour takes a minute." },
  { tab: "settings", title: "1. Settings: connect your tools",
    text: "Start here. nzb2seed needs SABnzbd (with a Usenet provider, to download), qBittorrent (to " +
          "check and seed) and Prowlarr (with your torrent trackers and Usenet indexers, to search). " +
          "Help - What you need before you start - shows how to set each up." },
  { tab: "build", title: "2. Build: one torrent from Usenet",
    text: "Search Prowlarr or upload a .torrent, tick it, and nzb2seed finds the closest Usenet posts: " +
          "the same release group and resolution, a whole pack before seasons, seasons before episodes." },
  { tab: "jobs", title: "3. Jobs: watch it happen",
    text: "Every build is a job, with its steps, its log and a piece map. A build that ends a little " +
          "short stays stopped in qBittorrent, and the job says how short and what you can do." },
  { tab: "seasons", title: "4. Seasons and Assemble: other ways in",
    text: "Seasons grabs a season or a whole series from Usenet without a torrent. Assemble builds a " +
          "torrent you already have from files you have already downloaded." },
  { tab: "auto", title: "5. Automatic: hands-off builds",
    text: "Link autobrr, and the releases its filters catch are built from Usenet by themselves, " +
          "instead of being downloaded over BitTorrent. Not much of a library yet? This is a good way to " +
          "build your tracker stats: new releases seed from the moment they appear, when demand is highest." },
  { tab: "library", title: "6. Library: seed what you already own",
    text: "Got a library from Usenet, Sonarr/Radarr or a tracker that has closed? Let it boost your " +
          "torrent tracker stats. Pick a tracker (or several, in order) and your TV folders: seasons " +
          "the tracker has as the very same release are seeded straight away, the rest are built within a " +
          "daily budget - more seeding size, ratio and bonus points from shows you already own. Nothing in " +
          "your library is swapped or deleted without your approval." },
  { tab: "demand", title: "7. Demand: what pays for its space",
    text: "How much your seeding returns per GB stored, suggestions drawn from it, and rules that decide " +
          "what automatic builds do first - or never." },
  { tab: "help", title: "That's it",
    text: "Help explains every tab and the words nzb2seed uses. Take this tour again from there at any time." },
];

export const tourStep = createStore(null);          // index of the card on screen, or null

export function startTour() { tourStep.set(0); }

export function maybeStartTour() {
  try {
    if (!localStorage.getItem(SEEN)) startTour();
  } catch { /* private mode: no tour rather than a tour every time */ }
}

function finish() {
  try { localStorage.setItem(SEEN, String(Date.now())); } catch { /* ignore */ }
  tourStep.set(null);
}

export function Tour() {
  const i = useStore(tourStep);
  const step = i == null ? null : STEPS[i];
  useEffect(() => { if (step && step.tab) go(step.tab); }, [i]);
  useEffect(() => {
    if (i == null) return;
    const key = (e) => {
      if (e.key === "Escape") finish();
      else if (e.key === "ArrowRight") next();
      else if (e.key === "ArrowLeft" && i > 0) tourStep.set(i - 1);
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [i]);
  if (!step) return null;
  const last = i === STEPS.length - 1;
  function next() { last ? finish() : tourStep.set(i + 1); }
  return html`<div class="tour" role="dialog" aria-modal="false" aria-labelledby="tour-title">
    <div class="tour-card">
      <div class="tour-count">${i + 1} / ${STEPS.length}</div>
      <h2 id="tour-title">${step.title}</h2>
      <p>${step.text}</p>
      <div class="tour-dots">${STEPS.map((_, k) => html`<span class=${k === i ? "on" : ""}></span>`)}</div>
      <div class="tour-buttons">
        <button type="button" class="btn ghost" onClick=${finish}>${last ? "Close" : "Skip the tour"}</button>
        <span>
          ${i > 0 && html`<button type="button" class="btn ghost" onClick=${() => tourStep.set(i - 1)}>Back</button>`}
          <button type="button" class="btn primary" onClick=${next}>${last ? "Done" : "Next"}</button>
        </span>
      </div>
    </div>
  </div>`;
}
