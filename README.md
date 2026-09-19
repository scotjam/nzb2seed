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
   lists it, downloaded on its own and kept only if size and CRC match. The
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
touched. `assemble` never deletes anything from the folders you point it at.

## Paths

SABnzbd, nzb2seed and qBittorrent often see the same folders under different
names (Docker volumes, network shares). For example:

| | SABnzbd sees | nzb2seed (host) sees | qBittorrent sees |
|---|---|---|---|
| finished NZBs (SSD) | `/downloads` | `/mnt/ssd/downloads` | - |
| finished torrents | - | `/mnt/hdd/complete` | `/data/complete` |

Configured in `nzb2seed.toml` (`[paths] sab_to_local`, `local_to_qbit`,
`output_dir`) or the GUI's Settings tab.

## Network: nothing leaves from this machine's own address

nzb2seed never talks to an indexer or tracker itself for anything Prowlarr,
SABnzbd or qBittorrent can do. Two things do need the internet: Usenet
indexers' .nzb downloads (Prowlarr always hands those out as a redirect to the
indexer) and the srrDB / predb / xrel.to / TVmaze lookups. Both go **only**
through the proxy in `[network] proxy` - e.g. the HTTP proxy of the VPN
container the other services run in, bound to `127.0.0.1` - and are refused
when no proxy is set, rather than made from the host's own address.

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
