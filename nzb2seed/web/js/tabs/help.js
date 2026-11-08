// The Help tab: what each tab does and when it is the one to use, the ideas behind
// nzb2seed in a few lines each, and the way to take the tour again.
import { html } from "../lib.js";
import { startTour } from "../components/tour.js";

const TABS = [
  { id: "build", name: "Build", does:
      "Turns one torrent into files from Usenet. Search Prowlarr (or upload a .torrent), tick it, check the " +
      "NZBs nzb2seed suggests on the right - or pick your own - and build.",
    use: "A release you want to seed and that was also posted to Usenet. Several torrents can be ticked " +
         "and built one after another." },
  { id: "jobs", name: "Jobs", does:
      "Every build, past and running: its steps, its log and a piece map showing which parts of the " +
      "torrent are there. Failed or short builds say why and offer what can be done next.",
    use: "Following a build, finding out why one stopped short, retrying, or handing a nearly complete " +
         "build to qBittorrent to download the rest." },
  { id: "seasons", name: "Seasons", does:
      "Grabs a season, or every season of a show, from Usenet without a torrent: you choose the release " +
      "groups and resolutions in order, each episode is checked against an episode list, and a report " +
      "is written next to the season.",
    use: "Filling a show in your library from Usenet, skipping the episodes you already have." },
  { id: "assemble", name: "Assemble", does:
      "Builds a .torrent you already have from folders you already downloaded - and, if you allow it, " +
      "fetches only what those folders lack from Usenet.",
    use: "A release you grabbed from Usenet yourself and now want to seed from the same files." },
  { id: "auto", name: "Automatic", does:
      "Links autobrr: the releases its ticked filters catch are dropped into an inbox and built from " +
      "Usenet by themselves, waiting for the post to appear if it is not there yet.",
    use: "Building your tracker stats if you don't have much of a library yet: new releases are seeded " +
         "from the moment they appear, when they are most in demand, without downloading them over " +
         "BitTorrent - hands-off." },
  { id: "library", name: "Library", does:
      "Seeds your TV library from one tracker - or several, in an order you set - a season at a time. " +
      "A season a tracker has as the very " +
      "same release is linked into a torrent with nothing downloaded; the others are built from Usenet " +
      "within a daily budget and offered as a swap you approve. While it builds or moves a season, " +
      "qBittorrent's on-finished program (cross-seed) is held back so the disk is not swamped.",
    use: "A library from other sources - Usenet, Sonarr/Radarr, a tracker that has closed - put to work " +
         "boosting your torrent tracker stats (seeding size, ratio, bonus points, seeding time), without " +
         "downloading those shows again over BitTorrent." },
  { id: "demand", name: "Demand", does:
      "Shows what your seeding returns for each GB it takes, suggests what to build from that, and holds " +
      "the rules that decide what automatic builds take first - or never.",
    use: "Deciding what is worth disk space, and steering automatic builds." },
  { id: "settings", name: "Settings", does:
      "Every setting as a form: connections to Prowlarr, SABnzbd and qBittorrent, folders and path " +
      "mappings, how near a build must be before it may finish over BitTorrent (per tracker), free space, " +
      "and what is removed afterwards.",
    use: "First setup, and changing a tracker's limit or where files go. The tracker limits (percent and " +
         "MB, not always add) can be exported and imported as a CSV file, to share them with others." },
];

const IDEAS = [
  ["What you need before you start",
   html`Three programs you run yourself, and a way onto Usenet. <b>A Usenet provider</b> (the paid news
   server account that stores the posts) and <b>Usenet indexers</b> (the search sites whose NZB files say which
   posts make up a release) - at least one of each; SABnzbd's guide explains both:
   <a href="https://sabnzbd.org/wiki/introduction/quick-setup" target="_blank" rel="noopener noreferrer">quick setup</a>
   (your provider's server) and
   <a href="https://sabnzbd.org/wiki/introduction/nzb-sources" target="_blank" rel="noopener noreferrer">NZB sources</a>
   (indexers). <b>SABnzbd</b> downloads from Usenet: add your provider's server in it, then put its URL and API
   key (Config, General, Security) in Settings.
   <b>qBittorrent</b> checks each build piece by piece and seeds it: its URL, user name and password go in
   Settings. <b>Prowlarr</b> searches your trackers and indexers (below). If SABnzbd or qBittorrent run in
   Docker, fill in the path translations in Settings too. The README has a docker-compose file for each
   (<a href="https://docs.linuxserver.io/images/docker-sabnzbd/" target="_blank" rel="noopener noreferrer">SABnzbd</a>,
   <a href="https://docs.linuxserver.io/images/docker-qbittorrent/" target="_blank" rel="noopener noreferrer">qBittorrent</a>).
   Recommended too: <b>cross-seed</b> (see below).`],
  ["Setting up Prowlarr (needed before anything else)",
   html`nzb2seed finds everything through
   <a href="https://prowlarr.com/" target="_blank" rel="noopener noreferrer">Prowlarr</a>: your torrent trackers
   for the .torrent, and your Usenet indexers for the NZBs that hold the same release - so Prowlarr needs both.
   In short: run Prowlarr (its Docker image is <code>lscr.io/linuxserver/prowlarr</code>, port 9696); set a
   login; under Indexers, add each torrent tracker (with your passkey or API key from the tracker, and
   Redirect left off) and each Usenet indexer; then copy the API key from Settings, General. In nzb2seed's
   Settings, under Prowlarr, enter its URL and that key and press Test. The README has a ready docker-compose
   file, and the
   <a href="https://wiki.servarr.com/prowlarr/quick-start-guide" target="_blank" rel="noopener noreferrer">quick start guide</a>
   covers every step
   (<a href="https://wiki.servarr.com/prowlarr/installation" target="_blank" rel="noopener noreferrer">installing</a>,
   <a href="https://wiki.servarr.com/prowlarr/indexers" target="_blank" rel="noopener noreferrer">indexers</a>).`],
  ["cross-seed (recommended)",
   html`Not needed to build anything, but recommended:
   <a href="https://www.cross-seed.org" target="_blank" rel="noopener noreferrer">cross-seed</a> finds the same
   release on your other trackers and adds it to qBittorrent seeding from the files you already have (hard
   links, nothing downloaded). Every torrent nzb2seed builds - from Build, Automatic or Library - then seeds on
   each tracker that has it, so one Usenet download grows your stats on all of them. Point qBittorrent's "Run
   external program on torrent finished" at cross-seed's webhook so each build is searched for as soon as it
   is complete. The README has a ready docker-compose file and the settings that matter; the
   <a href="https://www.cross-seed.org/docs/basics/getting-started" target="_blank" rel="noopener noreferrer">cross-seed documentation</a>
   covers the rest.`],
  ["Nothing over BitTorrent unless you allow it",
   "A build that is not 100% from Usenet stays stopped in qBittorrent. It downloads the rest only when you " +
   "choose to, or if, within nzb2seed's settings, its tracker is manually set to download nearly complete " +
   "builds for that tracker (Settings: builds that stop a few percent short) - and only after " +
   "qBittorrent's own check agrees how much is missing. With only a few seeders, that rest only comes if " +
   "qBittorrent is connectable: forward its listening port (Tools, Options, Connection) on your router, or " +
   "use a VPN with port forwarding - two peers that cannot take incoming connections never reach each other."],
  ["Closest match first",
   "nzb2seed looks for posts by the same release group, at the same resolution, at least as large as the " +
   "torrent's files: a whole pack first, then seasons, then episodes. Only sources with the same release " +
   "group and resolution are considered potential matches."],
  ["When no post is complete",
   "Some posts are missing articles, or fail in SABnzbd. nzb2seed then builds from everything it " +
   "downloaded: each file from a complete download when one has it, the incomplete posts last, closest " +
   "first - including what can be unpacked from a broken RAR set. The gaps they leave are counted before " +
   "anything is decided, and come over BitTorrent only within your limit."],
  ["Posts SABnzbd gives up on",
   "SABnzbd's Abort jobs that cannot be completed (on by default) stops downloading a post the moment it can " +
   "tell it cannot be repaired - yet most of a broken post usually still checks out piece by piece. When a " +
   "build used such a post, its job offers to download the rest: just this time (the abort goes off only while " +
   "it downloads), or always. Settings warns while the abort is on. With it off, SABnzbd still tries each " +
   "article a few times on each news server (max_art_tries, 3 by default), then marks the job Failed - Repair " +
   "failed, not enough repair blocks in its History, so it never retries forever; it only means the whole post " +
   "is downloaded, even when it cannot be repaired."],
  ["Every piece is checked",
   "The torrent's pieces are checked against the files laid out. Pieces that fail make nzb2seed try " +
   "other posts of the same files, and qBittorrent's recheck has the last word."],
  ["Torrents short of 100%, and kept originals",
   "A tracker's copy of a release can differ in a few pieces from the Usenet post, so a cross-seed of it (or a " +
   "Library season) can sit stopped short of 100%. Every half hour nzb2seed tries the missing files from " +
   "an original it kept first, then from another torrent of the same name, as a hard link (never written into), and " +
   "starts the torrent once it rechecks at 100.0%. With nothing left to try, a torrent on a tracker ticked always add " +
   "is started to download the rest - only within that tracker's limit, once, after its short files get a copy of " +
   "their own so the torrents sharing them are not changed. Originals: before a repair or a BitTorrent download changes " +
   "a built file, nzb2seed keeps the Usenet version in .nzb2seed-originals on the same disk - often exactly " +
   "what your other trackers have - and the change goes into a copy of its own. They are kept for " +
   "keep_originals_days (3 by default; 0 keeps none), then let go; a torrent finished from one keeps its bytes."],
  ["Can my library boost my tracker stats?",
   "Yes - that is what the Library tab is for, wherever the library came from: Usenet, Sonarr/Radarr, " +
   "a tracker that has since closed, etc. Pick the tracker whose stats you want to grow (or several, " +
   "in order) and your TV " +
   "folders. Seasons the tracker has as the very same release start seeding straight away from your files, " +
   "with nothing downloaded. The others are built from Usenet as that tracker's release, within a daily " +
   "budget, and offered as a swap for your copy. Each season seeded adds to your seeding size, and to your " +
   "ratio, bonus points and seeding time as it is downloaded by others. With cross-seed (recommended, " +
   "above) the same files seed on your other trackers too."],
  ["One tracker or several (Library)",
   "One tracker is the recommended default: every season pack comes from it. Choose Several trackers to " +
   "tick more and put them in order, first choice at the top. The list also holds Your files - the very " +
   "release you have, on any ticked tracker - at the top by default, so a season any of them has as your " +
   "release is linked with nothing downloaded; otherwise the first tracker with a pack builds it from " +
   "Usenet. Move Your files down to prefer a tracker's own pack over it. One Prowlarr search covers them " +
   "all, and a new order makes waiting seasons search again. Ticking another tracker puts seasons left as " +
   "No season pack back in the queue to try it."],
  ["Not much of a library? Start with Automatic",
   "The Library tab grows your stats from shows you already have. With few of those, the Automatic tab is " +
   "the better start: link autobrr, tick the filters for the trackers you want to grow, and each new " +
   "release they catch is built from Usenet and seeded from the moment it appears - when most people are " +
   "downloading it, so it uploads the most. Nothing is downloaded over BitTorrent unless that tracker's " +
   "limit allows the last few percent."],
  ["Setting up autobrr for the Automatic tab",
   html`<a href="https://autobrr.com/introduction" target="_blank" rel="noopener noreferrer">autobrr</a> watches
   your trackers' IRC announce channels and catches new releases as they are posted. In short: run its Docker
   container (<code>ghcr.io/autobrr/autobrr</code>, port 7474, on the same machine as nzb2seed); create your
   login; add your trackers under Settings, Indexers (your passkey and the tracker's IRC channel); make a
   filter for the releases you want (trackers, resolution, size, groups) - no action needed, nzb2seed adds its
   own; and create an API key under Settings, API keys. Then, in Automatic, enter autobrr's address and that
   key, tick the filters, and use "Find autobrr's folder" for the inbox. The README has a ready
   docker-compose file; the
   <a href="https://autobrr.com/installation/docker" target="_blank" rel="noopener noreferrer">official docs</a>
   cover every step in full
   (<a href="https://autobrr.com/configuration/indexers" target="_blank" rel="noopener noreferrer">indexers</a>,
   <a href="https://autobrr.com/configuration/irc" target="_blank" rel="noopener noreferrer">IRC</a>,
   <a href="https://autobrr.com/filters" target="_blank" rel="noopener noreferrer">filters</a>).`],
  ["Moving seasons into your library with cross-eyed",
   html`Without it, the Library tab swaps a season by hard-linking the torrent's files over yours. With
   <a href="https://codeberg.org/cross-eyed/cross-eyed" target="_blank" rel="noopener noreferrer">cross-eyed</a>
   - a separate, self-hosted app that tracks which torrents share which files - approving a season moves the
   torrent itself, and its cross-seed copies, into the show's folder, and qBittorrent rechecks it there.
   To use it: set up cross-seed first (above), then run cross-eyed's Docker container next to qBittorrent (the
   README's "Moving seasons into your library with cross-eyed" walks through it, with a ready docker-compose
   file), open it on port 8765, then put its address in
   Library, under "cross-eyed address", and save. cross-eyed starts in dry-run each time: while it is, the Library tab says so
   and nothing is moved (auto move waits too) until you switch it to live in its header. "Auto delete the existing library files" then removes your old
   season folder once the moved torrent seeds at 100.0% - it may cause data loss, so watch a few moves first.`],
  ["Only its own files",
   "nzb2seed only removes what it created itself, keeps a record of it, and never deletes from your " +
   "library without asking. Removing nzb2seed? See the README's last section first."],
];

export function HelpTab() {
  return html`<div class="help">
    <h1>Help</h1>
    <p class="lead">nzb2seed builds a torrent's files from Usenet, so the torrent can be seeded at 100% without
      downloading it over BitTorrent.</p>
    <p><button type="button" class="btn primary" onClick=${startTour}>Take the tour again</button></p>

    <h2>What each tab is for</h2>
    <dl class="help-tabs">${TABS.map(t => html`
      <div key=${t.id}>
        <dt><a href=${"#" + t.id}>${t.name}</a></dt>
        <dd><p>${t.does}</p><p class="use"><b>Useful for:</b> ${t.use}</p></dd>
      </div>`)}
    </dl>

    <h2>Good to know</h2>
    ${IDEAS.map(([q, a]) => html`<details key=${q}><summary>${q}</summary><p>${a}</p></details>`)}
  </div>`;
}
