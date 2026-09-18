# nzb2seed

Build a **100% complete torrent entirely from Usenet downloads**, so it can be
seeded without downloading a byte over BitTorrent (no hit-and-run risk).

```
Prowlarr search ──► torrent + matching NZBs
        │
        ├──► .torrent (decides: RARs inside → SABnzbd +Repair, unpacked files → +Repair/Unpack)
        │
        └──► NZBs: smaller than the torrent → skipped; the rest looked inside and ranked;
             best post → SABnzbd (never +Delete); a post that lacks files → next post
                                   │
        files laid out exactly like the torrent (Sample/ Proof/ Subs/, any folder names),
        text files fixed only where that makes their pieces verify, extras removed
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
python3 -m nzb2seed assemble my.torrent /path/to/finished/download --dry-run
```

To keep the GUI running, install it as a systemd service (see below). It
keeps its job list in `jobs.json` next to the config, so restarts lose nothing.
`systemctl restart nzb2seed` after deploying new code; `journalctl -u nzb2seed`
for its output.

The **GUI** has four tabs: *Build* (search, pick the torrent on the left, the
matching NZBs on the right are ticked for you), *Jobs* (live progress, a map of
every piece as it verifies, full log, cancel), *Assemble* (a .torrent plus
folders you already have) and *Settings* (edit and test the connections).
Without `--password` it only lets in this machine and private LAN addresses;
it only answers to its own names/IPs (add others with `--allowed-host`).

`run` flags: `--indexer X` (only torrents from indexers matching X),
`--pp auto|repair|unpack`, `--local-verify` (hash every piece before
qBittorrent), `--start` (start seeding at 100%), `--no-cleanup`, `--no-qbit`,
`--output-dir`, `--dry-run`, `-y` (no prompts; needs an unambiguous match).

## What it does, step by step

1. **Search** Prowlarr; pair the torrent with Usenet posts of the same name, or
   a season pack with its `SxxEyy` episode posts.
2. **.torrent** is fetched first. Magnet-only releases are refused; a torrent
   already active in qBittorrent stops the build.
3. **Post-processing** (`auto`): a torrent that is mostly RAR volumes gets
   **+Repair** (SABnzbd must not unpack); one with unpacked files - even with a
   small `Subs/*.subs.rar` - gets **+Repair/Unpack** (archives kept, removed
   later as extras). **+Delete is never used**; the level is read back from
   SABnzbd after queuing and corrected if anything changed it.
4. **Choosing the post**: NZBs smaller than the torrent are skipped. The rest
   (one per distinct size) are opened and scored: are enough bytes posted
   (yEnc ×0.995-1.06 of the torrent), and how many of the torrent's file names
   appear. The best is downloaded; if it turns out to lack files, the next is
   tried (up to 3). Downloads of posts that fell short are removed once the
   torrent is complete.
5. **Laying out**: each torrent file is matched by name+size, unique size, or a
   piece hash inside the file (obfuscated names), regardless of folder names
   or nesting on either side, and moved to `<output>/<torrent path>`. Moves
   across disks copy to a `.part` file, fsync, rename, then delete the source.
6. **Text files** (.nfo .sfv .txt .md5 .srt): left alone when their pieces
   verify as downloaded (e.g. an LF-only .nfo that the torrent also has as LF);
   rewritten only when the pieces fail *and* a line-ending variant makes them
   pass. Adjacent files sharing a piece are solved together.
7. Anything missing or the wrong size **stops the build** before qBittorrent.
8. **qBittorrent**: added stopped, stopped again if it came up running, save
   path set, force recheck. Below 100.0% it reports per file and stays stopped.

## Only removing what it added

nzb2seed deletes only:
* leftovers in SABnzbd job folders **it submitted** (.nzb, .par2, archives,
  .srr, poster extras), and
* files/folders **it created** on an earlier run of the same torrent, recorded
  in `torrents/<infohash>.owned.json` (including interrupted `.part` copies).

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

`python -m pytest -q` - 42 tests, including a full run against fake
Prowlarr/SABnzbd/qBittorrent whose recheck really hashes the files, season
packs with mixed LF/CRLF .nfo files, cross-disk moves, and the ownership rules.
