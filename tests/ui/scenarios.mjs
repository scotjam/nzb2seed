// The same things done on both versions of the page. Each returns what was on screen,
// what was sent to the server and which dialogs asked - to be compared.

const since = (calls, n) => calls.slice(n).filter(c => !/^\/api\/(auto\/state|demand|season\/reports|jobs\/\d+)/.test(c.path));

export const SCENARIOS = {
  /* the new page only: the torrents found, summarised by release group and resolution */
  async digest({ act, doc }) {
    await act.go("build");
    await act.type("Search Prowlarr", "Film.2020");
    await act.submit("Search Prowlarr");
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
    const boxes = () => [...doc.querySelectorAll(".ticks label")].map(l => (l.querySelector("input").checked ? "[x] " : "[ ] ") + l.textContent.trim());
    const listed = boxes();
    await act.click("TrackerFour", "input[type=checkbox]");
    await act.click("TrackerThree (API)", "input[type=checkbox]");
    const after = boxes();
    const n = calls.length;
    await act.click("Save settings", "button");
    const sent = calls.slice(n).filter(c => c.path === "/api/settings");
    return { listed, after, saved: sent[0]?.body.settings.behaviour.nearly_auto_trackers };
  },

  /* the new page only: a number the browser thinks is "off step" never blocks saving */
  async oddnumbers({ act, calls }) {
    await act.go("settings");
    await act.type("0 = off", "7.3");                      // "Any other disk: below (% free)"
    await act.type("…and less than (MB) - whichever is less", "205");
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
    const why = [...doc.querySelectorAll("#jobbar button")].find(b => b.textContent.startsWith("Remove from list")).title;
    const n = calls.length;
    await act.click("Cancel (1)", "button");
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


  async jobs({ act, calls }) {
    await act.go("jobs");
    const out = { list: act.text(act.view().querySelector("#joblist")), bar: act.text(act.view().querySelector("#jobbar")) };
    await act.choose("Show only jobs of one kind", "nearly");
    out.nearly = act.text(act.view().querySelector("#joblist"));
    const n = calls.length;
    await act.click("Tick all shown that failed");
    out.ticked = act.text(act.view().querySelector("#jobbar"));
    await act.click("Add to torrent client (1)", "button");
    out.added = since(calls, n);
    await act.choose("Show only jobs of one kind", "all");
    const m = calls.length;
    await act.click("Tick all that failed");
    await act.click("Remove from list", "button");
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
