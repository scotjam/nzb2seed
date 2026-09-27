// A stand-in for the nzb2seed server: the same made-up data for both versions of the
// page, and a record of what each one sent. Names are invented - never real releases.

const H = (c) => c.repeat(40);
const job = (id, title, status, more = {}) => ({
  id, title, kind: "build", status, result: "", started: 1_700_000_000 + id, ended: status === "running" ? null : 1_700_000_100 + id,
  progress: "", question: null, can_retry: false, retried_as: null, extra: null, steps: ["Finding the NZBs, closest match first"],
  infohash: "", ...more,
});

export const JOBS = [
  job(10, "Show.S03.1080p.WEB-GRPC", "waiting", { question: { prompt: "No post of GRPC holds S03E02 - pick one", choices: [
    { title: "Show.S03E02.1080p.WEB-GRPC", indexer: "IndexerA", size: 1181116006, size_text: "1.10 GB", grabs: 12, note: "" },
    { title: "Show.S03E02.1080p.WEB-GRPC-xpost", indexer: "IndexerB", size: 1170378588, size_text: "1.09 GB", grabs: 3, note: "smaller than the episode" }] } }),
  job(9, "Show.S02.1080p.WEB-GRPB", "running", { progress: "downloading 42%", steps: ["Downloading from Usenet"] }),
  job(8, "Film.2020.1080p.BluRay-GRPA", "failed", { result: "the NZB download(s) do not contain every file of the torrent: 99.9999% of it is here",
    can_retry: true, extra: { have: 0.9999993, infohash: H("a"), torrent_path: "/t/a.torrent", save_path: "/data", in_client: false, tracker: "TrackerOne", short: 13471, seeders: 7 } }),
  job(7, "auto: Other.2019.1080p.WEB-GRPD", "failed", { kind: "auto", result: "gave up: no Usenet post could supply Other.2019.1080p.WEB-GRPD", can_retry: true }),
  job(6, "Film.2021.1080p.BluRay-GRPA", "done", { result: "100.0% (seeding)" }),
  job(5, "auto: Film.2018.1080p.BluRay-GRPE", "cancelled", { kind: "auto", result: "cancelled" }),
  job(4, "Film.2021.1080p.BluRay-GRPA", "failed", { result: "the NZB download(s) do not contain every file", retried_as: 6,
    extra: { have: 0.99, infohash: H("b"), torrent_path: "/t/b.torrent", save_path: "/data", in_client: false, tracker: "TrackerTwo (API)", short: 90000000 } }),
  job(3, "Film.2017.1080p.BluRay-GRPF", "failed", { result: "the NZB download(s) do not contain every file", can_retry: true,
    extra: { have: 0.98, infohash: H("c"), torrent_path: "/t/c.torrent", save_path: "/data", in_client: false, tracker: "TrackerOne", short: 200000000, seeders: 0 } }),
  job(2, "Show.S01.720p.HDTV-GRPG", "interrupted", { result: "the GUI stopped while this build was running", can_retry: true }),
  job(1, "Film.2016.1080p.WEB-GRPH", "failed", { result: "SABnzbd did not get the NZB from IndexerA within 180s",
    extra: { have: 0.999, infohash: H("d"), torrent_path: "/t/d.torrent", save_path: "/data", in_client: true, tracker: "TrackerOne", added: true } }),
];

const LINES = [
  { k: "step", t: "Using .torrent file x.torrent" }, { k: "info", t: "4 file(s), 20.00 GB" },
  { k: "step", t: "Finding the NZBs, closest match first" }, { k: "warn", t: "IndexerA: refused - its page: https://indexer.example/details/abc" },
  { k: "step", t: "Using .torrent file x.torrent" }, { k: "step", t: "Finding the NZBs, closest match first" },
  { k: "line", t: "    move  a.mkv  ->  Film/a.mkv" },
];

const TORRENTS = [
  { title: "Film.2020.1080p.BluRay-GRPA", protocol: "torrent", indexer: "TrackerOne", indexer_id: 1, size: 21474836480, guid: "t1",
    download_url: "", info_url: "", publish_date: "2024-01-01T00:00:00Z", grabs: 5, seeders: 11, files: 2 },
  { title: "Film.2020.2160p.BluRay-GRPZ", protocol: "torrent", indexer: "TrackerTwo", indexer_id: 2, size: 42949672960, guid: "t2",
    download_url: "", info_url: "", publish_date: "2023-06-01T00:00:00Z", grabs: 9, seeders: 3, files: 1 },
];
const USENET = [
  { title: "Film.2020.1080p.BluRay-GRPA", protocol: "usenet", indexer: "IndexerA", indexer_id: 3, size: 21600000000, guid: "u1",
    download_url: "", info_url: "", publish_date: "2024-01-02T00:00:00Z", grabs: 40, seeders: null, files: 60 },
  { title: "Film 2020 1080p BluRay-GRPA", protocol: "usenet", indexer: "IndexerB", indexer_id: 4, size: 21700000000, guid: "u2",
    download_url: "", info_url: "", publish_date: "2024-01-03T00:00:00Z", grabs: 7, seeders: null, files: 61 },
  { title: "Film.2020.720p.WEB-GRPQ", protocol: "usenet", indexer: "IndexerA", indexer_id: 3, size: 4000000000, guid: "u3",
    download_url: "", info_url: "", publish_date: "2024-02-01T00:00:00Z", grabs: 2, seeders: null, files: 20 },
];

const AUTO = {
  settings: { enabled: true, folder: "/inbox", autobrr_folder: "/config/inbox", wait_hours: 0.23, retry_minutes: 2,
    retry_first_minutes: 2, parallel: 2, queue_max: 10, queue_keep_older: false, searches_per_hour: 40, start: true,
    autobrr_url: "http://autobrr.example:7474" },
  has_key: true,
  items: [
    { infohash: H("e"), name: "Other.2019.1080p.WEB-GRPD", status: "failed", first_seen: 1_700_000_000, attempts: 3, why: "no Usenet post could supply it", job: 7, running: false },
    { infohash: H("f"), name: "Show.S04.1080p.WEB-GRPC", status: "waiting", first_seen: 1_700_000_500, attempts: 1, next_try: 1_700_000_900, job: 11, running: true },
    { infohash: H("g"), name: "Film.2015.1080p.BluRay-GRPA", status: "queued", first_seen: 1_700_000_600, attempts: 0, running: true },
    { infohash: H("h"), name: "Film.2014.1080p.BluRay-GRPA", status: "done", first_seen: 1_700_000_100, attempts: 1, why: "100.0% (seeding)", job: 6, running: false },
  ],
};

const DEMAND = {
  torrents: 120, age_days: 14, overall_ratio: 0.82, uploaded_gb: 1500.5, stored_gb: 1830, dead: 17, dead_gb: 240,
  rules: [{ enabled: true, name: "Big films", types: ["film"], min_gb: 5 }], block: [{ enabled: true, name: "Tiny", max_gb: 1 }],
  only_rules: false,
  chase: [{ what: "group", value: "GRPA", n: 30, ratio: 1.8, dead: 5, rule: { name: "GRPA", groups: ["GRPA"] } }],
  avoid: [{ what: "tracker", value: "tracker.example", n: 20, ratio: 0.1, dead: 60, saves_gb: 120 }],
  by: [{ what: "group", rows: [{ where: "GRPA", n: 30, ratio: 1.8, dead: 5 }, { where: "GRPB", n: 12, ratio: 0.2, dead: 50 }] }],
};

export function makeServer(settingsPayload) {
  const calls = [];
  const detail = (id, since) => {
    const j = JOBS.find(x => x.id === id) || JOBS[0];
    return { ...j, lines: LINES.slice(since), next: LINES.length, pieces: "####x....##" };
  };
  const answer = (path, body) => {
    if (path === "/api/settings") return settingsPayload;
    if (path === "/api/jobs") return JOBS;
    const m = path.match(/^\/api\/jobs\/(\d+)(\?since=(\d+))?$/);
    if (m) return detail(Number(m[1]), Number(m[3] || 0));
    if (path === "/api/auto/state") return AUTO;
    if (path.startsWith("/api/auto/")) return { ...AUTO, stopped: 2 };
    if (path === "/api/demand") return DEMAND;
    if (path === "/api/search") return body.protocol === "usenet" ? { usenet: USENET.slice(2) } : { torrents: TORRENTS, usenet: USENET.slice(0, 2) };
    if (path === "/api/pair") return { usenet: USENET.slice(0, 2), groups: [["u1", "u2"]], how: "exact name" };
    if (path === "/api/previous") return { previous: [{ title: "Film.2020.1080p.BluRay-GRPA", state: "failed", indexer: "IndexerA", size: 21600000000, submitted: 1_700_000_000, note: "SABnzbd could not complete it" }] };
    if (path === "/api/build" || path === "/api/assemble" || path === "/api/season/grab" || path === "/api/series/grab") return { id: 12 };
    if (path === "/api/season/reports") return [{ name: "Show S01 GRPC 1080p", episodes: 9, total: 10, created: 1_700_000_000 }];
    if (path === "/api/season/shows") return [{ id: 5, name: "Show", premiered: "2015-03-01", network: "Net", imdb: "tt0000001" }];
    if (path === "/api/season/seasons") return [{ number: 1, episodes: 10, source: "TVmaze", premiere: "2015-03-01" }, { number: 2, episodes: 8, source: "TMDB", premiere: "2016-03-01" }];
    if (path === "/api/season/options") return { show: { name: "Show" }, source: "TVmaze", links: { TVmaze: "https://tvmaze.example/5" },
      episodes: [1, 2, 3].map(n => ({ number: n })),
      options: [{ key: "k1", name: "Show.S01.1080p.WEB-GRPC", group: "GRPC", res: "1080p", episodes_found: [1, 2, 3], season_nzbs: 1, complete: true, size: 3e9 },
                { key: "k2", name: "Show.S01.720p.HDTV-GRPG", group: "GRPG", res: "720p", episodes_found: [1, 3], season_nzbs: 0, complete: false, size: 1e9 }] };
    if (path === "/api/series/options") return { seasons: [{ season: 1, episodes: 10, missing: 4 }, { season: 2, episodes: 8, missing: 8 }],
      options: [{ key: "s1", group: "GRPC", res: "1080p", total: 10, season_nzbs: 1, seasons: { 1: 4, 2: 6 }, episodes: { 1: [1, 2], 2: [1, 2, 3] } }] };
    if (path === "/api/folders") return { path: "", parent: null, roots: ["/data"], dirs: [{ name: "/data", path: "/data" }] };
    if (path === "/api/space") return { disks: [{ role: "downloads", path: "/dl", free: 5e11, percent: 21, min_gb: 100, min_percent: 0 }], limits_set: true, low: false, holding: false };
    if (path === "/api/retention/sweep") return { removed: [{ name: "Film.2010.1080p-GRPA", files: 2, bytes: 5e9 }], kept: [{ name: "Film.2011.1080p-GRPA", skipped: "not automatic" }], days: 30, builds: 3, oldest_days: 90, bytes: 5e9 };
    if (path === "/api/test") return { prowlarr: { ok: true, text: "Prowlarr 1.0" }, sabnzbd: { ok: false, text: "refused" }, qbittorrent: { ok: true, text: "qBittorrent 5" } };
    if (path === "/api/metadata/clear_cache") return { removed: 3 };
    if (path === "/api/demand/forget") return { removed: 2 };
    if (path === "/api/jobs/other_trackers") return { name: "Film.2020.1080p.BluRay-GRPA", size: 21474836480, releases: [
      { ...TORRENTS[0], indexer: "TrackerThree", guid: "t9", seeders: 4, approved: true, same_size: true },
      { ...TORRENTS[0], indexer: "TrackerFour", guid: "t8", seeders: 30, size: 21474836481, approved: false, same_size: false }] };
    if (path.startsWith("/api/jobs/")) return { result: "done", id: 13, removed: 1, kept: 0 };
    return {};
  };
  const fetch = async (url, opt = {}) => {
    const u = new URL(url, "http://nzb2seed.test/");
    const path = u.pathname + (u.search || "");
    const body = opt.body ? JSON.parse(opt.body) : undefined;
    if (opt.method === "POST") calls.push({ path, body });
    const data = answer(u.pathname + (u.pathname.match(/^\/api\/jobs\/\d+$/) ? u.search : ""), body || {});
    return { ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(data)) };
  };
  return { fetch, calls, jobs: JOBS };
}
