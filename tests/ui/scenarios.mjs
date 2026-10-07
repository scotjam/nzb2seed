// The same things done on both versions of the page. Each returns what was on screen,
// what was sent to the server and which dialogs asked - to be compared.

const since = (calls, n) => calls.slice(n).filter(c => !/^\/api\/(auto\/state|demand|season\/reports|jobs\/\d+)/.test(c.path));

/* the new page shows only whole seasons/films with NZBs found, at first: untick both to see
   every torrent, as the classic page did (nothing to do on the classic page) */
async function showAll(act, doc) {
  for (const box of doc.querySelectorAll(".tfilters input[type=checkbox]")) {
    if (box.checked) { box.click(); await act.settle(); }
  }
}

export const SCENARIOS = {
  /* the new page only: the torrents found, summarised by release group and resolution */
  async digest({ act, doc }) {
    await act.go("build");
    await act.type("Search Prowlarr", "Film.2020");
    await act.submit("Search Prowlarr");
    await act.settle();
    await showAll(act, doc);
    const toggle = doc.querySelector(".digest .dtoggle");
    const arrow = () => toggle.querySelector(".arrow").textContent;
    const shown = () => doc.querySelectorAll(".digest .ditem").length;
    const folding = { arrow: arrow(), items: shown() };
    toggle.click(); await act.settle();
    Object.assign(folding, { foldedArrow: arrow(), foldedItems: shown(), expanded: toggle.getAttribute("aria-expanded") });
    toggle.click(); await act.settle();
    folding.openedAgain = shown();
    const out = { folding, summary: act.text(doc.querySelector(".digest")),
                  hover: [...doc.querySelectorAll(".digest .ditem")].map(i => i.title) };
    doc.querySelector(".digest .ditem input").click();                      // tick the first one
    await act.settle();
    out.bar = act.text(doc.querySelector(".buildbar"));
    [...doc.querySelectorAll("button")].find(b => b.textContent.trim() === "Full list").click();
    await act.settle();
    out.full = doc.querySelectorAll(".side.torrent .row").length;
    return out;
  },

  /* the new page only: the release groups found, to filter the torrents by */
  async groups({ act, doc }) {
    await act.go("build");
    await act.type("Search Prowlarr", "Film.2020");
    await act.submit("Search Prowlarr");
    await act.settle();
    await showAll(act, doc);
    [...doc.querySelectorAll("button")].find(b => b.textContent.trim() === "Full list").click();
    await act.settle();
    const chips = () => [...doc.querySelectorAll(".groupchips .chip")].map(c => c.textContent.replace(/\s+/g, " ").trim());
    const rows = () => [...doc.querySelectorAll(".side.torrent .row .title")].map(t => t.textContent);
    const out = { chips: chips(), before: rows().length };
    [...doc.querySelectorAll(".groupchips .chip")].find(c => c.textContent.includes("GRPZ")).click();
    await act.settle();
    out.only = rows();
    [...doc.querySelectorAll(".groupchips .chip")][0].click();
    await act.settle();
    out.after = rows().length;
    return out;
  },

  /* the new page only: one of Add or Override per build, both asking first */
  async override({ doc, act, calls, confirms }) {
    await act.go("jobs");
    const buttons = (title) => [...[...doc.querySelectorAll(".jrow")].find(r => r.querySelector(".t").textContent.includes(title))
      .querySelectorAll("span.btn")].map(b => b.textContent);
    const out = { nearly: buttons("Film.2020.1080p.BluRay-GRPA"), noSeeders: buttons("Film.2017.1080p.BluRay-GRPF") };
    const n = calls.length;
    [...[...doc.querySelectorAll(".jrow")].find(r => r.textContent.includes("Film.2017.1080p.BluRay-GRPF"))
      .querySelectorAll("span.btn")].find(b => b.textContent.startsWith("Override")).click();
    await act.settle();
    out.asked = confirms.at(-1);
    out.sent = calls.slice(n).filter(c => c.path === "/api/jobs/add_to_client").map(c => c.body);
    return out;
  },

  /* the new page only: at first only whole seasons/films with NZBs found; a language can be
     chosen; a tip says how to see more */
  async tfilters({ act, doc, server }) {
    const base = server.torrents[0];
    server.torrents.push({ ...base, title: "Film.2020.FRENCH.1080p.BluRay-GRPA", guid: "t5" },
                         { ...base, title: "Show.S01E02.1080p.BluRay-GRPA", guid: "t6" });
    await act.go("build");
    await act.type("Search Prowlarr", "Film.2020");
    await act.submit("Search Prowlarr");
    await act.settle();
    const shown = () => [...doc.querySelectorAll(".ditem input")].map(i => i.getAttribute("aria-label")).sort();
    const tip = () => doc.querySelector(".rtip")?.textContent.replace(/\s+/g, " ").trim() || "";
    const out = { first: shown(), tip: tip(), head: doc.querySelector(".side.torrent .head small").textContent };
    const lang = doc.querySelector(".tfilters select");
    lang.value = "french"; lang.dispatchEvent(new doc.defaultView.Event("change", { bubbles: true }));
    await act.settle();
    out.french = shown();
    lang.value = "none"; lang.dispatchEvent(new doc.defaultView.Event("change", { bubbles: true }));
    await act.settle();
    out.none = shown();
    lang.value = "any"; lang.dispatchEvent(new doc.defaultView.Event("change", { bubbles: true }));
    await act.settle();
    for (const box of doc.querySelectorAll(".tfilters input[type=checkbox]")) { box.click(); await act.settle(); }
    out.all = shown();
    out.tipAfter = tip();
    return out;
  },

  /* the new page only: a build that stopped short on a tracker with no limit set asks how much
     it allows - nothing is offered for the torrent client until it is set */
  async limitAsk({ act, doc, calls, server }) {
    server.jobs.unshift({ id: 92, title: "Film.2021.1080p.BluRay-GRPN", kind: "build", status: "failed",
      result: "short", started: 1_700_000_000, ended: 1_700_000_100, progress: "", question: null, can_retry: true,
      extra: { have: 0.99999, infohash: "n".repeat(40), tracker: "TrackerNine", short: 900, seeders: 4 } });
    await act.go("jobs");
    await act.settle();
    const row = () => [...doc.querySelectorAll(".jrow")].find(r => r.textContent.includes("Film.2021.1080p.BluRay-GRPN"));
    const buttons = () => [...row().querySelectorAll("span.btn")].map(b => b.textContent);
    const out = { asks: !!row().querySelector(".limitask"), before: buttons() };
    const pct = row().querySelector('input[aria-label="Percent TrackerNine allows"]');
    pct.value = "1"; pct.dispatchEvent(new doc.defaultView.Event("input", { bubbles: true }));
    await act.settle();
    const n = calls.length;
    row().querySelector(".limitask button").click();
    await act.settle();
    out.sent = calls.slice(n).filter(c => c.path === "/api/trackers/limit").map(c => c.body);
    out.after = buttons();
    out.asksAfter = !!row().querySelector(".limitask");
    return out;
  },

  /* the new page only: Try whole posts says how much is missing, however little */
  async wholeSmall({ act, doc, server }) {
    server.jobs.unshift({ id: 91, title: "auto: Show.S02E06.1080p.BluRay-GRPQ", kind: "auto", status: "failed",
      result: "short", started: 1_700_000_000, ended: 1_700_000_100, progress: "", question: null, can_retry: true,
      extra: { have: 0.9999999, infohash: "h".repeat(40), tracker: "TrackerOne", short: 1000, seeders: 3,
               settled: [{ label: "S02E06", missing: 1000, post: 12_000_000_000 }] } });
    await act.go("jobs");
    await act.settle();
    const row = [...doc.querySelectorAll(".jrow")].find(r => r.textContent.includes("Show.S02E06.1080p.BluRay-GRPQ"));
    return { button: [...row.querySelectorAll("span.btn")].map(b => b.textContent).find(t => t.startsWith("Try whole posts")) };
  },

  /* the new page only: Select by tracker - a job from it as itself, a job it was found on
     by Look on other trackers via it; Add sends each the way it goes */
  async byTracker({ act, doc, calls, server }) {
    const base = { kind: "build", status: "failed", result: "short", started: 1_700_000_000, ended: 1_700_000_100,
                   progress: "", question: null, can_retry: true };
    const other = { title: "Show.S05.1080p.BluRay-GRPV", protocol: "torrent", indexer: "TrackerSix", guid: "six1", size: 10 };
    server.jobs.unshift(
      { ...base, id: 95, title: "Show.S04.1080p.BluRay-GRPV", extra: { have: 0.999, short: 900, infohash: "p".repeat(40), tracker: "TrackerSix", seeders: 3 } },
      { ...base, id: 96, title: "Show.S05.1080p.BluRay-GRPV", extra: { have: 0.80, infohash: "q".repeat(40), tracker: "TrackerOne", seeders: 3, others: [other] } });
    server.settingsLimit?.();
    await act.go("jobs");
    await act.settle();
    const sel = doc.querySelector('select[aria-label="Select by tracker"]');
    const options = [...sel.options].map(o => o.value).filter(Boolean);
    sel.value = "TrackerSix"; sel.dispatchEvent(new doc.defaultView.Event("change", { bubbles: true }));
    await act.settle();
    const ticked = [...doc.querySelectorAll(".jrow")].filter(r => r.querySelector("input").checked)
      .map(r => r.querySelector(".t").textContent.replace(/\s+/g, " ").trim());
    const add = [...doc.querySelectorAll("#jobbar button")].find(b => b.textContent.startsWith("Add to torrent client"));
    const label = add.textContent;
    const n = calls.length;
    add.click(); await act.settle();
    return { options, ticked, label, sent: calls.slice(n).filter(c => /add_via|add_to_client/.test(c.path)).map(c => [c.path, c.body]) };
  },

  /* the new page only: what Look on other trackers found by itself is shown on the job
     straight away - no button to press first */
  async savedOthers({ act, doc, server }) {
    const other = { title: "Film.2021.1080p.BluRay-GRPA", protocol: "torrent", indexer: "TrackerSix", guid: "six2",
                    size: 21_000_000_000, seeders: 13, approved: false, same_size: false, near_size: false, size_diff: 900_000_000 };
    server.jobs.unshift({ id: 97, kind: "auto", title: "auto: Film.2021.1080p.BluRay-GRPA", status: "failed", result: "short",
      started: 1_700_000_000, ended: 1_700_000_100, progress: "", question: null, can_retry: true,
      extra: { have: 0.9999999, short: 900, infohash: "r".repeat(40), tracker: "TrackerOne", seeders: 3, others: [other] } });
    await act.go("jobs");
    await act.settle();
    const row = [...doc.querySelectorAll(".jrow")].find(r => r.textContent.includes("Film.2021.1080p.BluRay-GRPA"));
    return { text: row.textContent.replace(/\s+/g, " "),
             buttons: [...row.querySelectorAll("span.btn, button")].map(b => b.textContent.trim()) };
  },

  /* the new page only: a job built again from another tracker is replaced - grey, saying so,
     with nothing left on it but clearing it */
  async replacedJob({ act, doc, server }) {
    const base = { result: "", started: 1_700_000_000, ended: 1_700_000_100, progress: "", question: null, steps: [] };
    server.jobs.unshift(
      { ...base, id: 99, kind: "build", title: "auto: Film.2022.1080p.BluRay-GRPA", status: "running", can_retry: false, extra: null },
      { ...base, id: 98, kind: "auto", title: "auto: Film.2022.1080p.BluRay-GRPA", status: "cancelled", can_retry: false,
        retried_as: 99, result: "replaced by job 99: built from TrackerTwo",
        extra: { have: 0.9999999, short: 900, infohash: "s".repeat(40), tracker: "TrackerOne", seeders: 3, built_from_other: "TrackerTwo" } });
    await act.go("jobs");
    await act.settle();
    const row = [...doc.querySelectorAll(".jrow")].find(r => r.querySelector(".jretry")?.textContent.includes("replaced by job 99"));
    return { says: row.querySelector(".jretry .status").textContent.replace(/\s+/g, " ").trim(),
             dot: row.querySelector(".dot").className,
             buttons: [...row.querySelectorAll(".jretry span.btn, .jretry button")].map(b => b.textContent.trim()) };
  },

  /* the new page only: the jobs list narrowed by keywords in a job's name, tracker or result */
  async jobSearch({ act, doc }) {
    await act.go("jobs");
    await act.settle();
    const titles = () => [...doc.querySelectorAll(".jrow .t")].map(t => t.textContent.trim());
    const before = titles().length;
    await act.type("Filter jobs by keyword", "show s02");
    const narrowed = titles();
    await act.type("Filter jobs by keyword", "");
    return { before, narrowed, after: titles().length };
  },

  /* the new page only: a resolution folds away under its release group, like the group does */
  async resFold({ act, doc }) {
    await act.go("build");
    await act.type("Search Prowlarr", "Film.2020");
    await act.submit("Search Prowlarr");
    await act.settle();
    for (const box of doc.querySelectorAll(".tfilters input[type=checkbox]")) { if (box.checked) { box.click(); await act.settle(); } }
    const res = () => doc.querySelector(".dres");
    const toggle = () => res().querySelector("h4 .dtoggle");
    const items = () => res().querySelectorAll(".ditem").length;
    const out = { arrow: toggle().querySelector(".arrow").textContent, items: items() };
    toggle().click(); await act.settle();
    Object.assign(out, { folded: toggle().querySelector(".arrow").textContent, foldedItems: items(),
                         says: res().querySelector("h4").textContent.replace(/\s+/g, " ").trim(),
                         groupsStillOpen: doc.querySelectorAll(".digest section .dres").length });
    toggle().click(); await act.settle();
    out.again = items();
    return out;
  },

  /* the new page only: a misspelt search finds torrents but no NZBs - the tip says why and
     offers the name the torrents go by */
  async noNzbs({ act, doc, calls, server }) {
    server.others.noUsenet = true;
    await act.go("build");
    await act.type("Search Prowlarr", "Flim 2020");
    await act.submit("Search Prowlarr");
    await act.settle();
    const tip = () => doc.querySelector(".rtip")?.textContent.replace(/\s+/g, " ").trim() || "";
    const out = { tip: tip() };
    const n = calls.length;
    [...doc.querySelectorAll(".rtip button")].find(b => b.textContent.includes("Film 2020"))?.click();
    await act.settle();
    out.searched = calls.slice(n).filter(c => c.path === "/api/search").map(c => c.body.query);
    out.after = tip();
    return out;
  },

  /* the new page only: a build handed to qBittorrent is orange while it downloads the rest,
     green once qBittorrent has it complete */
  async clientDots({ act, doc, server }) {
    const base = { kind: "build", status: "failed", result: "short", started: 1_700_000_000, ended: 1_700_000_100,
                   progress: "", question: null, can_retry: true };
    server.jobs.unshift(
      { ...base, id: 93, title: "Show.S02.1080p.BluRay-GRPK", extra: { have: 0.99, infohash: "k".repeat(40), tracker: "TrackerOne", added: true } },
      { ...base, id: 94, title: "Show.S03.1080p.BluRay-GRPK", extra: { have: 0.99, infohash: "m".repeat(40), tracker: "TrackerOne", added: true, complete: true } });
    await act.go("jobs");
    await act.settle();
    const row = (t) => [...doc.querySelectorAll(".jrow")].find(r => r.textContent.includes(t));
    return { downloading: row("Show.S02.1080p").querySelector(".dot").className,
             complete: row("Show.S03.1080p").querySelector(".dot").className,
             says: row("Show.S03.1080p").querySelector(".jretry .status").textContent.trim() };
  },

  /* the new page only: a skipped automatic build gets a grey dot, a built one a green one */
  async skippedDot({ act, doc, server }) {
    server.jobs.unshift({ id: 90, title: "auto: Film.2026.2160p.WEB-GRPQ", kind: "auto", status: "done",
      result: "skipped - no priority rule matches it, and only what a rule matches is being built",
      started: 1_700_000_000, ended: 1_700_000_100, progress: "", question: null, can_retry: false, extra: null });
    await act.go("jobs");
    await act.settle();
    const dot = (t) => [...doc.querySelectorAll(".jrow")].find(r => r.textContent.includes(t))?.querySelector(".dot")?.className;
    const done = server.jobs.find(j => j.status === "done" && !/^skipped/.test(j.result || ""));
    return { skipped: dot("Film.2026.2160p.WEB-GRPQ"), done: done && dot(done.title) };
  },

  /* the new page only: nzb2seed's automatic builds measured alongside all your torrents */
  async demandAuto({ act, doc, server }) {
    server.demand.nzb2seed = {
      manual: { torrents: 40, built: 44, stored_gb: 900, uploaded_gb: 1100, overall_ratio: 1.22, median_ratio: 1.0,
        dead: 3, dead_gb: 30, cross_seeds: 0, by: [{ what: "group", rows: [{ where: "GRPB", n: 12, ratio: 0.2, dead: 50 }] }] },
      auto: { torrents: 9, built: 9, stored_gb: 300, uploaded_gb: 450, overall_ratio: 1.5, median_ratio: 1.2,
        dead: 1, dead_gb: 20, cross_seeds: 2, by: [{ what: "group", rows: [{ where: "GRPA", n: 7, ratio: 1.9, dead: 0 }] }] } };
    await act.go("demand");
    await act.settle();
    const table = doc.querySelector(".dmtable table");
    return { page: act.text(act.view()),
             rows: [...table.querySelectorAll("tbody tr")].map(tr => [...tr.children].map(td => td.textContent.trim())) };
  },

  /* the new page only: a torrent's name (or season) opens its tracker page; only the box ticks */
  async pagelinks({ act, doc, server }) {
    server.torrents[0].info_url = "https://tracker.example/details/1?id=5&passkey=SECRET";
    await act.go("build");
    await act.type("Search Prowlarr", "Film.2020.1080p.BluRay-GRPA");
    await act.submit("Search Prowlarr");
    await act.settle();
    const out = {};
    const link = doc.querySelector(".ditem a.pagelink");
    out.digestHref = link?.href;
    const box = () => link.closest(".ditem").querySelector("input");
    const before = box().checked;
    link.click();
    await act.settle();
    out.linkChanged = box().checked !== before;
    box().click();
    await act.settle();
    out.boxChanged = box().checked !== before;
    [...doc.querySelectorAll("button")].find(b => b.textContent.trim() === "Full list").click();
    await act.settle();
    const row = doc.querySelector(".side.torrent .row a.pagelink, .row a.pagelink");
    out.rowHref = row?.href;
    out.plain = [...doc.querySelectorAll(".row .title")].filter(t => !t.querySelector("a")).length;
    return out;
  },

  /* the new page only: the always-added trackers are ticked in Prowlarr's list */
  async trackers({ act, doc, calls }) {
    await act.go("settings");
    const rows = () => [...doc.querySelectorAll("table.limits tbody tr")].map(tr => {
      const [name, pct, mb, always] = tr.children;
      return `${name.textContent.trim()} ${pct.querySelector("input").value || "-"}% ${mb.querySelector("input").value || "-"}MB ${always.querySelector("input").checked ? "[x]" : "[ ]"}`;
    });
    const listed = rows();
    const field = (label) => doc.querySelector(`input[aria-label="${label}"]`);
    field("Always add for TrackerFour").click(); await act.settle();
    field("Always add for TrackerThree (API)").click(); await act.settle();
    const pct = field("Percent TrackerFour allows");
    pct.value = "2"; pct.dispatchEvent(new doc.defaultView.Event("input", { bubbles: true })); await act.settle();
    const one = field("Percent TrackerOne allows");
    one.value = ""; one.dispatchEvent(new doc.defaultView.Event("input", { bubbles: true })); await act.settle();
    const onemb = field("Megabytes TrackerOne allows");
    onemb.value = ""; onemb.dispatchEvent(new doc.defaultView.Event("input", { bubbles: true })); await act.settle();
    const after = rows();
    const n = calls.length;
    await act.click("Save settings", "button");
    const sent = calls.slice(n).filter(c => c.path === "/api/settings");
    const b = sent[0]?.body.settings.behaviour || {};
    return { listed, after, saved: b.nearly_auto_trackers,
             limits: (b.nearly_limits || []).map(r => `${r.tracker} ${r.percent || 0}% ${r.mb || 0}MB`).sort() };
  },

  /* the new page only: a number the browser thinks is "off step" never blocks saving */
  async oddnumbers({ act, calls }) {
    await act.go("settings");
    await act.type("0 = off", "7.3");                      // "Any other disk: below (% free)"
    await act.type("…and less than (MB)", "205");
    const n = calls.length;
    await act.click("Save settings", "button");
    const sent = calls.slice(n).filter(c => c.path === "/api/settings");
    return { saved: sent.length, space: sent[0]?.body.settings.space.min_percent, mb: sent[0]?.body.settings.behaviour.nearly_complete_mb };
  },

  /* the new page only: the NZB pick list filtered and sorted - and the answer still
     names the NZB that was picked, not its place in the filtered list */
  async askfilter({ act, doc, calls }) {
    await act.go("jobs");
    await act.click("Show.S03.1080p.WEB-GRPC", ".jmain");
    const rows = () => [...doc.querySelectorAll(".ask .row .title")].map(t => t.textContent);
    await act.choose("Sort these NZBs", "small");
    const sorted = rows();
    await act.type("Filter these NZBs", "xpost");
    const filtered = rows();
    doc.querySelector(".ask .row input").click();
    await act.settle();
    const n = calls.length;
    await act.click("Download this NZB", "button");
    return { sorted, filtered, sent: calls.slice(n).filter(c => /answer/.test(c.path)) };
  },

  /* the new page only: running builds are cancelled from the bar, and the greyed-out
     buttons say why they are greyed out */
  async cancel({ act, doc, calls }) {
    await act.go("jobs");
    const row = [...doc.querySelectorAll(".jrow")].find(r => r.textContent.includes("Show.S02.1080p.WEB-GRPB"));
    row.querySelector("input[type=checkbox]").click();
    await act.settle();
    const why = [...doc.querySelectorAll("#jobbar button")].find(b => b.textContent.startsWith("Clear from list, keep files")).title;
    const n = calls.length;
    await act.click("Cancel selected (kept on list) (1)", "button");
    return { why, sent: calls.slice(n).map(c => c.path) };
  },

  /* the new page only: a nearly complete build found again on your other trackers */
  async others({ act, doc, calls, server }) {
    // no pre-approved tracker with a seeder: the list, to choose from
    server.others.releases[0].seeders = 0;
    await act.go("jobs");
    const row = () => [...doc.querySelectorAll(".jrow")].find(r => r.textContent.includes("Film.2020.1080p.BluRay-GRPA"));
    row().querySelector("span.btn[title^='Find this release']").click();
    await act.settle();
    const listed = act.text(row().querySelector(".others"));
    const n = calls.length;
    row().querySelector(".other span.btn").click();
    await act.settle();
    return { listed, sent: calls.slice(n).filter(c => c.path === "/api/build") };
  },

  /* the new page only: a pre-approved copy that only lacks a small file (an .nfo the first
     tracker added) is the same video - built from at once too */
  async othersNear({ act, doc, calls, server }) {
    Object.assign(server.others.releases[0], { same_size: false, near_size: true, size_diff: -920 });
    await act.go("jobs");
    const row = () => [...doc.querySelectorAll(".jrow")].find(r => r.textContent.includes("Film.2020.1080p.BluRay-GRPA"));
    const n = calls.length;
    row().querySelector("span.btn[title^='Find this release']").click();
    await act.settle();
    return { sent: calls.slice(n).filter(c => c.path === "/api/build").map(c => c.body.torrent.indexer) };
  },

  /* the new page only: a pre-approved tracker with the same files and a seeder - built
     from at once, nothing offered */
  async othersAuto({ act, doc, calls }) {
    await act.go("jobs");
    const row = () => [...doc.querySelectorAll(".jrow")].find(r => r.textContent.includes("Film.2020.1080p.BluRay-GRPA"));
    const n = calls.length;
    row().querySelector("span.btn[title^='Find this release']").click();
    await act.settle();
    return { sent: calls.slice(n).filter(c => c.path === "/api/build"), shown: act.text(row()),
             listed: !!row().querySelector(".others") };
  },

  /* the new page only: on a phone the list and the job are two screens */
  async phone({ act, doc }) {
    await act.go("jobs");
    const showing = () => doc.querySelector(".jobs").classList.contains("showing");
    const out = { start: showing() };
    await act.click("Film.2020.1080p.BluRay-GRPA", ".jmain");
    out.opened = showing();
    await act.click("← All jobs", "button");
    out.back = showing();
    await act.click("Film.2020.1080p.BluRay-GRPA", ".jmain");
    await act.go("build"); await act.go("jobs");
    out.leftAndCameBack = showing();
    return out;
  },

  /* the new page only: Back (the browser's, or Android's) goes from a job to the jobs list */
  async backButton({ act, doc, w }) {
    await act.go("jobs");
    const showing = () => doc.querySelector(".jobs").classList.contains("showing");
    const back = async () => { w.history.back(); for (let i = 0; i < 20; i++) { await new Promise(r => setTimeout(r, 10)); await act.settle(); } };
    const rows = [...doc.querySelectorAll(".jrow .jmain")];
    const out = {};
    rows[0].click(); await act.settle();
    out.opened = [w.location.hash.startsWith("#jobs/"), showing()];
    await back();
    out.afterBack = [w.location.hash, showing()];
    rows[0].click(); await act.settle();
    [...doc.querySelectorAll(".jrow .jmain")][1].click(); await act.settle();   // a second job, straight after
    await back();
    out.afterTwo = [w.location.hash, showing()];
    return out;
  },

  /* the new page only: the list updates by itself, and never rebuilds the filter you are
     reading (which closed it, in the classic page) */
  async live({ act, doc, server }) {
    await act.go("jobs");
    const filter = doc.querySelector("#jobbar select");
    filter.focus();
    const rows = doc.querySelectorAll(".jrow").length;
    server.jobs.unshift({ ...server.jobs.find(j => j.id === 6), id: 20, title: "Film.2022.1080p.BluRay-GRPA", status: "running", result: "" });
    await new Promise(r => setTimeout(r, 6000));          // the fallback refresh, every 5 s
    await act.settle();
    return { sameFilter: doc.querySelector("#jobbar select") === filter, focused: doc.activeElement === filter,
             rowsBefore: rows, rowsAfter: doc.querySelectorAll(".jrow").length,
             newJob: act.text(doc.querySelector("#joblist")).includes("Film.2022.1080p.BluRay-GRPA") };
  },


  async jobs({ act, calls, doc }) {
    // the bar was redesigned (Select: / Action:) - compared here is what the page does, not
    // its wording: the old name is clicked where it is, the new one where that is
    const press = async (...labels) => {
      const b = [...doc.querySelectorAll("#jobbar button")].find(x => labels.some(l => x.textContent.trim().startsWith(l)) && !x.disabled);
      b.click(); await act.settle();
    };
    await act.go("jobs");
    const out = { list: act.text(act.view().querySelector("#joblist")) };
    await act.choose("Show only jobs of one kind", "nearly");
    out.nearly = act.text(act.view().querySelector("#joblist"));
    const n = calls.length;
    await press("Tick all shown that failed", "Failed");
    await press("Add to torrent client");
    out.added = since(calls, n);
    await act.choose("Show only jobs of one kind", "all");
    const m = calls.length;
    await press("Tick all that failed", "Failed");
    await press("Remove from list", "Clear from list, keep files");
    out.removed = since(calls, m);
    return out;
  },

  async job({ act, calls }) {
    await act.go("jobs");
    await act.click("Film.2020.1080p.BluRay-GRPA", ".jmain");
    await act.settle();
    const job = () => act.text(act.view().querySelector("#job"));
    const out = { nearly: job() };
    await act.click("Show.S03.1080p.WEB-GRPC", ".jmain");
    out.question = job();
    const n = calls.length;
    await act.click("Show.S03E02.1080p.WEB-GRPC", "label");
    await act.click("Download this NZB", "button");
    out.answered = since(calls, n);
    return out;
  },

  async build({ act, calls, doc }) {
    await act.go("build");
    await act.type("Search Prowlarr", "Film.2020.1080p.BluRay-GRPA");
    await act.submit("Search Prowlarr");
    await act.settle();
    await showAll(act, doc);
    await act.settle();
    // the new page opens on a summary by release group; the list the classic page had is one tap away
    const full = [...doc.querySelectorAll("button")].find(b => b.textContent.trim() === "Full list");
    if (full) { full.click(); await act.settle(); }
    const out = { results: act.text(act.view()), bar: act.text(doc.querySelector(".buildbar")) };
    await act.type("Filter NZBs, or search Usenet", "720p");
    out.filtered = act.text(act.view().querySelector(".side.usenet"));
    await act.type("Filter NZBs, or search Usenet", "");
    const n = calls.length;
    await act.click("Build", "button");
    out.built = since(calls, n);
    return out;
  },

  async auto({ act, calls }) {
    await act.go("auto");
    const out = { page: act.text(act.view()) };
    const n = calls.length;
    await act.click("Clear queue", "button");
    await act.type("Inbox folder (as the NAS sees it)", "/new/inbox");
    await act.click("Save", "button");
    out.sent = since(calls, n);
    return out;
  },

  async demand({ act, calls }) {
    await act.go("demand");
    const out = { page: act.text(act.view()) };
    const n = calls.length;
    await act.click("Chase this", "button");
    await act.click("Save rules", "button");
    out.saved = since(calls, n);
    return out;
  },

  async settings({ act, calls }) {
    await act.go("settings");
    const out = { page: act.text(act.view()) };
    const n = calls.length;
    await act.click("Save settings", "button");
    out.saved = since(calls, n);
    await act.click("Test connections", "button");
    await act.click("Check free space now", "button");
    await act.click("Show what would go", "button");
    out.after = act.text(act.view());
    return out;
  },

  async seasons({ act, calls }) {
    await act.go("seasons");
    await act.type("Show name", "Show");
    await act.submit("Show name");
    await act.click("Show", "input[type=radio]");
    await act.click("Find releases", "button");
    await act.click("Show.S01.1080p.WEB-GRPC", "input[type=checkbox]");
    await act.click("Show.S01.720p.HDTV-GRPG", "input[type=checkbox]");
    const out = { page: act.text(act.view()) };
    const n = calls.length;
    await act.click("Grab season from 2 releases", "button");
    out.grabbed = since(calls, n);
    return out;
  },

  async assemble({ act, calls }) {
    await act.go("assemble");
    await act.type("D:\\torrents\\Release.Name.torrent", "/t/x.torrent");
    await act.type("One folder per line, as seen by the machine running nzb2seed", "/dl/a\n/dl/b");
    const n = calls.length;
    await act.click("Assemble", "button");
    return { page: act.text(act.view()), sent: since(calls, n) };
  },
};
