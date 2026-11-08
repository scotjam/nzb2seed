# nzb2seed

Build a **100% complete torrent entirely from Usenet downloads**, so it can be
seeded without downloading a byte over BitTorrent (no hit-and-run risk).

```
.torrent (search, or a file you have) ─► RARs inside → SABnzbd +Repair, unpacked files → +Repair/Unpack
        │
NZBs, closest match first:  multi-season ─► season ─► episode   (same release group
        │                   and resolution as the torrent's own files; never smaller)
        ▼
files laid out exactly like the torrent (Sample/ Proof/ Subs/, any folder names),
text files fixed only where that makes their pieces verify, extras removed
        │
every piece hashed ─► failing pieces? other posts of that file until they verify
        │
qBittorrent: add STOPPED ─► set location ─► force recheck ─► 100.0%
```

## Before you start: what nzb2seed needs

nzb2seed drives three programs you run yourself, and needs a way onto Usenet:

* **A Usenet provider and Usenet indexers.** A *provider* (news server) is the
  paid account that stores the posts and serves the downloads; an *indexer* is
  the search site whose NZB files say which posts make up a release. You need
  at least one of each: the provider goes into SABnzbd, the indexers into
  Prowlarr. SABnzbd's guide explains both:
  [quick setup](https://sabnzbd.org/wiki/introduction/quick-setup) (adding
  your provider's server) and [NZB sources](https://sabnzbd.org/wiki/introduction/nzb-sources)
  (indexers).
* **SABnzbd** downloads from Usenet.
* **qBittorrent** checks each build piece by piece and seeds it.
* **Prowlarr** searches your torrent trackers and your Usenet indexers.
* **cross-seed** (recommended) finds the same release on your other trackers
  and seeds it there too, from the same files.

### SABnzbd

```yaml
services:
  sabnzbd:
    image: lscr.io/linuxserver/sabnzbd:latest
    container_name: sabnzbd
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=Europe/London
    ports:
      - "8080:8080"
    volumes:
      - ./sabnzbd:/config
      - /srv/disk1/downloads:/downloads     # where downloads land
    restart: unless-stopped
```

At `http://<server>:8080` its wizard asks for your **provider's server**
(host, port, SSL, user name and password - from your provider's account
page; [servers](https://sabnzbd.org/wiki/configuration/4.5/servers)). Then
copy the **API key** (*Config -> General -> Security*) into nzb2seed's
**Settings -> SABnzbd** with its URL. The [SABnzbd wiki](https://sabnzbd.org/wiki/)
and the [container's page](https://docs.linuxserver.io/images/docker-sabnzbd/)
cover the rest.

### qBittorrent

```yaml
services:
  qbittorrent:
    image: lscr.io/linuxserver/qbittorrent:latest
    container_name: qbittorrent
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=Europe/London
      - WEBUI_PORT=8081                     # 8080 is SABnzbd's
    ports:
      - "8081:8081"
      - "6881:6881"
      - "6881:6881/udp"
    volumes:
      - ./qbittorrent:/config
      - /srv/disk1:/data                    # where torrents are seeded from
    restart: unless-stopped
```

At `http://<server>:8081` sign in (the container's log prints the first
password), set your own under *Tools -> Options -> Web UI*, and enter the
URL, user name and password in nzb2seed's **Settings -> qBittorrent**. See the
[container's page](https://docs.linuxserver.io/images/docker-qbittorrent/)
and the [qBittorrent wiki](https://github.com/qbittorrent/qBittorrent/wiki).

**Forward qBittorrent's listening port.** Builds that stop a few percent short
get the rest over BitTorrent, often from a torrent with only a few seeders - an
older release, or a pack only one or two people still seed. Two peers that
cannot accept incoming connections cannot reach each other at all, so if your
qBittorrent is not connectable and the remaining seeders are not either, the
download never starts and the torrent sits short of 100% for good. Forward the
port qBittorrent listens on (*Tools -> Options -> Connection -> Port used for
incoming connections*; 6881 in the example above) to the server on your router,
TCP and UDP. Behind a VPN, use one that offers port forwarding and set
qBittorrent's port to the one the VPN gives you. qBittorrent's status bar then
shows its connection status as connected, not firewalled, and many trackers
show you as connectable on your profile. Being connectable also gets you more
of the upload when you seed.

**Paths:** when SABnzbd or qBittorrent run in containers they see your disks
under other names (`/downloads`, `/data`). Tell nzb2seed in **Settings**:
*SABnzbd path -> path on this machine* and *Path on this machine ->
qBittorrent path* (for example `/data` -> `/srv/disk1`).

**SABnzbd stops downloading posts it cannot repair** (*Config -> Switches ->
Abort jobs that cannot be completed*, on by default). Most of a broken post
usually still checks out piece by piece, so nzb2seed can build from it - if it
was downloaded. nzb2seed's Settings warns while it is on and can turn it off;
a job that used such a post offers to download the rest, just that once or
always. With it off, SABnzbd still tries each article a few times on each news
server (`max_art_tries`, 3 by default), then marks the job *Failed - Repair
failed, not enough repair blocks* in its History, so it never retries forever;
it only means the whole post is downloaded, even when it cannot be repaired.

### Prowlarr

nzb2seed finds everything through [Prowlarr](https://prowlarr.com/): it
searches your **torrent trackers** for the .torrent, and your **Usenet
indexers** for the NZBs that hold the same release. Prowlarr has to be set up
with both before nzb2seed can build anything (SABnzbd and qBittorrent are needed
too - see above). The [quick start guide](https://wiki.servarr.com/prowlarr/quick-start-guide)
covers it in full; in short, with Docker
([other install methods](https://wiki.servarr.com/prowlarr/installation)):

```yaml
services:
  prowlarr:
    image: lscr.io/linuxserver/prowlarr:latest
    container_name: prowlarr
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=Europe/London
    ports:
      - "9696:9696"
    volumes:
      - ./prowlarr:/config
    restart: unless-stopped
```

Then, at `http://<server>:9696`:

1. **Set a login** when it asks (*Settings -> General -> Authentication*).
2. **Add your torrent trackers**: *Indexers -> Add Indexer*, search for the
   tracker and enter what it asks for (usually your passkey, API key or
   cookie, from your profile on the tracker). Leave **Redirect** off, so
   Prowlarr fetches .torrent files itself.
3. **Add your Usenet indexers** the same way (most are listed by name; any
   other works as *Generic Newznab* with its URL and API key)
   ([indexers](https://wiki.servarr.com/prowlarr/indexers)).
4. **Copy the API key**: *Settings -> General -> API Key*.

In nzb2seed's **Settings**, under *Prowlarr*, enter its URL (for example
`http://192.0.2.10:9696`) and that API key, and press *Test*. *Only these
indexer ids* and *Only these categories* can narrow what it searches; blank
means everything.

### cross-seed (recommended)

Not needed to build anything, but recommended:
[cross-seed](https://www.cross-seed.org) finds the same release on your other
trackers and adds it to qBittorrent seeding from the files you already have
(hard links, nothing downloaded). Every torrent nzb2seed builds - from the
Build, Automatic or Library tab - then seeds on each tracker that has it, so
one Usenet download grows your stats on all of them. cross-eyed (for the
Library tab) works alongside it too. The
[cross-seed documentation](https://www.cross-seed.org/docs/basics/getting-started)
has the details.

```yaml
services:
  cross-seed:
    image: ghcr.io/cross-seed/cross-seed:6
    container_name: cross-seed
    command: daemon
    ports:
      - "2468:2468"
    volumes:
      - ./cross-seed:/config
      - /srv/disk1/data:/data          # the SAME paths qBittorrent uses for its downloads
    restart: unless-stopped
```

```
docker compose run --rm cross-seed gen-config   # writes ./cross-seed/config.js
docker compose run --rm cross-seed api-key      # prints the key the webhook below needs
```

Edit `config.js` (the settings below matter most), then `docker compose up -d cross-seed`:

```js
torznab: ["http://prowlarr:9696/1/api?apikey=YOUR_PROWLARR_KEY"],   // one per tracker, from Prowlarr
torrentClients: ["qbittorrent:http://user:password@192.0.2.10:8080"],
action: "inject",                   // add matches to qBittorrent itself
linkDirs: ["/data/cross-seed"],     // on the same disk as your downloads, so links work
linkType: "hardlink",
matchMode: "flexible",
autoResumeMaxDownload: 0,           // never let a near-match download into shared files
```

* **`linkDirs`** must be on the same disk as the files it links to; with
  several disks, list one folder per disk.
* **`autoResumeMaxDownload: 0`** matters: a near-match left stopped can never
  write its few differing pieces into files other torrents seed from
  (cross-eyed's *Short & shared* page sorts those out safely).
* To search each torrent as it finishes, set qBittorrent's *Options -> Downloads
  -> Run external program on torrent finished* to
  `curl -XPOST http://cross-seed:2468/api/webhook?apikey=YOUR_CROSS_SEED_KEY -d "infoHash=%I"`.
  Then every build is searched for on your other trackers as soon as it is
  complete. While nzb2seed's Library is on, it routes this through itself and
  holds the calls back while a season is built or moved (*Hold back cross-seed*
  in the Library tab), then puts your setting back when the Library is
  switched off.

### Torrents left short of 100%, and the originals kept for them

The same release is not always byte for byte the same everywhere: a tracker's
copy of an episode can differ in a few pieces from the Usenet post (a re-mux,
a fixed header). A cross-seed of such a release, or a Library season, then
sits stopped in qBittorrent short of 100%.

Every half hour nzb2seed looks at the stopped torrents short of 100% and tries
the files they are missing from elsewhere: first an original nzb2seed kept
(below) - a torrent stuck a little short was linked from the files already
here, so their version is the one it has had - then another torrent of the
same name in qBittorrent. The file is replaced by a
hard link to that copy - never written into, so the library and the torrents
sharing the old file keep theirs - and rechecked. A copy that comes out no
better is put back. At 100.0% the torrent is started: it only seeds, nothing
was downloaded. Torrents match by name even when one tracker names a
single-file torrent after its file and another after a folder, and an
unfinished torrent's files are looked for in qBittorrent's download folder
too.

When nothing here is left to try, the rest can come from the torrent's own
tracker (with few seeders it only comes if qBittorrent is connectable - see
*Forward qBittorrent's listening port* under qBittorrent above) - only a
tracker ticked **always add** (Settings, "Builds that stop a
few percent short"), and only while what is missing is within the limit you
set for it, counting what earlier starts of the torrent downloaded, with 10%
of the limit kept free. Before it starts, every file the download could write
into - each short file, and any whole file sharing a piece with one - gets a
copy of its own (cross-seeded files are usually shared with other torrents,
which would otherwise change too) and its present version is kept as an
original. If a file would stay shared (no room for the copy), it is not
started. The same applies when you add a build with *Add to torrent client*.
Each torrent is started this way once - one you stop again stays stopped - and
nothing starts while the free-space guard is holding downloads.

With cross-eyed set up (Library tab), mend leaves alone any torrent cross-eyed
has a job open on - anything not done or rolled back, a failed one included,
since it can leave a safety copy linked to the files - and does not use such a
torrent's files to finish another. Before downloading anything it asks
cross-eyed, which works it out from piece hashes:

* **repairable** - other copies on disk have the missing pieces right:
  cross-eyed repairs it, nothing is downloaded (no ratio cost);
* **shared, and the other torrents need those bytes as they are** - left alone;
* **shared, and the other torrents need the same bytes** - downloaded into the
  shared files, which fixes them all;
* **its own files**, or **files a library or backup holds too** - downloaded
  after each file gets a copy of its own.

What mend did and why it left a torrent alone is listed under *Mend* in the
Jobs tab; in cross-eyed's dry-run it says the job or repair only runs once you
run it there or switch cross-eyed to live. If cross-eyed does not answer, mend
swaps no files in and only downloads into files of their own.

**Originals.** A build changes its own files in two cases: when pieces fail
and another post's copy replaces a file (or patches a piece of it), and when
a build a little short of 100% is handed to qBittorrent to download the rest
over BitTorrent. The Usenet version of those files is often the very version
other trackers carry, so before either change nzb2seed keeps it - a second
name for the same bytes in `.nzb2seed-originals` at the top of the same disk -
and the change goes into a copy of the file of its own. Keeping it is instant;
what it costs is the room for that copy, and only for the files about to
change, not the whole torrent (without room for it, nothing is kept and the
build goes on as before). Your other trackers' cross-seeds of the release are
then finished from it, with nothing downloaded.

Originals are kept for `keep_originals_days` (3 by default, in
`[behaviour]`; 0 keeps none) - long enough for cross-seed to find the release
on your other trackers and for the next rounds to finish them. After that only
the name in `.nzb2seed-originals` goes: a torrent that was finished from an
original keeps its bytes. What was kept is listed in `originals.json` next to
the config.

## Running it

Python 3.10+ with `requests`. Run it on the machine that holds the
downloads (e.g. the NAS) so moves never cross the network.

```
python3 -m nzb2seed gui --host 0.0.0.0          # web GUI on http://<nas>:9797/
python3 -m nzb2seed search "Show.S01E01.1080p.BluRay.x264-GRP"
python3 -m nzb2seed run    "Show.S01E01.1080p.BluRay.x264-GRP" --indexer MyTracker -y
python3 -m nzb2seed run    --torrent "Show.S01.1080p.BluRay.x264-GRP.torrent" --dry-run   # show the plan
python3 -m nzb2seed assemble my.torrent /path/to/finished/download --dry-run
python3 -m nzb2seed season "Show Name" 1 --group GRP --res 720p
python3 -m nzb2seed series "Show Name" --library "/data/tv/Show Name" --plan
```

To keep the GUI running, install it as a systemd service (see below). It
keeps its job list in `jobs.json` next to the config, so restarts lose nothing.
`systemctl restart nzb2seed` after deploying new code; `journalctl -u nzb2seed`
for its output.

The **GUI** has four tabs: *Build* (search - or upload a .torrent you already
have - and pick the torrent on the left; NZBs
with its exact name are ticked, anything else you tick is tried first, and
with nothing ticked nzb2seed finds the NZBs itself; *Search Usenet* looks for
other NZBs; NZBs from earlier attempts at that torrent are listed below with
what became of them), *Jobs* (live progress, a map of every piece as it
verifies, full log, cancel, and a pick list when a build needs you to choose
an NZB), *Assemble* (a .torrent plus folders you already have) and *Settings*
(edit and test the connections).
It only answers to its own names/IPs (add others with `--allowed-host`).

### Login

The GUI asks for a login (your browser shows the prompt). The default is:

| User name | Password |
|---|---|
| `admin` | `nzb2seed` |

**Change the password** in the GUI under *Settings → GUI login* (it takes
effect immediately and is saved to `nzb2seed.toml` under `[gui]`). While the
default password is in use the GUI shows a reminder. `--username` /
`--password` on the command line override the config. An empty password
switches the login off; the GUI then only lets in this machine and private
(LAN) addresses.

`run` flags: `--torrent FILE` (build a .torrent you already have),
`--indexer X` (only torrents from indexers matching X), `--pp auto|repair|unpack`,
`--local-verify` (hash every piece before qBittorrent), `--no-retry` (stop
instead of trying other posts when pieces fail), `--start` (start seeding at
100%), `--no-cleanup`, `--no-qbit`, `--output-dir`, `--dry-run` (with
`--torrent`: show the plan, download nothing), `-y` (no prompts).

## What it does, step by step

1. **The .torrent** comes from a Prowlarr search or a file you give it.
   Magnet-only releases are refused; a torrent already active in qBittorrent
   stops the build.
2. **Post-processing** (`auto`): a torrent that is mostly RAR volumes gets
   **+Repair** (SABnzbd must not unpack); one with unpacked files - even with a
   small `Subs/*.subs.rar` - gets **+Repair/Unpack** (archives kept, removed
   later as extras). **+Delete is never used**; the level is read back from
   SABnzbd after queuing and corrected if anything changed it.
3. **Finding the NZBs, closest match first.** The torrent is a tree: a movie or
   an episode is one part; a season pack is a season with episodes under it; a
   multi-season pack has seasons under it. Each part is tried with NZBs of its
   own kind - your picks first, then everything Prowlarr has **from the release
   group and resolution of the torrent's own files** and **at least as large**
   as the part - and only split into seasons, then episodes, once all of those
   have been tried. A part that mixes release groups goes straight down a level.
   Season/whole candidates are ranked by looking inside their NZBs (enough
   bytes posted, file names). When nothing fits, the same-group results are
   shown for you to pick from (GUI pick list, or a prompt in the CLI).
4. **Nothing is downloaded twice**: finished downloads of the same NZB are
   reused (nzb2seed keeps a record of its SABnzbd jobs in `sab_jobs.json`), a
   post that failed before is skipped, a short season post still supplies the
   episodes it holds, and files already placed by an earlier run stay. Whether
   two NZBs are **the same post** is judged by the Usenet message-IDs they
   list, not by their size: the same post on another indexer - or an NZB that
   lists fewer of its files - is not downloaded again, while an NZB with a file
   the earlier one lacked (say, the .nfo) or a re-upload still gets its turn.
5. **RAR sets** a download finished with are looked into (7-Zip or unrar) and
   only the files the torrent needs are unpacked, into that download's folder.
6. **Re-packed scene releases.** When a RAR'd torrent's volumes are not in the
   download (a re-packed or obfuscated post: another RAR set around the same
   video, no .nfo/.sfv/Sample), srrDB fills the gap: the video is taken out of
   whatever RARs the post has, its CRC checked against srrDB, and the original
   scene volumes rebuilt byte for byte from srrDB's `.srr` (pyReScene); the
   .nfo and .sfv come from srrDB, and a Sample from any other post whose NZB
   lists it (a trimmed NZB with just that file) and kept only if size and CRC match. The
   piece check below still decides.
7. **Laying out**: each torrent file is matched by name+size, unique size, or a
   piece hash inside the file (obfuscated names), regardless of folder names
   or nesting on either side, and moved to `<output>/<torrent path>`. Moves
   across disks copy to a `.part` file, fsync, rename, then delete the source.
8. **Text files** (.nfo .sfv .txt .md5 .srt): left alone when their pieces
   verify as downloaded (e.g. an LF-only .nfo that the torrent also has as LF);
   rewritten only when the pieces fail *and* a line-ending variant makes them
   pass. Adjacent files sharing a piece are solved together.
9. **Every piece is hashed.** When pieces fail (e.g. a repost with re-muxed
   headers of the same size), the files behind them are replaced with copies
   from other posts of the same release - each copy is checked first and only
   swapped in when it makes the failing pieces pass; on a piece spanning two
   files, both neighbours are tried. On by default; *Try other posts if pieces
   fail* on the build screen, `retry_bad_pieces` in the config or `--no-retry`
   turn it off. Usenet is preferred: a repair downloads a whole other post even
   to fix a few MB. With *Prefer small downloads from permitted trackers over
   large Usenet downloads* ticked in Settings (`prefer_tracker_small`), a post
   bigger than what still fails is not downloaded when a tracker ticked
   **always add**, with seeders, would let that much come over BitTorrent
   within its limit - the build stops nearly complete and is handed on instead.
10. Anything missing or the wrong size **stops the build** before qBittorrent.
11. **qBittorrent**: added stopped, stopped again if it came up running, save
    path set, force recheck. Below 100.0% it reports per file and stays stopped.

## Grabbing a season, or a whole series

The GUI's *Seasons* tab (or `season` / `series` on the command line) grabs from
Usenet without a torrent: one release group and resolution per season, every
episode checked against TVmaze's list and - for scene releases - against srrDB's
CRC of the original video; scene RARs kept or rebuilt, .nfo/.sfv/Sample fetched,
MediaInfo, screenshots, links and a report in a `<Season.Name>-metadata` folder
next to the season (so they never end up in a torrent).

* **All seasons** (the first entry of the season list, or `series`): every season
  TVmaze lists, one after another. The series gets one release group and
  resolution - the one covering the most missing episodes (a resolution in the
  name beats none), or your preferred resolution - and a season without it gets
  its most complete option at the same resolution. *Show the plan* / `--plan`
  lists each season's choice and downloads nothing. A show of the same name from
  another year (the 1998 original next to a 2016 revival) is never mixed in.
* **Skip episodes I already have** (a checkbox that unlocks a chooser for the
  show's folder, or `--library` / `--skip-owned`): episodes in that folder (any
  layout, recognised by SxxEyy in file and folder names; folders of a same-named
  show from another year are left out) and in qBittorrent are not downloaded -
  any copy counts. Where some episodes of a season are skipped, the missing ones
  are fetched as episode NZBs first; a season NZB only fills what is left, and
  the episodes you have are never laid out from it.

* **Several releases, in your order**: tick several releases of a season (or,
  for all seasons, *Find releases for all seasons*) and put them in a priority
  list; each episode comes from the highest release that has it, the next ones
  fill the gaps. Without a list, a series grab fills each season's gaps from
  the other releases at the same resolution.
* **Posts named by episode title** (`Show De Bakkerij FLEMISH - GRP`, no
  SxxEyy) - a checkbox on the Seasons tab, off by default and remembered per
  show (or `--match-names`), because it takes more indexer API searches: they
  are placed by the episode's name from TMDB /
  TheTVDB / TVmaze - only when the name belongs to exactly one episode - and
  offered like any other release; files in your library named that way count
  as yours too.
* **Episode list** (a choice in the tab and in Settings, or `--episodes`): TVmaze,
  TMDB, TheTVDB, IMDb, or **all sources** - per season, whichever lists the most
  episodes (TVmaze's lists are often incomplete for smaller shows). TMDB and
  TheTVDB are read from their public pages, IMDb from its official episode
  dataset (IMDb's pages answer automated requests with a bot check, which
  nzb2seed does not get around). All of it goes through the outbound proxy and
  is cached for a day in `cache/metadata` next to the config (IMDb's dataset for
  a week); *Settings → Clear metadata cache* forgets it. `--library` can be
  given more than once when a show is spread over several folders.

```
python3 -m nzb2seed series "Show Name" --year 2016 --library "/data/tv/Show Name" --plan
python3 -m nzb2seed series "Show Name" --year 2016 --library "/data/tv/Show Name" --res 1080p
```

## Automatic builds from autobrr

A good way to build your tracker stats if you don't have much of a library yet:
new releases are seeded from the moment they appear, when they are most in
demand, without downloading them over BitTorrent. (With a library already, see
the Library tab and *Moving seasons into your library with cross-eyed* below.)

### Installing and setting up autobrr

[autobrr](https://autobrr.com/introduction) watches your trackers' IRC
announce channels and catches new releases the moment they are posted. The
[official docs](https://autobrr.com/installation/docker) cover every install
method; with Docker, on the same machine as nzb2seed:

```yaml
services:
  autobrr:
    image: ghcr.io/autobrr/autobrr:latest
    container_name: autobrr
    ports:
      - "7474:7474"
    volumes:
      - ./autobrr:/config      # nzb2seed's inbox goes in here (see "Inbox" below)
    restart: unless-stopped
```

```
docker compose up -d autobrr
```

Then, at `http://<server>:7474`:

1. **Create your login** on the first visit.
2. **Add your trackers**: *Settings -> Indexers -> Add new*. Each needs the
   details from your tracker profile (passkey or RSS key) and its IRC
   announce channel - autobrr connects to IRC for you
   ([indexers](https://autobrr.com/configuration/indexers),
   [IRC](https://autobrr.com/configuration/irc)).
3. **Make a filter** per kind of release you want to seed: *Filters -> Add
   new*, pick the trackers, and narrow it down - resolution, sources,
   categories, size, release groups ([filters](https://autobrr.com/filters)).
   It needs no action: nzb2seed adds its own when you tick the filter.
4. **Create an API key**: *Settings -> API keys*, for nzb2seed.

### Connecting it to nzb2seed

The *Automatic* tab (switch at the top; a dot in the menu shows whether it is on):

1. **autobrr**: enter its URL and an API key (autobrr: *Settings → API keys*), list
   the filters and tick the ones to build. nzb2seed adds a *watch folder* action
   called "nzb2seed" to each ticked filter through autobrr's API, and removes it
   again when you untick. A ticked filter is enabled in autobrr, and its own
   download actions (qBittorrent,
   SABnzbd, Sonarr...) are switched off while nzb2seed builds its releases -
   otherwise they would download the same release over BitTorrent, which is what
   building from Usenet avoids. Unticking puts it all back as it was: the filter
   to whatever it was before, and only the actions nzb2seed switched off back on.
2. **Inbox**: *Find autobrr's folder* uses autobrr's own config folder
   (`<autobrr config>/nzb2seed-inbox`, `/config/nzb2seed-inbox` inside autobrr),
   which autobrr can already write to - no change to autobrr's container.
3. **Each torrent** autobrr saves there becomes an automatic job: it waits for the
   Usenet post (searching again every *n* minutes, for up to *h* hours), never asks
   anything, builds like the Build tab does, adds the torrent stopped, rechecks, and
   starts seeding only at exactly 100.0%. Anything short stays stopped and is listed
   as such. autobrr fetches the .torrent from the tracker; nzb2seed never contacts it.
4. At most *n* builds at once, and automatic builds make at most *n* Prowlarr
   searches an hour (your own searches are not counted). What happened to each
   torrent is kept in `inbox.json`, so a restart picks up where it was.

## Seeding your library: the Library tab

The *Library* tab works through your TV folders (`Show/Season N/...`) one
season at a time. A season a tracker has as the very release you have is
linked into its torrent and rechecked - nothing is downloaded. Otherwise the
tracker's single-season pack is built from Usenet within a daily budget, and
once it seeds at 100.0% you decide whether it replaces your files. Only packs
with every episode you have are used. An episode no Usenet post has does not
stop the build when BitTorrent can finish it by itself: the pack's tracker - or,
with at least 90% here, another tracker with the same release (same files) - is
ticked **always add**, has seeders, and all that is missing stays within its
limit (10% of it kept free). The build then stops nearly complete and is handed
on (to the other tracker's torrent, if it is that one). Otherwise the pack is
passed over for the season's next usable pack, in the same run.

**Where season packs come from** - under *Trackers and folders*:

* **One tracker (recommended)**: every season pack comes from the tracker you
  choose. Simplest to follow, and all the seeding builds one tracker's stats.
* **Several trackers, in an order you set**: tick the trackers to use and move
  them up or down; the first is tried first. The list also holds **Your
  files** - the very release you have, on *any* ticked tracker - at the top by
  default. Each season takes the first entry with a usable pack:
  * *Your files* at the top: wherever a ticked tracker has your release, it is
    used (linked, nothing downloaded); otherwise the first tracker in the list
    with a pack builds it.
  * *Your files* moved down: the trackers above it are preferred even when
    they only have another release (built from Usenet).
  * A season none of the ticked trackers has is left as *No season pack*.

  One Prowlarr search covers all the ticked trackers, made when the season's
  turn comes. Changing the list makes seasons still waiting search again with
  the new order; seasons already built or seeding stay as they are. Ticking a
  tracker puts seasons left as *No season pack* back in the queue, to try it;
  otherwise they are not searched again unless you click *Try again*. With
  cross-seed (recommended, above) a season also seeds on your other trackers
  that have the same release.

**Redownload even if already in torrent client** gets seasons qBittorrent
already holds from a tracker that is not ticked; a season one of the ticked
trackers already seeds is always left alone.

## Moving seasons into your library with cross-eyed

The *Library* tab is for people whose library came from other sources -
Usenet, Sonarr/Radarr, a tracker that has since closed - and who want it to
boost their torrent tracker stats (seeding size, ratio, bonus points, seeding
time) without downloading those shows again over BitTorrent. It can swap a
season in two ways. Without cross-eyed, the
torrent's files are hard-linked over your season's files. With
[cross-eyed](https://codeberg.org/cross-eyed/cross-eyed), approving a season
**moves the torrent itself** - and every cross-seed copy linked to it - into
the show's folder under its own name, has qBittorrent recheck it there, and
only then lets the old season folder go. cross-eyed is a separate,
self-hosted web app that keeps track of which torrents share which files.

cross-eyed works alongside cross-seed: set that up first (see
*cross-seed (recommended)* above).

### 1. Run cross-eyed

It needs Docker on the machine that holds your downloads and library, the
same machine qBittorrent runs on. A minimal `docker-compose.yml`:

```yaml
services:
  cross-eyed:
    image: codeberg.org/cross-eyed/cross-eyed:latest
    container_name: cross-eyed
    command: ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765"]
    ports:
      - "8765:8765"                 # leave out when it shares a VPN container's network (below)
    environment:
      QBIT_URL: "http://192.0.2.10:8080"      # qBittorrent's Web UI
      QBIT_USER: "admin"
      QBIT_PASS: "your-qbittorrent-password"
      QBIT_CONTAINER_NAME: "qbittorrent"        # so it can read qBittorrent's volume mappings
      CROSS_SEED_URL: "http://192.0.2.10:2468"    # cross-seed, set up above
      CROSS_SEED_API_KEY: "YOUR_CROSS_SEED_KEY"
      HOST_MOUNT_PREFIX: "/host"
      DB_PATH: "/config/cross-eyed.db"
      RULES_PATH: "/config/tracker-rules.yaml"
      DRY_RUN: "true"                           # nothing is changed until you switch it to live
      TZ: "Europe/London"
    volumes:
      - ./config:/config
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - /:/host                                 # your disks, at /host/<the same path>
    restart: unless-stopped
```

```
docker compose up -d
```

Then open `http://<server>:8765`. Things to know:

* **`/:/host`** is how cross-eyed reaches your files: a path such as
  `/srv/disk1/tv/Show` on the server is `/host/srv/disk1/tv/Show` inside the
  container. It must be writable, since moving a torrent writes to the disk.
  To limit what it sees, mount a narrower folder that holds every download and
  library folder (for example `/srv:/host/srv`).
* **The Docker socket** (read-only) lets cross-eyed read qBittorrent's container
  mappings, so it can translate qBittorrent's paths (`/data/...`) to real ones.
* **Dry-run** is on at every start: moves are planned and shown on its *Jobs*
  page but carried out only after you switch cross-eyed to live in its header.
  While it is in dry-run, nzb2seed's Library tab says so and offers no moves
  (auto move waits too); once it is live, seasons ready to swap and moves
  already queued go ahead by themselves. To keep it live across restarts, set
  `DRY_RUN: "false"`.
* **No login.** cross-eyed has no password of its own: keep it on your local
  network, or behind your reverse proxy's authentication.
* **Behind a VPN container** (gluetun): put it in the VPN's network
  (`network_mode: "container:gluetun"`, no `ports:`), and publish 8765 on the
  gluetun container instead.

### 2. Connect nzb2seed

In nzb2seed's **Library** tab, under *Trackers and folders*, set **cross-eyed
address** to where nzb2seed can reach it, for example `http://192.0.2.10:8765`.
If cross-eyed shares a VPN container's network, use the server's LAN address,
not `127.0.0.1`. Save.

* **Where cross-eyed sees your disks** is `cross_eyed_prefix` under `[library]`
  in `nzb2seed.toml`, `/host` by default. Change it only if you mounted your
  disks somewhere else in the cross-eyed container.
* nzb2seed must see your library and downloads at the **same paths as the
  server** (it runs on the server, or its container maps them 1:1), because it
  passes those paths to cross-eyed with the prefix in front.

From then on, **Approve** on a season *Ready to swap* hands it to cross-eyed
instead of hard-linking. Two optional switches build on it:

* **Auto move new torrents to the existing library location using cross-eyed**:
  each season ready to swap is moved by itself, one at a time.
* **Auto delete the existing library files once moved using cross-eyed**:
  **may cause data loss.** Your original season folder is deleted once the
  moved torrent seeds at 100.0% from the library and nothing in qBittorrent
  uses the old folder. Leave it off until you have watched a few moves go
  through.

## Only removing what it added

nzb2seed deletes only:
* leftovers in SABnzbd job folders **it submitted** (.nzb, .par2, archives,
  .srr, poster extras), and
* files/folders **it created** on an earlier run of the same torrent, recorded
  in `torrents/<infohash>.owned.json` (including interrupted `.part` copies),
* and, once a torrent is complete, the leftovers of every download it made for
  that torrent on earlier attempts.

A file already at a target path that nzb2seed did not create is used as-is when
it has the right size and otherwise stops the build - it is never moved,
rewritten or deleted. Other files in an existing torrent folder are never
touched. `assemble` never moves, changes or deletes anything in the folders you point it at:
their files are hard-linked into place (or copied, across disks), and files the
torrent has unpacked but your folder holds as RAR sets (a scene release next to a
torrent of its videos) are unpacked into a temporary folder on the output disk.
Files that are in none of the folders are downloaded from Usenet the way a build
does it - only those files (the Assemble tab's *Download missing files from Usenet*,
on by default; `--no-fetch` on the command line) - and only those downloads are
tidied afterwards.

## Paths

SABnzbd, nzb2seed and qBittorrent often see the same folders under different
names (Docker volumes, network shares). For example:

| | SABnzbd sees | nzb2seed (host) sees | qBittorrent sees |
|---|---|---|---|
| finished NZBs (SSD) | `/downloads` | `/mnt/ssd/downloads` | - |
| finished torrents | - | `/mnt/hdd/complete` | `/data/complete` |

Configured in `nzb2seed.toml` (`[paths] sab_to_local`, `local_to_qbit`,
`output_dir`) or the GUI's Settings tab.

## Network: nzb2seed never contacts an indexer or tracker

Only Prowlarr, SABnzbd and qBittorrent do - in whatever network they run in
(e.g. a VPN container):

* **searches and .torrent files**: Prowlarr (with "Redirect" off for torrent
  indexers, Prowlarr fetches the file itself; a redirect is refused, never
  followed);
* **.nzb files**: Prowlarr always answers those with a redirect to the indexer,
  so SABnzbd fetches them - nzb2seed hands it Prowlarr's link as a **paused**
  job (named `nzb2seed-check-...`) and reads the NZB SABnzbd saved. A post
  that is then wanted is resumed (no second grab); the rest are deleted from
  the queue before anything downloads, at the latest when the build ends
  (paused check jobs left by a killed build are removed by the next one).

nzb2seed's only own internet use is the srrDB / predb / xrel.to / TVmaze
lookups, and those go **only** through the proxy in `[network] proxy` (e.g. the
VPN container's HTTP proxy bound to `127.0.0.1`) - without one they are skipped.

## Running the GUI as a service

```ini
# /etc/systemd/system/nzb2seed.service
[Unit]
Description=nzb2seed web GUI
Wants=network-online.target
After=network-online.target docker.service

[Service]
WorkingDirectory=/opt/nzb2seed
ExecStart=/usr/bin/python3 -m nzb2seed --config /opt/nzb2seed/nzb2seed.toml gui --host 0.0.0.0 --no-browser
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`systemctl enable --now nzb2seed`; after updating the code, `systemctl restart nzb2seed`.

## Removing nzb2seed

While the Library tab is switched on, qBittorrent's "Run external program on torrent
finished" (usually cross-seed's search) is pointed at nzb2seed, which holds those calls
back while a season is being built or moved and runs them once it is idle. The original
setting is saved in `hook.json` and put back when the tab is switched off. Before removing
nzb2seed, switch the Library tab off - or run

    python3 -m nzb2seed --config /opt/nzb2seed/nzb2seed.toml restore-qbit-hook

which puts the original program back and runs any held calls through it.

## Tests

`python -m pytest -q` - 80 tests, including a full run against fake
Prowlarr/SABnzbd/qBittorrent whose recheck really hashes the files, season
packs with mixed LF/CRLF .nfo files, cross-disk moves, and the ownership rules.

## License

nzb2seed is free software: you can redistribute it and/or modify it under the
terms of the GNU General Public License as published by the Free Software
Foundation, version 3 or (at your option) any later version. See
[LICENSE](LICENSE).

## Third-party code

nzb2seed ships [pyReScene](https://github.com/srrDB/pyrescene) (MIT licence) in
`nzb2seed/_vendor/rescene`, used to rebuild original scene RAR volumes from srrDB's `.srr`
files. Its licence is in `nzb2seed/_vendor/PYRESCENE-COPYING`; the exact source commit is
in `nzb2seed/_vendor/README.md`.
