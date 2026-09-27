// The page: the header with its tabs, the notice about the default login, every tab
// (kept alive while hidden, so what you did on one is still there when you come back),
// and the toast.
import { html, render, useEffect } from "./lib.js";
import { TABS, autoOn, jobScreen, jobs, loadSettings, refreshAuto, refreshJobs, settings, tab, toastMsg, useStore } from "./store.js";
import { BuildTab } from "./tabs/build.js";
import { JobsTab } from "./tabs/jobs.js";
import { SeasonsTab } from "./tabs/seasons.js";
import { AssembleTab } from "./tabs/assemble.js";
import { AutoTab } from "./tabs/auto.js";
import { DemandTab } from "./tabs/demand.js";
import { SettingsTab } from "./tabs/settings.js";

const VIEWS = { build: BuildTab, jobs: JobsTab, seasons: SeasonsTab, assemble: AssembleTab,
                auto: AutoTab, demand: DemandTab, settings: SettingsTab };
const NAMES = { build: "Build", jobs: "Jobs", seasons: "Seasons", assemble: "Assemble",
                auto: "Automatic", demand: "Demand", settings: "Settings" };

function App() {
  const now = useStore(tab);
  const all = useStore(jobs);
  const on = useStore(autoOn);
  const cfg = useStore(settings);
  const msg = useStore(toastMsg);
  const running = all.filter(j => j.status === "running" || j.status === "waiting").length;

  useEffect(() => { if (now !== "jobs") jobScreen.set(false); }, [now]);

  return html`
    <header>
      <div class="brand"><span class="u">nzb</span>2<span class="t">seed</span></div>
      <nav>${TABS.map(name => html`
        <a href=${"#" + name} aria-current=${name === now ? "page" : null}>${NAMES[name]}${
          name === "jobs" && running > 0 && html`<span class="count">${running}</span>`}${
          name === "auto" && html`<span class=${"autodot" + (on ? " on" : "")}
            title=${on ? "Automatic builds are on" : "Automatic builds are off"}></span>`}</a>`)}
      </nav>
    </header>
    ${cfg?.default_login && html`<div class="notice" role="status">
      You're using the default login (admin / nzb2seed). <a href="#settings">Change the password in Settings</a>.</div>`}
    <main>
      ${TABS.map(name => {
        const View = VIEWS[name];
        return html`<section id=${"view-" + name} class=${"view" + (name === now ? "" : " hidden")}>
          <${View} active=${name === now} /></section>`;
      })}
    </main>
    ${msg && html`<div class="toast" role="status">${msg}</div>`}`;
}

(async () => {
  let s = null;
  try { s = await loadSettings(); } catch { /* the Settings tab says what is wrong */ }
  if (s && !s.exists && !location.hash) location.hash = "#settings";
  render(html`<${App} />`, document.getElementById("app"));
  refreshJobs(); refreshAuto();
})();
