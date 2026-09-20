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
   turn it off.
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
* **Posts named by episode title** (`Show De Dierenwinkel FLEMISH - GRP`, no
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
