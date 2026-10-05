"""The build pipeline, shared by the CLI and the GUI.

Output goes through ``report`` so the same code can print to a console or
feed a GUI job log.
"""
from __future__ import annotations

import dataclasses
import glob
import itertools
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from . import metadata
from . import assemble as asm
from . import archives, grab, matching, nzbinfo, space
from .clients import (PP_NONE, PP_REPAIR, PP_UNPACK, SAB_DONE, SAB_FAILED, ApiError,
                      Prowlarr, QBittorrent, Release, SABnzbd)
from .config import Config
from .ledger import Ledger
from .pathmap import map_path
from .report import ask as report_ask
from .report import torrent as report_torrent
from .report import check_cancel, end_progress, info, line, pieces, progress, step, warn
from .torrent import PieceVerifier, Torrent, parse

STOPPED = {"pausedUP", "pausedDL", "stoppedUP", "stoppedDL", "error", "missingFiles"}


class Abort(Exception):
    pass


class Incomplete(Abort):
    """A build that got most of the way. It says how far, and carries what is needed to
    finish it by other means - so a torrent that is 97% built from Usenet is not thrown
    away when many trackers let you download a few percent without it counting towards a
    hit-and-run. Nothing of it is deleted: the person decides what happens next."""

    def __init__(self, message: str, have: float, infohash: str, torrent_path: str,
                 save_path: str, in_client: bool, tracker: str = "", short: int | None = None,
                 seeders: int | None = None):
        super().__init__(message)
        # seeders Prowlarr reported for the torrent (None = not known). With none, the
        # missing part could never come over BitTorrent, so it is not offered
        self.seeders = seeders if seeders is not None else getattr(_this_build, "seeders", None)
        self.short = short                      # bytes missing, when known
        self.have, self.infohash, self.torrent_path = have, infohash, torrent_path
        self.save_path, self.in_client, self.tracker = save_path, in_client, tracker
        # parts only small files were missing from, left without downloading another whole
        # post: a whole post may still hold them (a Sample inside an obfuscated RAR set)
        self.settled = list(getattr(_this_build, "settled", []) or [])

    def details(self) -> dict:
        # unrounded: a few KB short of a season rounds to 1.0, which then reads as 100%
        return {"have": self.have, "infohash": self.infohash,
                "torrent_path": self.torrent_path, "save_path": self.save_path,
                "in_client": self.in_client, "tracker": self.tracker, "short": self.short,
                "seeders": self.seeders, "settled": self.settled}


@dataclass
class Options:
    pp: str | None = None          # "auto" | "repair" | "unpack" | None = config default
    output_dir: str | None = None
    no_cleanup: bool = False
    local_verify: bool = False
    no_qbit: bool = False
    start: bool = False
    dry_run: bool = False
    retry_bad: bool | None = None  # None = config default (behaviour.retry_bad_pieces)
    fetch_missing: bool = False    # assemble: download what the folders do not have from Usenet
    unattended: bool = False       # automatic builds: never ask the person to pick a post
    peek: bool | None = None       # look inside a RAR post first (None = behaviour.peek_archives)
    whole_posts: bool = False      # a small file missing: try whole posts for it before settling
    related: tuple = ()            # infohashes of torrents of the same release built before


def gb(n: int) -> str:
    return f"{n / 1024**3:.2f} GB" if n >= 1024**3 else f"{n / 1024**2:.1f} MB"


def exact_gb(n: int) -> str:
    """A shortfall, to four decimals and rounded *up*: "0.0 MB missing" beside a torrent
    that is not complete reads as though nothing is wrong, so a gap is never shown as 0."""
    unit, size = ("GB", 1024 ** 3) if n >= 1024 ** 3 else ("MB", 1024 ** 2)
    return f"{math.ceil(n / size * 10000) / 10000:.4f} {unit}"


def exact_pct(fraction: float) -> str:
    """How complete, to four decimals and rounded *down*: a torrent 30 bytes short of
    25 GB is 99.9999999% there, and rounding that to "100.0000%" would hide the gap."""
    return f"{math.floor(fraction * 1000000) / 10000:.4f}%"


# ---------------------------------------------------------------- pairing

def search(cfg: Config, query: str) -> tuple[list[Release], list[Release]]:
    pr = Prowlarr(cfg.prowlarr_url, cfg.prowlarr_key)
    results = pr.search(query, cfg.indexer_ids, cfg.categories)
    return ([r for r in results if r.protocol == "torrent"],
            [r for r in results if r.protocol == "usenet"])


def pair(cfg: Config, pr: Prowlarr, torrent: Release, nzbs: list[Release]
         ) -> tuple[list[list[Release]], list[Release], str]:
    """Usenet groups for a torrent: each group is one release, later entries are
    fallbacks (same post on other indexers). Also returns the (possibly grown)
    NZB list and a short description of how the pairing was made."""
    exact = matching.rank_nzbs(matching.exact_matches(torrent, nzbs))
    if exact and torrent.size:
        # smaller NZBs cannot hold the whole torrent (re-checked against the real .torrent later)
        exact = [n for n in exact if n.size >= torrent.size] or exact
    if exact:
        return [exact], nzbs, f"exact name match on {len(exact)} indexer(s): " + \
            ", ".join(n.indexer for n in exact)
    # Nothing is ticked otherwise: the build finds NZBs itself, closest match first
    # (multi-season, then season, then episode) - ticking episodes here would skip that.
    return [], nzbs, "automatic"


def group_selection(nzbs: list[Release]) -> list[list[Release]]:
    """Hand-picked NZBs: identical names are one release with fallbacks."""
    groups: dict[str, list[Release]] = {}
    for n in nzbs:
        groups.setdefault(matching.norm(n.title), []).append(n)
    return list(groups.values())


def size_warning(torrent: Release, groups: list[list[Release]]):
    total = sum(g[0].size for g in groups)
    if torrent.size and total < torrent.size * 0.97:
        warn(f"NZB total {gb(total)} is smaller than the torrent ({gb(torrent.size)}); "
             "some torrent files will probably be missing")


# ---------------------------------------------------------------- SABnzbd

def sab_preflight(sab: SABnzbd, pp: int):
    try:
        conf = sab.config()
    except ApiError as e:
        warn(f"could not read SABnzbd config ({e})")
        return
    misc = conf.get("misc", {})
    if str(misc.get("ignore_samples", "0")) not in ("0", "", "False"):
        warn("SABnzbd 'ignore_samples' is on: Sample files will be skipped and the torrent "
             "cannot reach 100% if it has a Sample folder")
    if misc.get("unwanted_extensions") and str(misc.get("action_on_unwanted_extensions", "0")) != "0":
        warn(f"SABnzbd acts on unwanted extensions ({misc.get('unwanted_extensions')}); "
             "a job may be paused/aborted")
    if misc.get("cleanup_list"):
        warn(f"SABnzbd cleanup list removes: {misc.get('cleanup_list')} - these will be missing "
             "if the torrent contains them")
    if pp == PP_REPAIR and str(misc.get("deobfuscate_final_filenames", "0")) not in ("0", "False"):
        info("SABnzbd deobfuscation is on; renamed files are matched by size/hash instead of name")


MAX_PEEK = 8        # NZBs looked inside per build (each is a "grab" on most indexers)
_RAR = re.compile(r"\.(rar|r\d{2,3}|\d{3})$", re.I)


def packed_release(t: Torrent) -> bool:
    """Is the torrent a RAR'd release? Judged by bytes, so an unpacked release that
    only carries a small Subs/*.subs.rar still counts as unpacked."""
    rar = sum(f.length for f in t.real_files if _RAR.search(f.name))
    return rar > t.total_size / 2


def choose_pp(opts: Options, cfg: Config, t: Torrent) -> int:
    """RARs in the torrent: +Repair only, SABnzbd must not unpack. Unpacked files in the
    torrent: +Repair/Unpack (archives are kept, then removed as extras). Never +Delete."""
    mode = opts.pp or cfg.post_processing
    if mode == "repair":
        return PP_REPAIR
    if mode == "unpack":
        return PP_UNPACK
    return PP_REPAIR if packed_release(t) else PP_UNPACK


def rank_posts(pr: Prowlarr, t: Torrent, group: list[Release]):
    """Candidate posts for one release, best first: [(release, nzb bytes | None, score | None)].

    NZBs smaller than the torrent are dropped (they cannot hold all of it), the rest are
    ranked by looking inside them, and NZBs of the very same post (same Usenet articles,
    listed by several indexers) count once."""
    big = [r for r in group if r.size >= t.total_size]
    if len(big) < len(group):
        info(f"skipping {len(group) - len(big)} NZB(s) smaller than the torrent ({gb(t.total_size)}); "
             "they cannot contain all of it")
    uniq = sorted(big, key=lambda r: -(r.grabs or 0))
    ranked, seen = [], []
    for r in uniq[:MAX_PEEK]:
        check_cancel()
        try:
            data = pr.fetch(r)
            sc = nzbinfo.score(nzbinfo.parse(data), t)
            ids = set(nzbinfo.post_ids(data))
        except (ApiError, ET.ParseError) as e:
            warn(f"{r.indexer}: could not read the NZB ({e})")
            continue
        if ids and ids in seen:
            info(f"{r.indexer[:16]:<16} {gb(r.size):>9}  the same post as one listed above")
            continue
        seen.append(ids)
        info(f"{r.indexer[:16]:<16} {gb(r.size):>9}  {sc.summary}")
        ranked.append((r, data, sc))
    ranked.sort(key=lambda x: (x[2].key, x[0].grabs or 0), reverse=True)
    return ranked + [(r, None, None) for r in uniq[MAX_PEEK:]]


class Verdicts:
    """Posts already found not to hold what part of a torrent needs, kept across runs.

    Only verdicts that cannot change are kept: a post whose archive holds a different
    release, or one that downloaded completely and still lacked the files. Those never
    improve, so a retry skips them without so much as asking the indexer for the NZB
    again. What can change is not kept - an NZB that failed to fetch, a timeout, an
    indexer that was down - so a retry tries those afresh.

    Keyed by the torrent and the part of it (a season, an episode) the post was wanted
    for: a post that lacks one episode may still be the right thing for another. The same
    post listed by another indexer is recognised by its name and exact size - a post that
    had the missing file would be bigger by that file."""

    def __init__(self, path: str):
        self.path = path

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as fh:
                return json.load(fh) or {}
        except (OSError, ValueError):
            return {}

    def seen(self, infohash: str, label: str, rel: Release) -> str:
        """Why this post is no good for this part of this torrent, or "" when unknown."""
        name = matching.norm(rel.title)
        for v in self._load().get(infohash, {}).get(label, []):
            if v.get("guid") and v["guid"] == rel.guid:
                return v.get("why", "")
            if name and v.get("name") == name and rel.size and v.get("size") == rel.size:
                return v.get("why", "")
        return ""

    def mark(self, infohash: str, label: str, rel: Release, why: str):
        with _verdict_lock:
            d = self._load()
            rows = d.setdefault(infohash, {}).setdefault(label, [])
            if not any(r.get("guid") == rel.guid for r in rows):
                rows.append({"guid": rel.guid, "name": matching.norm(rel.title), "size": rel.size,
                             "title": rel.title, "indexer": rel.indexer, "why": why,
                             "at": time.time()})
            tmp = f"{self.path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(d, fh, indent=1)
            os.replace(tmp, self.path)

    def forget(self, infohash: str) -> int:
        """Drop what is known about one torrent (e.g. to have everything tried again)."""
        with _verdict_lock:
            d = self._load()
            n = sum(len(v) for v in d.pop(infohash, {}).values())
            tmp = f"{self.path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(d, fh, indent=1)
            os.replace(tmp, self.path)
            return n


_verdict_lock = threading.Lock()


def verdicts_for(cfg: Config) -> Verdicts:
    return Verdicts(os.path.join(os.path.dirname(os.path.abspath(cfg.path)), "verdicts.json"))


def ledger_for(cfg: Config) -> Ledger:
    return Ledger(os.path.join(os.path.dirname(os.path.abspath(cfg.path)), "sab_jobs.json"))


# What the build running in this thread is building. Per thread, since builds run side by
# side: shared, each build's downloads were recorded under whichever torrent started last.
#   torrent - its infohash, recorded with each SABnzbd job it submits
#   tracker - where it came from, for "is a few percent allowed there?"
#   seeders - how many Prowlarr reported for it
_this_build = threading.local()


def _torrent_now() -> str:
    return getattr(_this_build, "torrent", "")


def _tracker_now() -> str:
    return getattr(_this_build, "tracker", "")


def sab_submit(cfg: Config, pr, sab: SABnzbd, rel: Release, data: bytes | None, pp: int) -> str:
    """Start downloading a post. With a grab.Source, the paused SABnzbd job that fetched
    its NZB is resumed (nzb2seed never downloads an NZB from an indexer itself)."""
    # a download writes to staging, so it is staging that has to have room
    space.wait_for_room(step, progress, role="downloads")
    nzb = data if data is not None else pr.fetch(rel)
    if b"<nzb" not in nzb[:4096].lower():
        raise ApiError(f"{rel.indexer} did not return an NZB for {rel.title}")
    try:
        ids = nzbinfo.post_ids(nzb)
    except ET.ParseError:
        ids = []
    if hasattr(pr, "start"):
        nzo = pr.start(rel, pp)
    else:
        nzo = sab.add_nzb(nzb, rel.title, cfg.sab_category, pp, cfg.sab_priority)
    ledger_for(cfg).record(nzo, rel.title, rel.guid, rel.indexer, rel.size, _torrent_now(), ids)
    mode = sab.ensure_pp(nzo, pp)
    info(f"{nzo}  {mode:<16} {rel.title}  ({rel.indexer}, {gb(rel.size)})")
    return nzo


PROBE_DIR = os.path.join(os.sep, "nonexistent-nzb2seed-probe")


def supplies(t: Torrent, d: str, target: str | None, unpack: bool = True) -> bool:
    """Does folder ``d`` hold ``target`` (a torrent file name), or - with target None - every
    file of the torrent? Checked by matching; if files are missing but the folder holds RAR
    archives that contain them, those files are unpacked into the folder first."""
    res = asm.assemble(t, [d], PROBE_DIR, dry_run=True, log=lambda *_: None)
    wanted = [f for f in res.missing if target is None or f.name == target]
    ok = not res.missing if target is None else any(os.path.basename(rel) == target for rel in res.placed)
    if ok or not unpack:
        return ok
    if unpack_from_archives(d, wanted):
        return supplies(t, d, target, unpack=False)
    return False


_listings: dict[tuple, list] = {}


def _contents(first: str) -> tuple[list, bool]:
    """(entries, first time) - an archive is listed once per build, and reported once."""
    st = os.stat(first)
    key = (os.path.abspath(first), st.st_mtime, st.st_size)
    if key in _listings:
        return _listings[key], False
    _listings[key] = archives.list_contents(first)
    return _listings[key], True


def _merge_nzb(a: bytes, b: bytes | None) -> bytes:
    """One NZB with the files of two trimmed NZBs of the same post."""
    if b is None:
        return a
    ra, rb = ET.fromstring(a), ET.fromstring(b)
    ns = ra.tag[:ra.tag.index("}") + 1] if ra.tag.startswith("{") else ""
    seen = {f.get("subject") for f in ra if f.tag == f"{ns}file"}
    for f in rb:
        if f.tag == f"{ns}file" and f.get("subject") not in seen:
            ra.append(f)
    return b'<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(ra, encoding="utf-8")


def _drop(pr, rel: Release):
    """A post that will not be downloaded: its paused NZB-check job goes."""
    if hasattr(pr, "drop"):
        pr.drop(rel)


def unpack_from_archives(d: str, wanted: list, dest: str | None = None) -> bool:
    """Extract the torrent files in ``wanted`` from archive sets (RAR, 7z, zip, numbered) in
    download folder ``d`` (into
    ``d``'s unpack folder, or ``dest``).
    A file is recognised by its exact size (and, for small files, its name too - posters
    often obfuscate the names of big ones). Returns True when anything was unpacked."""
    sets = archives.archive_sets(d)
    if not sets or not wanted:
        return False
    extracted = False
    for first in sets:
        name = os.path.basename(first)
        try:
            entries, fresh = _contents(first)
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            if not re.search(r"\.\d{3}$", name):      # numbered pieces may be a plain split file
                warn(f"cannot look inside {name}: {e}")
            continue
        members = []
        for f in wanted:
            for path, size in entries:
                same_name = os.path.basename(path.replace("\\", "/")).casefold() == f.name.casefold()
                if size == f.length and (same_name or f.length >= 1 << 20) and path not in members:
                    members.append(path)
                    break
        if not members:
            if fresh:
                # say what was wanted and what is actually in there: a size that is close
                # but not equal is the usual sign of a different encode of the same title
                want = ", ".join(f"{f.name} ({gb(f.length)})" for f in wanted[:3])
                more = f" and {len(wanted) - 3} more" if len(wanted) > 3 else ""
                held = ", ".join(f"{os.path.basename(p.replace(chr(92), '/'))} ({gb(s)})"
                                 for p, s in entries[:3])
                held_more = f" and {len(entries) - 3} more" if len(entries) > 3 else ""
                info(f"{name}: does not hold {want}{more}; it holds {held or 'nothing'}{held_more}")
            continue
        info(f"{name} holds {len(members)} needed file(s): " + ", ".join(os.path.basename(m) for m in members[:3])
             + " - unpacking into the download folder")
        try:
            archives.extract(first, members, dest or os.path.join(d, archives.UNPACK_DIR))
            extracted = True
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            warn(f"unpacking {name} failed: {e}")
    return extracted


def failed_before(cfg: Config, sab: SABnzbd, rel: Release) -> bool:
    """Did an earlier nzb2seed download of this same NZB fail in SABnzbd? Then it would
    fail again (the articles are gone), so it is skipped."""
    for j in ledger_for(cfg).matches(rel.title, rel.guid, rel.size):
        try:
            if sab.status(j["nzo"])[0] in SAB_FAILED:
                info(f"{rel.title} ({rel.indexer}): failed in SABnzbd before ({j['nzo']}) - skipping it")
                return True
        except ApiError:
            pass
    return False


def earlier_post(cfg: Config, sab: SABnzbd, rel: Release, nzb: bytes):
    """An earlier download that held every file of this post (same Usenet articles - the
    same post on another indexer, or one listing fewer of its files):
    (state, nzo, folder, why) with state "downloading", "done" or "failed"; None when
    this post has something no earlier download had."""
    try:
        ids = nzbinfo.post_ids(nzb)
    except ET.ParseError:
        return None
    for j in ledger_for(cfg).covering(ids):
        try:
            status, slot = sab.status(j["nzo"])
        except ApiError:
            continue
        why = (f"{rel.title} ({rel.indexer}) has no file that {j.get('title')} "
               f"({j.get('indexer') or 'an earlier download'}, {j['nzo']}) did not have")
        if status in SAB_FAILED:
            return "failed", j["nzo"], None, why
        if status.startswith("Queued:") or status not in SAB_DONE | {"Unknown"}:
            return "downloading", j["nzo"], None, why
        if status in SAB_DONE:
            try:
                return "done", j["nzo"], job_dir(cfg, slot, j.get("title", "")), why
            except Abort:
                continue                  # its files are gone: this post is worth downloading
    return None


def reuse(cfg: Config, sab: SABnzbd, rel: Release, ok) -> tuple[str, str | None] | None:
    """An earlier nzb2seed download of the same NZB that can be used instead of downloading
    it again: (nzo, folder) when finished and ``ok(folder)``, (nzo, None) while it is still
    downloading. None when there is nothing to reuse."""
    for j in ledger_for(cfg).matches(rel.title, rel.guid, rel.size):
        try:
            status, slot = sab.status(j["nzo"])
        except ApiError:
            continue
        if status.startswith("Queued:") or status not in SAB_DONE | SAB_FAILED | {"Unknown"}:
            info(f"{rel.title}: already downloading as {j['nzo']} - waiting for that instead")
            return j["nzo"], None
        if status not in SAB_DONE:
            continue
        try:
            d = job_dir(cfg, slot, j["title"])
        except Abort:
            continue                      # its files have been moved away since
        if ok(d):
            info(f"{rel.title}: reusing the finished download {j['nzo']} in {d}")
            return j["nzo"], d
    return None


PEEK_PREFIX = "nzb2seed-peek-"
PEEK_QUIET = 600       # seconds a finished peek is left before a sweep may remove it

# the peek jobs this process is reading right now - never swept up from under a build
_peeking: set[str] = set()


def archive_holds(files: list, entries: list[tuple[str, int]], inner=()) -> bool | None:
    """Judging from the file list inside a RAR set, does it hold ``files`` (torrent files)?

    True when it holds a file of exactly the right size - one of the torrent's, or one of
    ``inner``: the sizes of what the torrent's own scene RARs hold (from srrDB), since a
    re-pack of that video is what the scene RARs are rebuilt from. False when it holds the
    right name at a different size or a file as large as the one wanted that is not it (a
    different encode of the same title), None when the listing cannot say."""
    if not entries or not files:
        return None
    want_names = {os.path.basename(f.name).lower() for f in files}
    want_sizes = {f.length for f in files} | set(inner)
    if any(size in want_sizes for _, size in entries):
        return True
    if any(os.path.basename(n).lower() in want_names for n, _ in entries):
        return False
    if max(size for _, size in entries) >= max(want_sizes):
        return False
    return None            # only small files listed so far: the rest is in later volumes


def worth_peeking(nzb: bytes, files: list) -> bool:
    """Is a post worth looking inside? Only when it is a RAR set that names none of the
    torrent's files - then the NZB alone cannot say whether it is the same release."""
    try:
        posted = nzbinfo.parse(nzb)
    except ET.ParseError:
        return False
    if not nzbinfo.first_volume(posted):
        return False
    names = {os.path.basename(f.name).lower() for f in files}
    return not any(p.name.lower() in names for p in posted)


def peek_archive(cfg: Config, pr, sab: SABnzbd, rel: Release, nzb: bytes, files: list,
                 inner=()) -> bool | None:
    """Look inside a RAR post before downloading all of it: only its first volume is
    fetched, and the names and sizes in that volume's headers are compared with the torrent's
    files. False means the post holds a different encode - skip it and keep the bytes."""
    try:
        vol = nzbinfo.first_volume(nzbinfo.parse(nzb))
    except ET.ParseError:
        return None
    # only the first articles of the first volume: the headers are at its very start
    part = nzbinfo.trim_head(nzb, vol) if vol else None
    if part is None:
        return None
    name = PEEK_PREFIX + secrets.token_hex(4)
    try:
        # no post-processing: a volume on its own can never pass a repair, and "failed"
        # was making the peek throw away a download it had in hand
        nzo = sab.add_nzb(part, name, cfg.sab_category, PP_NONE, cfg.sab_priority)
    except ApiError as e:
        warn(f"could not look inside {rel.title}: {e}")
        return None
    _peeking.add(name)
    info(f"looking inside {rel.title} ({rel.indexer}, {gb(rel.size)}): fetching the start "
         f"of {vol} only (about {nzbinfo.PEEK_SEGMENTS * 0.7:.1f} MB)")
    d = None
    try:
        status, slot = sab_wait(sab, {nzo: f"first volume of {rel.title}"})[nzo]
        # read what arrived whatever SABnzbd calls the job: with nothing to post-process
        # it may still say "failed" about a volume that is sitting there complete
        storage = slot.get("storage") or ""
        if not storage:
            return None
        d = map_path(storage, cfg.sab_to_local)
        if os.path.isfile(d):
            d = os.path.dirname(d)
        if not os.path.isdir(d):
            return None
        got = [os.path.join(d, f) for f in os.listdir(d)]
        got = [p for p in got if os.path.isfile(p)]
        if not got:
            return None
        entries = archives.list_contents(max(got, key=os.path.getsize))
        held = archive_holds(files, entries, inner)
        want = ", ".join(f"{f.name} ({gb(f.length)})" for f in files[:3])
        inside = ", ".join(f"{os.path.basename(p.replace(chr(92), '/'))} ({gb(s)})"
                           for p, s in sorted(entries, key=lambda e: -e[1])[:3])
        if held is False:
            info(f"{rel.title}: looking for {want}; the archive holds {inside} - "
                 f"a different release, skipping it, {gb(rel.size)} not downloaded")
        elif held:
            info(f"{rel.title}: the archive holds {inside}, which is what the torrent "
                 f"needs - downloading it")
        return held
    except (Abort, ApiError, OSError, RuntimeError, subprocess.SubprocessError) as e:
        warn(f"could not look inside {rel.title}: {e}")
        return None
    finally:
        _peeking.discard(name)
        if d and os.path.isdir(d) and os.path.basename(d).startswith(PEEK_PREFIX):
            shutil.rmtree(d, ignore_errors=True)
        for how in ("queue", "history"):                   # wherever SABnzbd has it now
            try:
                if how == "queue":
                    sab.queue_do("delete", nzo)
                else:
                    sab.delete_history(nzo, files=True)
            except (ApiError, TypeError):
                pass


def remove_stray_peeks(cfg: Config, sab: SABnzbd) -> tuple[int, int]:
    """Remove what finished or abandoned peeks left behind: (jobs, bytes freed).

    A peek's verdict is taken the moment it is read, so once it is not being read any
    more everything of it is spare - whether or not its season has finished. Only what
    nzb2seed named, only what no build is reading now, and only once it has been quiet
    for a while: a peek that finished seconds ago may still be being read by a build in
    another process."""
    jobs, freed = 0, 0
    now = time.time()
    try:
        queue = sab._call("queue", limit="0").get("queue", {}).get("slots", [])
        for q in queue:
            name = str(q.get("filename", ""))
            # a peek still queued with nobody reading it is one a killed build started
            if name.startswith(PEEK_PREFIX) and name not in _peeking and \
                    str(q.get("status", "")) == "Paused":
                sab.queue_do("delete", q["nzo_id"])
                jobs += 1
        hist = sab._call("history", limit="1000").get("history", {}).get("slots", [])
        for h in hist:
            name = str(h.get("name", ""))
            if name.startswith(PEEK_PREFIX) and name not in _peeking \
                    and now - float(h.get("completed") or 0) > PEEK_QUIET:
                sab.delete_history(h["nzo_id"], files=True)
                jobs += 1
    except ApiError:
        pass
    try:                                    # a check still in the queue is being read
        queued_names = {str(q.get("filename", "")) for q in
                        sab._call("queue", limit="0").get("queue", {}).get("slots", [])}
    except ApiError:
        return jobs, freed                  # cannot tell what is in use: leave the folders
    roots = []
    for where in (sab.incomplete_dir(), (sab.config().get("misc") or {}).get("complete_dir")):
        if where:
            roots.append(map_path(where, cfg.sab_to_local))
    for root in roots:
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for n in names:
            p = os.path.join(root, n)
            if not n.startswith((PEEK_PREFIX, grab.CHECK_PREFIX)) or n in _peeking \
                    or n in queued_names or not os.path.isdir(p):
                continue
            try:
                if now - os.path.getmtime(p) < PEEK_QUIET:
                    continue
                freed += sum(os.path.getsize(os.path.join(dp, f))
                             for dp, _, fs in os.walk(p) for f in fs)
            except OSError:
                continue
            shutil.rmtree(p, ignore_errors=True)
    return jobs, freed


def remove_download(cfg: Config, sab: SABnzbd, nzo: str) -> bool:
    """Delete one of nzb2seed's SABnzbd downloads, files and all. SABnzbd's own del_files
    only deletes a *failed* job's files, so a finished job's folder is removed here first
    and its history entry after. A job still queued is deleted from the queue (SABnzbd does
    remove those files). Never SABnzbd's own complete or incomplete folder."""
    slot = sab.history_slot(nzo)
    if slot is None:
        if sab.queue_slot(nzo) is not None:
            sab.queue_do("delete", nzo)
            return True
        return False
    where = map_path(slot.get("storage") or slot.get("path") or "", cfg.sab_to_local)
    if where and os.path.isfile(where):
        where = os.path.dirname(where)
    roots = {os.path.normpath(map_path(r, cfg.sab_to_local)) for r in (
        sab.incomplete_dir(), (sab.config().get("misc") or {}).get("complete_dir")) if r}
    if where and os.path.isdir(where) and os.path.normpath(where) not in roots:
        shutil.rmtree(where)
    sab.delete_history(nzo, files=False)
    return True


def delete_downloads_of(cfg: Config, sab: SABnzbd, qb, hashes: set[str]) -> dict:
    """Delete the Usenet downloads nzb2seed made for these torrents - part downloads still
    in SABnzbd's queue and finished ones - as long as nothing depends on them: never a
    folder a torrent in qBittorrent uses, one a running build has claimed, or one shared
    with a download of anything else. {"deleted", "bytes", "kept": [why, ...]}.
    Nothing is deleted when qBittorrent cannot be asked, since then nobody can tell."""
    if qb is None:
        raise Abort("qBittorrent is not set up, so it cannot be told which downloads a torrent uses - nothing deleted")
    torrents = qb.torrents()
    back = [(b, a) for a, b in (cfg.local_to_qbit or [])]
    busy = [os.path.normpath(map_path(t["content_path"], back)) for t in torrents if t.get("content_path")]
    busy += list(claimed())

    def in_use(p: str) -> bool:
        p = os.path.normpath(p)
        return any(b == p or b.startswith(p + os.sep) or p.startswith(b + os.sep) for b in busy)

    ours = {e.get("nzo"): e for e in ledger_for(cfg).all()}
    mine = {z for z, e in ours.items() if e.get("torrent") in hashes}
    if not mine:
        return {"deleted": 0, "bytes": 0, "kept": []}
    slots = sab._call("history", limit="1000000").get("history", {}).get("slots", [])   # 0 means 10
    folders: dict[str, list[str]] = {}
    for slot in slots:
        where = map_path(slot.get("storage") or slot.get("path") or "", cfg.sab_to_local)
        if where and os.path.isfile(where):
            where = os.path.dirname(where)
        if where:
            folders.setdefault(os.path.normpath(where), []).append(slot.get("nzo_id"))
    roots = {os.path.normpath(map_path(r, cfg.sab_to_local)) for r in (
        sab.incomplete_dir(), (sab.config().get("misc") or {}).get("complete_dir")) if r}
    deleted = freed = 0
    kept: list[str] = []
    done_nzos: set[str] = set()
    for where, nzos in folders.items():
        if not set(nzos) & mine:
            continue
        name = os.path.basename(where)
        if where in roots or not os.path.isdir(where):
            continue
        if not set(nzos) <= mine:
            kept.append(f"{name}: shared with another download")
            continue
        if in_use(where):
            kept.append(f"{name}: a torrent in qBittorrent (or a running build) uses it")
            continue
        try:
            size = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(where) for f in fs)
            shutil.rmtree(where)
            for z in nzos:
                sab.delete_history(z, files=False)
            done_nzos.update(nzos)
            deleted += 1
            freed += size
        except (ApiError, OSError) as err:
            kept.append(f"{name}: {err}")
    # part downloads still in the queue (SABnzbd deletes their files itself)
    for z in mine - done_nzos:
        try:
            if sab.queue_slot(z) is not None:
                sab.queue_do("delete", z)
                deleted += 1
        except ApiError as err:
            kept.append(f"{ours[z].get('title') or z}: {err}")
    return {"deleted": deleted, "bytes": freed, "kept": kept}


QUIET = 3600          # a download folder is left alone until nothing has touched it for this long
_SEASON = re.compile(r"(?<![a-z0-9])s(\d{1,4})(?:e\d{1,4})?(?![0-9])")
_YEAR = re.compile(r"(?<![0-9])(19|20)\d{2}(?![0-9])")


def release_key(name: str) -> str:
    """What release a name is of, loosely: "show|s1" for any episode, season or
    pack name of that season, "film|2020" for a film - whatever the group, resolution or
    punctuation. For matching an old download record to the builds that could use it."""
    n = re.sub(r"[()\[\]{}]", "", matching.norm(name)).replace("..", ".")
    m = _SEASON.search(n)
    if m:
        show = _YEAR.sub("", n[:m.start()]).strip(".-")
        return f"{show}|s{int(m.group(1))}"
    y = _YEAR.search(n)
    if y and y.start() > 0:
        return f"{n[:y.start()].strip('.-')}|{y.group(0)}"
    return ".".join(n.split(".")[:4])


def clean_unused_downloads(cfg: Config, sab: SABnzbd, qb, keep: set[str], live: set[str],
                           names: set[str], log=print, dry_run: bool = False,
                           now: float | None = None) -> tuple[int, int]:
    """Delete nzb2seed's Usenet downloads that nothing can use any more: (folders, bytes).

    A folder goes only when every SABnzbd entry pointing at it is nzb2seed's, and none of
    them is for a torrent in ``live`` (being built, or queued to be) or ``keep`` (a job on
    the list could still use it: try again, add to the torrent client, build from another
    tracker); no torrent in qBittorrent uses the folder or holds one of those torrents
    unfinished; no running build has claimed it; and nothing has touched it for QUIET
    seconds. Records from before each build filed its own downloads may name the wrong
    torrent, so for those a download of the same release (same show and season, or film
    and year - see release_key) as one of those builds, jobs or torrents (``names``) is kept
    too. Anything SABnzbd is still downloading is never touched."""
    torrents = qb.torrents() if qb is not None else []
    if qb is None:
        return 0, 0                          # cannot tell what qBittorrent uses: nothing goes
    unfinished = {t["hash"] for t in torrents if (t.get("progress") or 0) < 1}
    names = set(names) | {release_key(t.get("name") or "") for t in torrents if (t.get("progress") or 0) < 1}
    back = [(b, a) for a, b in (cfg.local_to_qbit or [])]
    busy = [os.path.normpath(map_path(t["content_path"], back)) for t in torrents if t.get("content_path")]
    busy += list(claimed())

    def in_use(p: str) -> bool:
        p = os.path.normpath(p)
        return any(b == p or b.startswith(p + os.sep) or p.startswith(b + os.sep) for b in busy)

    ours = {e.get("nzo"): e for e in ledger_for(cfg).all()}
    try:
        slots = sab._call("history", limit="1000000").get("history", {}).get("slots", [])   # 0 means 10
    except ApiError as err:
        log(f"not clearing downloads: SABnzbd's history could not be read ({err})")
        return 0, 0
    roots = {os.path.normpath(map_path(r, cfg.sab_to_local)) for r in (
        sab.incomplete_dir(), (sab.config().get("misc") or {}).get("complete_dir")) if r}
    folders: dict[str, list[dict]] = {}
    for slot in slots:
        where = map_path(slot.get("storage") or slot.get("path") or "", cfg.sab_to_local)
        if not where or not os.path.exists(where):
            continue
        if os.path.isfile(where):
            where = os.path.dirname(where)
        folders.setdefault(os.path.normpath(where), []).append(slot)
    now = time.time() if now is None else now
    wanted = keep | live | unfinished

    def needed(e: dict) -> bool:
        if not e.get("torrent") or e.get("torrent") in wanted:
            return True                        # no torrent (a season grab's): not ours to judge
        # an old record may name another build's torrent: one of a release still wanted stays
        return not e.get("own") and release_key(e.get("title") or "") in names

    n = freed = 0
    for where, slots_here in folders.items():
        entries = [ours.get(s.get("nzo_id")) for s in slots_here]
        if where in roots or not all(entries) or any(needed(e) for e in entries) or in_use(where):
            continue
        try:
            touched = max([os.path.getmtime(where)] + [float(s.get("completed") or 0) for s in slots_here])
            if now - touched < QUIET:
                continue                       # something may still be working with it
            size = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(where) for f in fs)
            if not dry_run:
                shutil.rmtree(where)
                for s in slots_here:
                    sab.delete_history(s.get("nzo_id"), files=False)
            n += 1
            freed += size
        except (ApiError, OSError) as err:
            log(f"could not clear {os.path.basename(where)}: {err}")
    if n:
        log(f"{'would clear' if dry_run else 'cleared'} {n} Usenet download folder(s) nothing can use any more ({gb(freed)})")
    return n, freed


def clean_seeded_downloads(cfg: Config, sab: SABnzbd, qb, log=print, dry_run: bool = False) -> tuple[int, int]:
    """Delete nzb2seed's Usenet downloads whose torrent is complete in qBittorrent:
    (downloads, bytes). No build needs the files of a torrent that is already done; one
    whose torrent is not complete is kept, as a retry may need it. Never touched: a folder
    any torrent in qBittorrent is using, and one a running build has claimed (it may be
    reusing it for another torrent with the same files)."""
    torrents = qb.torrents() if qb is not None else []
    if not torrents:
        return 0, 0                          # cannot see qBittorrent: nothing is known done
    done = {t["hash"] for t in torrents if (t.get("progress") or 0) >= 1}
    back = [(b, a) for a, b in (cfg.local_to_qbit or [])]
    busy = [os.path.normpath(map_path(t["content_path"], back)) for t in torrents if t.get("content_path")]
    busy += list(claimed())

    def in_use(p: str) -> bool:
        p = os.path.normpath(p)
        return any(b == p or b.startswith(p + os.sep) or p.startswith(b + os.sep) for b in busy)

    # every SABnzbd history entry, by the folder it left: a folder is only cleared when every
    # entry pointing at it is nzb2seed's, for a torrent that is complete - one shared with a
    # download a retry may still need (or with anything not nzb2seed's) is kept
    ours = {e.get("nzo"): e.get("torrent") for e in ledger_for(cfg).all()}
    try:
        slots = sab._call("history", limit="1000000").get("history", {}).get("slots", [])   # 0 means 10
    except ApiError as err:
        log(f"not clearing downloads: SABnzbd's history could not be read ({err})")
        return 0, 0
    # SABnzbd's own folders themselves are never cleared, only job folders inside them
    roots = {os.path.normpath(map_path(r, cfg.sab_to_local)) for r in (
        sab.incomplete_dir(), (sab.config().get("misc") or {}).get("complete_dir")) if r}
    folders: dict[str, list[str]] = {}
    for slot in slots:
        where = map_path(slot.get("storage") or slot.get("path") or "", cfg.sab_to_local)
        if not where or not os.path.exists(where):
            continue
        if os.path.isfile(where):
            where = os.path.dirname(where)
        folders.setdefault(os.path.normpath(where), []).append(slot.get("nzo_id"))
    n = freed = 0
    for where, nzos in folders.items():
        if not all(ours.get(z) in done for z in nzos):
            continue                         # someone may still need it
        if where in roots or in_use(where):
            continue
        try:
            size = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(where) for f in fs)
            if not dry_run:
                # SABnzbd's del_files only deletes the files of *failed* jobs: a completed
                # job's folder is removed here, and only then its history entry - so a
                # folder that could not be removed keeps the entry a later run finds it by
                shutil.rmtree(where)
                for z in nzos:
                    sab.delete_history(z, files=False)
            n += 1
            freed += size
        except (ApiError, OSError) as err:
            log(f"could not clear {os.path.basename(where)}: {err}")
    if n:
        log(f"{'would clear' if dry_run else 'cleared'} {n} Usenet download folder(s) of torrents "
            f"that are complete in qBittorrent ({gb(freed)})")
    return n, freed


def sab_wait(sab: SABnzbd, nzos: dict[str, str]) -> dict[str, tuple[str, dict]]:
    """Wait until every job has finished; return {nzo: (status, slot)}."""
    out: dict[str, tuple[str, dict]] = {}
    no_path_since: dict[str, float] = {}
    said = ""                     # the disk-space reason this job has already been told
    while len(out) < len(nzos):
        check_cancel()
        parts = []
        for nzo, title in nzos.items():
            if nzo in out:
                continue
            status, slot = sab.status(nzo)
            if status in SAB_DONE and not slot.get("storage") \
                    and time.monotonic() - no_path_since.setdefault(nzo, time.monotonic()) < 120:
                # SABnzbd can mark a job Completed a moment before it records where the files went
                parts.append("finishing")
                continue
            if status in SAB_DONE or status in SAB_FAILED:
                out[nzo] = (status, slot)
                msg = f"{status.lower()}: {title}"
                if slot.get("fail_message"):
                    msg += f" - {slot.get('fail_message')}"
                (info if status in SAB_DONE else warn)(msg)
            elif status.startswith("Queued:"):
                parts.append(f"{slot.get('percentage', '?')}% {slot.get('timeleft', '')}".strip())
            else:
                parts.append(status)
        if parts:
            # a job that cannot move because the disk guard paused the queue looks like a
            # stalled download; say which it is, in the log as well as the progress line,
            # because the progress line is not kept
            held = ""
            if space.GATE:
                try:
                    held = space.GATE("downloads") or ""     # it is waiting on a download
                except Exception:
                    held = ""
            if held and held != said:
                warn(f"waiting for disk space - {held}")
                said = held
            elif said and not held:
                info("there is room again - the queue is running")
                said = ""
            progress(f"{len(out)}/{len(nzos)} done  " + "  ".join(parts)
                     + (f"  [waiting for disk space: {held}]" if held else ""))
        if len(out) < len(nzos):
            time.sleep(5)
    end_progress()
    return out


_claims: dict[int, set[str]] = {}          # thread -> download folders it is using
_claims_lock = threading.Lock()


def claim(d: str) -> str:
    """Note that the build running in this thread uses download folder ``d`` - it may be
    an earlier download of another torrent - so the cleanup leaves it alone meanwhile."""
    with _claims_lock:
        _claims.setdefault(threading.get_ident(), set()).add(os.path.normpath(d))
    return d


def claimed() -> set[str]:
    """Folders claimed by builds still running (a finished build's thread is gone, and so
    are its claims)."""
    alive = {t.ident for t in threading.enumerate()}
    with _claims_lock:
        for k in [k for k in _claims if k not in alive]:
            del _claims[k]
        return set().union(*_claims.values()) if _claims else set()


def job_dir(cfg: Config, slot: dict, title: str) -> str:
    return claim(_job_dir(cfg, slot, title))


def _job_dir(cfg: Config, slot: dict, title: str) -> str:
    storage = slot.get("storage") or ""
    local = map_path(storage, cfg.sab_to_local)
    if not os.path.exists(local):
        raise Abort(f"SABnzbd says the files are in {storage!r}, which maps to {local!r} here "
                    "but that does not exist - check the SABnzbd path translation in settings")
    if os.path.isfile(local):
        # SABnzbd reports the main file for some jobs; use its job folder, but only when the
        # folder really is this job's (never SABnzbd's shared complete folder)
        parent = os.path.dirname(local)
        if matching.norm(os.path.basename(parent)).startswith(matching.norm(title)):
            return parent
    return local


def missing_from(t: Torrent, d: str, output_dir: str) -> list:
    """Torrent files a finished job cannot supply (checked without touching anything)."""
    return asm.assemble(t, [d], output_dir, dry_run=True, log=lambda *_: None).missing


_EP = re.compile(r"(?<![a-z0-9])s\d{1,4}e\d{1,4}(?:-?e\d{1,4})*(?![0-9])")
_RES = re.compile(r"(?<![a-z0-9])(?:480|576|720|1080|2160)p(?![a-z0-9])")
MAX_INCOMPLETE = 3   # completed posts that turned out to lack files, before asking


# dash-joined tags that are not release groups ('WEB-DL', 'Blu-ray', 'DTS-HD MA'...)
_NOT_GROUP = {"dl", "rip", "ray", "hd", "ma", "es", "x", "hr", "lq", "dvd", "tv", "tc", "ts"}


def group_of(name: str) -> str | None:
    """Release group: the first real dash-token after the episode/resolution part, where a
    scene dash has no spaces around it and tech tags like WEB-DL are skipped.
    'Show.S03E02.720p.WEB-DL.AAC2.0.H.264-GRP-xpost' -> 'grp', '...-grp.mkv' -> 'grp',
    'Show S03 - Heat B' -> None."""
    s = name.strip()
    low = s.lower()
    # "Show (2022) S02E01 (1080p ATVP WEB-DL H265 ... English - GRPH).mkv": the group closes
    # the bracketed description at the end
    # (also with dots for spaces, and a poster's "-xpost" after it)
    m = re.search(r"[\s._]-[\s._]([A-Za-z0-9]{2,20})\)\s*(?:\.[A-Za-z0-9]{2,4})?(?:-x?(?:post|repo))?\s*$", s, re.I)
    if m and m.group(1).lower() not in _NOT_GROUP:
        return m.group(1).lower()
    m_res, m_ep = _RES.search(matching.norm(s)), _EP.search(matching.norm(s))
    # find the anchor in the raw name (norm only swaps separators, so positions line up)
    anchor = 0
    for m in (m_res, m_ep):
        if m:
            k = low.replace(" ", ".").replace("_", ".").find(m.group(0))
            anchor = max(anchor, k + len(m.group(0)) if k >= 0 else 0)
    for dm in re.finditer(r"-", s[anchor:]):
        i = anchor + dm.start()
        if i == 0 or s[i - 1].isspace() or i + 1 >= len(s) or s[i + 1].isspace() or \
                (s[i - 1] in "._" and s[i + 1] in "._"):
            continue                                     # ' - Heat B' (or '.-.'): a description, not a group
        tok = re.split(r"[-.\s\[\]()]", s[i + 1:].lower())[0]
        if tok and tok not in _NOT_GROUP:
            return tok
    return None


@dataclass
class Need:
    """What one Usenet download has to supply."""
    label: str                      # e.g. "S03E02" or the torrent name
    query: str                      # Prowlarr search text for alternatives
    group: str | None               # release group the alternative must come from
    res: str | None                 # resolution, if the name has one
    ep: str | None                  # episode token, e.g. "s03e02"
    min_size: int                   # an NZB smaller than this cannot hold the file(s)
    target: str                     # the torrent file (or torrent) it must supply
    results: list | None = None     # every Usenet result seen for this need
    level: str = "episode"          # "whole" | "season" | "episode": what kind of NZB fits
    season: int | None = None
    show: str = ""                  # normalised title prefix every fitting NZB starts with
    seasons: list = dataclasses.field(default_factory=list)   # for a multi-season whole


_FILE_EXT = re.compile(r"\.(mkv|mp4|m4v|avi|m2ts|ts|wmv|mov|iso|mpg|mpeg|webm)$", re.I)


def release_title(name: str) -> str:
    """A single-file torrent is named after its file ("...-GRP.mkv"); the release - and every
    Usenet post of it - is named without the extension."""
    return _FILE_EXT.sub("", (name or "").strip())


def need_for(t: Torrent, title: str, whole_torrent: bool) -> Need:
    """``whole_torrent``: one post must supply every file (single release); otherwise the
    post supplies the torrent file for ``title``'s episode (season pack from episodes)."""
    title = release_title(title)
    n = matching.norm(title)
    ep = _EP.search(n)
    ep_tok = ep.group(0) if ep else None
    target_file = None
    if ep_tok and not whole_torrent:
        files = [f for f in t.real_files if re.search(rf"(?<![a-z0-9]){ep_tok}(?![0-9])", matching.norm(f.name))]
        target_file = max(files, key=lambda f: f.length) if files else None
    if whole_torrent or target_file is None:
        main = max(t.real_files, key=lambda f: f.length)
        min_size, target = t.total_size, t.name
        group = group_of(main.name) or group_of(t.name) or group_of(title)
    else:
        min_size, target = target_file.length, target_file.name
        group = group_of(target_file.name) or group_of(title)
    res = _RES.search(n)
    if ep:
        query = n[:ep.end()]
    else:
        query = n[:n.find("-", res.end() if res else 0)] if "-" in n else n
    return Need(label=ep_tok.upper() if ep_tok else release_title(t.name), query=query.replace(".", " ").strip(),
                group=group, res=res.group(0) if res else None, ep=ep_tok,
                min_size=min_size, target=target)


def _fits(need: Need, r: Release) -> str | None:
    """Why ``r`` cannot be the alternative (None = it can)."""
    n = matching.norm(r.title)
    if need.show and not n.startswith(need.show):
        return "other title"
    if need.ep and not re.search(rf"(?<![a-z0-9]){need.ep}(?![0-9])", n):
        return "other episode"
    if not need.ep and _EP.search(n):
        return "single episode"
    if need.level == "season" and need.season is not None and \
            not re.search(rf"(?<![a-z0-9])s0*{need.season}(?![0-9e])", n):
        return "other season"
    if need.level == "whole" and len(need.seasons) > 1:
        tokens = set(re.findall(r"(?<![a-z0-9])s(\d{1,4})(?![0-9e])", n))
        if len(tokens) == 1 and not re.search(r"s\d{1,4}\.?-\.?s?\d{1,4}", n):
            return "single season"
    if need.res and need.res not in _RES.findall(n):
        return "other resolution" if _RES.findall(n) else "no resolution in its name"
    if need.group and group_of(r.title) != need.group:
        return "other release group" if group_of(r.title) else "no release group in its name"
    if r.size < need.min_size:
        return "smaller than the torrent's file - cannot hold it"
    return None


def usenet_results(cfg: Config, pr: Prowlarr, need: Need, known: list[Release]) -> list[Release]:
    """All Usenet results for the need's search, plus the ones already known; cached."""
    if need.results is None:
        found = [r for r in pr.search(need.query, cfg.indexer_ids, cfg.categories) if r.protocol == "usenet"]
        seen, out = set(), []
        for r in list(known) + found:
            if r.guid not in seen:
                seen.add(r.guid)
                out.append(r)
        need.results = out
    return need.results


def find_alternatives(cfg: Config, pr: Prowlarr, need: Need, tried: list[Release],
                      known: list[Release] = ()) -> list[Release]:
    """Other posts of the same release (same group, episode and resolution) that are at
    least as large as the file they must supply. Whether one is the same post as one tried
    already is decided from its NZB when it comes up (earlier_post), not from its size."""
    tried_guids = {r.guid for r in tried}
    step(f"Looking for another {need.group.upper() if need.group else 'matching'} post of {need.label}"
         f" (at least {gb(need.min_size)})")
    out = []
    for r in sorted(usenet_results(cfg, pr, need, known), key=lambda r: -(r.grabs or 0)):
        if r.guid in tried_guids or _fits(need, r):
            continue
        out.append(r)
    for r in out:
        info(f"candidate: {r.title}  ({r.indexer}, {gb(r.size)}, {r.grabs} grabs)")
    if not out:
        warn(f"no other {need.group.upper() if need.group else ''} post of {need.label} is large enough")
    return out


def ask_for_post(cfg: Config, pr: Prowlarr, need: Need, tried: list[Release],
                 known: list[Release] = ()) -> Release | None:
    """Nothing fits automatically: show the search results and let the person pick - but
    not one whose name shows another release group or another resolution than the
    torrent's file: those can never match it byte for byte. A name that shows no group or
    no resolution (an obfuscated post) is offered, since it may be the one. Nothing left:
    stop."""
    tried_guids = {r.guid for r in tried}

    def could_be(r: Release) -> bool:
        group, res = group_of(r.title), _RES.findall(matching.norm(r.title))
        return (not need.group or not group or group == need.group) and             (not need.res or not res or need.res in res)
    results = [r for r in usenet_results(cfg, pr, need, known) if r.guid not in tried_guids and could_be(r)]
    if not results:
        warn(f"no other {need.group.upper() + ' ' if need.group else ''}{need.res + ' ' if need.res else ''}"
             f"posts of {need.label} to choose from")
        return None
    results.sort(key=lambda r: (r.size < need.min_size, -(r.grabs or 0)))
    choices = [{"title": r.title, "indexer": r.indexer, "size": r.size, "size_text": gb(r.size),
                "grabs": r.grabs, "publish_date": r.publish_date, "note": _fits(need, r) or ""} for r in results]
    prompt = (f"No automatic replacement for {need.label}. Pick the {need.group.upper() + ' ' if need.group else ''}"
              f"NZB to download instead - it has to hold {need.target} ({gb(need.min_size)}).")
    idx = report_ask(prompt, choices)
    if idx is None:
        return None
    info(f"you picked: {results[idx].title} ({results[idx].indexer})")
    return results[idx]


def used_before(cfg: Config, need: "Need") -> list[Release]:
    """Posts nzb2seed downloaded earlier for this need (same episode and release group, or the
    same release for a whole torrent), as Releases, so a repair does not fetch them again."""
    out = []
    for j in Ledger(ledger_for(cfg).path)._load():
        title = j.get("title", "")
        n = matching.norm(title)
        if need.ep and not re.search(rf"(?<![a-z0-9]){need.ep}(?![0-9])", n):
            continue
        if need.group and group_of(title) != need.group:
            continue
        out.append(Release(title, "usenet", j.get("indexer", ""), 0, j.get("size") or 0,
                           j.get("guid") or j.get("nzo", ""), "", "", "", 0, None, None))
    return out


def placed_before(cfg: Config, opts: "Options", t: Torrent) -> set[str]:
    """Torrent files an earlier run of nzb2seed already put in place (right size, ours)."""
    out_dir = opts.output_dir or cfg.output_dir
    if not out_dir:
        return set()
    owned = owned_record(cfg, t)
    done = set()
    for f in t.real_files:
        p = asm.target_path(out_dir, f)
        if owned.owns(p) and os.path.isfile(p) and os.path.getsize(p) == f.length:
            done.add(f.relpath)
    return done


class Unit:
    """A part of the torrent that one NZB could supply: the whole torrent, one season of a
    multi-season torrent, or one episode. Units form a tree (whole -> seasons -> episodes);
    a unit is only split into its children when no NZB of its own kind works out."""

    def __init__(self, t: Torrent, title: str, level: str, files: list, show: str,
                 season: int | None = None, ep: str | None = None, seasons: list | None = None):
        self.level = level                      # "whole" | "season" | "episode"
        self.files = files
        self.children: list[Unit] = []
        self.queue: list[Release] = []
        self.seeded: list[Release] = []         # NZBs the person picked for exactly this unit
        self.tried: list[Release] = []
        self.searched = False
        self.done = False
        videos = [f for f in files if f.length >= 1 << 20] or files
        groups = {g for g in (group_of(f.name) for f in videos) if g}
        self.mixed = len(groups) > 1            # e.g. a pack with one episode from another group
        group = groups.pop() if len(groups) == 1 else (None if self.mixed else group_of(title))
        n = matching.norm(title)
        res = _RES.search(n) or next((m for m in (_RES.search(matching.norm(f.name)) for f in videos) if m), None)
        main = max(files, key=lambda f: f.length)
        if level == "episode":
            label, query = ep.upper(), f"{show} {ep}"
        elif level == "season":
            label, query = f"S{season:02d}", f"{show} s{season:02d}"
        else:
            label, query = release_title(t.name), n
        self.need = Need(label=label, query=query.replace(".", " ").strip(), group=group,
                         res=res.group(0) if res else None, ep=ep, min_size=sum(f.length for f in files),
                         target=main.name, level=level, season=season, show=show,
                         seasons=seasons or [])

    # the interface bad-piece repair and next_post() rely on
    @property
    def known(self) -> list[Release]:
        return self.seeded

    @property
    def whole(self) -> bool:
        return self.level == "whole"

    def covers(self, f) -> bool:
        return f.relpath in {x.relpath for x in self.files}

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()

    def satisfied_by(self, t: Torrent, d: str, fill=None) -> bool:
        """Does download folder ``d`` hold every file of this unit? RAR archives in it are
        looked into (and the needed files unpacked), and ``fill(d, missing)`` - srrDB's
        rebuild of a scene release - gets a go, before answering no."""
        want = {f.relpath for f in self.files}
        found = set(asm.find_sources(t, [d])) & want
        if found == want:
            return True
        missing = [f for f in self.files if f.relpath not in found]
        if unpack_from_archives(d, missing):
            found = set(asm.find_sources(t, [d])) & want
            if found == want:
                return True
            missing = [f for f in self.files if f.relpath not in found]
        if fill and fill(d, missing):
            return want <= set(asm.find_sources(t, [d]))
        return False


def _season_episode(t: Torrent, f) -> tuple[int | None, str | None]:
    rel = "/".join(f.parts[1:]) if t.multi_file else f.name
    m = _EP.search(matching.norm(f.name)) or _EP.search(matching.norm(rel))
    if m:
        return int(re.match(r"s(\d+)", m.group(0)).group(1)), m.group(0)
    s = re.search(r"(?<![a-z0-9])(?:s|season\.?)(\d{1,4})(?![0-9e])", matching.norm(rel))
    return (int(s.group(1)) if s else None), None


def show_prefix(title: str) -> str:
    """'Show.2016.S03E02.720p...' -> 'show.2016.'; for a movie, up to the resolution."""
    n = matching.norm(title)
    m = _EP.search(n) or re.search(r"(?<![a-z0-9])s\d{1,4}(?![0-9])", n) or _RES.search(n)
    return n[:m.start()] if m else n.split("-")[0]


def build_units(t: Torrent, title: str) -> Unit:
    title = release_title(title)
    """The tree of units for a torrent: a movie or an episode is one unit; a season pack is a
    season with episodes under it; a multi-season pack is the whole with seasons under it."""
    show = show_prefix(title)
    info_ = {f.relpath: _season_episode(t, f) for f in t.real_files}
    seasons = sorted({s for s, _ in info_.values() if s is not None})
    eps = sorted({e for _, e in info_.values() if e})

    def episodes(files, season):
        out = []
        for e in sorted({info_[f.relpath][1] for f in files if info_[f.relpath][1]}):
            out.append(Unit(t, title, "episode", [f for f in files if info_[f.relpath][1] == e], show, season, e))
        return out

    if len(seasons) > 1:
        whole = Unit(t, title, "whole", list(t.real_files), show, seasons=seasons)
        for s in seasons:
            files = [f for f in t.real_files if info_[f.relpath][0] == s]
            su = Unit(t, title, "season", files, show, s)
            su.children = episodes(files, s)
            whole.children.append(su)
        return whole
    if len(seasons) == 1 and len(eps) > 1:
        whole = Unit(t, title, "season", list(t.real_files), show, seasons[0])
        whole.children = episodes(list(t.real_files), seasons[0])
        return whole
    if len(eps) == 1:
        return Unit(t, title, "episode", list(t.real_files), show, seasons[0], eps[0])
    return Unit(t, title, "whole", list(t.real_files), show)


def _rank(pr: Prowlarr, t: Torrent, unit: Unit, rels: list[Release]) -> list[Release]:
    """Order candidates for a season/whole unit by looking inside their NZBs (enough bytes,
    then how many of the unit's file names appear); episode candidates by popularity."""
    rels = sorted(rels, key=lambda r: -(r.grabs or 0))
    if unit.level == "episode" or len(rels) < 2:
        return rels
    scored, rest = [], []
    for r in rels:
        if len(scored) >= MAX_PEEK:
            rest.append(r)
            continue
        try:
            sc = nzbinfo.score(nzbinfo.parse(pr.fetch(r)), t, unit.files)
        except (ApiError, ET.ParseError):
            rest.append(r)
            continue
        info(f"{r.indexer[:16]:<16} {gb(r.size):>9}  {sc.summary}")
        scored.append((sc.key, r.grabs or 0, r))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [r for _, _, r in scored] + rest


def plan_and_fetch(cfg: Config, opts: Options, pr: Prowlarr, sab: SABnzbd, t: Torrent, title: str,
                   picks: list[Release], pp: int, placed: set[str] = frozenset()):
    """Get every file of the torrent from Usenet, trying the closest kind of NZB first.

    Returns (download folders, SABnzbd job ids, all units, rejected folders). A unit is
    tried with NZBs of its own kind - the person's picks for it first, then everything
    Prowlarr finds from the same release group and resolution - and only split into its
    children (seasons, then episodes) once all of those have been tried. Parts of a download that fell short are still used for
    the children they cover."""
    root = build_units(t, title)
    units = list(root.walk())
    _listings.clear()

    from . import scenefill, season as season_mod   # season imports this module
    scenefill.reset()
    fetcher = season_mod.FileFetcher(cfg, pr, sab, set())
    looked: list[str] = []                      # download folders checked for this build
    # parts settled for with small files still missing, rather than another whole post -
    # offered at the end as "try whole posts" (kept per build: builds run side by side)
    settled = getattr(_this_build, "settled", None)
    if settled is None:
        settled = _this_build.settled = []

    def fill(d: str, missing: list) -> bool:
        return packed_release(t) and scenefill.fill(t, d, missing, fetcher)

    def packed_inner(u: "Unit") -> tuple[int, ...]:
        """For a RAR'd release: the sizes of what the scene RARs of ``u``'s files hold, from
        srrDB - a post holding that video can have the RARs rebuilt from it."""
        return scenefill.inner_sizes(t, u.files) if packed_release(t) else ()

    def satisfied(u: "Unit", d: str) -> bool:
        """Does ``d`` - together with the folders already looked at - hold the unit? Files
        rebuilt or fetched into one folder are not made again for the next."""
        if d not in looked:
            looked.append(d)
        if u.satisfied_by(t, d, fill):
            return True
        others = [x for x in looked if x != d and os.path.isdir(x)]
        want = {f.relpath for f in u.files}
        if others and want <= set(asm.find_sources(t, [d] + others)):
            info(f"{u.need.label}: complete across {len(others) + 1} downloads")
            for x in others:
                if x not in dirs:
                    dirs.append(x)
            return True
        return False

    # the person's picks go to the unit whose kind they are
    for p in picks:
        target = next((u for u in sorted(units, key=lambda u: {"episode": 0, "season": 1, "whole": 2}[u.level])
                       if _fits(dataclasses.replace(u.need, min_size=0, group=None), p) is None), root)
        target.seeded.append(p)

    dirs: list[str] = []
    nzos: list[str] = []
    partials: list[tuple[str, str]] = []        # (folder, title) of downloads that fell short
    pending: dict[str, tuple[Unit, Release]] = {}
    attempted: set[str] = set()                 # posts tried at any level of this build
    verdicts = verdicts_for(cfg)                 # ...and posts earlier runs found no good

    def resolve(u: Unit, d: str | None, nzo: str | None):
        u.done = True
        for x in u.walk():
            x.done = True
        if d and d not in dirs:
            dirs.append(d)
        if nzo:
            nzos.append(nzo)

    def seeded_below(u: Unit) -> bool:
        return any(c.seeded for c in u.walk() if c is not u)

    def next_candidate(u: Unit) -> Release | None:
        if not u.searched:
            u.searched = True
            kind = {"whole": "whole-torrent", "season": "season", "episode": "episode"}[u.level]
            info(f"{u.need.label}: looking for {kind} NZBs"
                 + (f" from {u.need.group.upper()}" if u.need.group else "")
                 + (f", {u.need.res}" if u.need.res else "") + f", at least {gb(u.need.min_size)}")
            found = [r for r in usenet_results(cfg, pr, u.need, u.seeded)
                     if r not in u.seeded and _fits(u.need, r) is None]
            small = [r for r in u.seeded if r.size < u.need.min_size]
            for r in small:
                info(f"{u.need.label}: skipping your pick {r.title} ({gb(r.size)}) - smaller than "
                     f"the {gb(u.need.min_size)} it has to hold")
            u.queue = [r for r in u.seeded if r not in small] + _rank(pr, t, u, found)
            if not u.queue:
                info(f"{u.need.label}: none found")
        tried_guids = {r.guid for r in u.tried} | attempted
        while u.queue:
            r = u.queue.pop(0)
            if r.guid not in tried_guids:
                return r
        return None

    def fetch_extras(u: Unit):
        """Files of ``u`` no child covers - a season's own .nfo/.sfv next to its episodes -
        downloaded on their own (see fetch_small)."""
        covered = {f.relpath for c in u.children for f in c.files}
        fetch_small(u, [f for f in u.files if f.relpath not in covered and f.relpath not in placed])

    def still_missing(u: Unit) -> list:
        """Files of ``u`` that nothing downloaded so far holds."""
        have = set(asm.find_sources(t, dirs + looked + [d for d, _ in partials]))
        return [f for f in u.files if f.relpath not in have and f.relpath not in placed]

    def only_small_missing(u: Unit, missing: list) -> bool:
        """Is what ``u`` still lacks too small to be worth another whole post? Then it comes
        from trimmed NZBs, or over BitTorrent when the build is handed on nearly complete -
        never from another download of everything else again."""
        short = sum(f.length for f in missing)
        whole = sum(f.length for f in u.files) or 1
        # your nearly-complete limit decides (a sample video is often more than a few MB);
        # fetch_small only trims NZBs for what is small enough, the rest comes over BitTorrent
        return 0 < short and short / whole * 100 < float(cfg.nearly_complete_percent or 5) \
            and short < float(cfg.nearly_complete_mb or 200) * 1024 ** 2

    def fetch_small(u: Unit, extras: list):
        """Download only ``extras`` - small files such as an .nfo or .sfv: each of ``u``'s
        posts (same group/resolution) is trimmed to files of those types, so a few KB come
        down instead of the whole post again."""
        if not extras:
            return
        want = {f.relpath for f in extras}
        have = set(asm.find_sources(t, dirs + [d for d, _ in partials]))
        if want <= have:
            return
        exts = sorted({f.ext for f in extras if f.ext})
        names = ", ".join(f.name for f in extras[:3])
        # no size cap: a trimmed NZB costs only the files asked for, and whatever Usenet cannot
        # supply is weighed once, at the end, against the nearly-complete limit
        if not exts:
            warn(f"{u.need.label}: {names} have no file type to look for - not fetched on their own")
            return
        step(f"Fetching {names} ({u.need.label} itself, not any one episode)")
        cands = [r for r in sorted(usenet_results(cfg, pr, u.need, u.seeded), key=lambda r: -(r.grabs or 0))
                 if _fits(dataclasses.replace(u.need, min_size=0), r) is None][:MAX_PEEK]
        for rel in cands:
            check_cancel()
            try:
                data = pr.fetch(rel)
                trimmed = None
                for ext in exts:                 # every posted file of those types
                    part = nzbinfo.trim(data, ext)
                    trimmed = part if trimmed is None else _merge_nzb(trimmed, part)
            except (ApiError, ET.ParseError) as e:
                warn(f"{rel.indexer}: {e}")
                continue
            finally:
                _drop(pr, rel)
            if trimmed is None:
                info(f"{rel.title} ({rel.indexer}) lists no {'/'.join(exts)} file")
                continue
            name = f"{t.name}.extras"
            try:
                nzo = sab.add_nzb(trimmed, name, cfg.sab_category, PP_REPAIR, cfg.sab_priority)
                ledger_for(cfg).record(nzo, name, rel.guid, rel.indexer, None, _torrent_now())
                info(f"{nzo}  {', '.join(exts)} from {rel.title} ({rel.indexer})")
                status, slot = sab_wait(sab, {nzo: name})[nzo]
                if status not in SAB_DONE:
                    continue
                d = job_dir(cfg, slot, name)
            except (ApiError, Abort) as e:
                warn(f"{rel.indexer}: {e}")
                continue
            if os.path.isfile(d):
                d = os.path.dirname(d) if matching.norm(os.path.basename(os.path.dirname(d))).startswith(
                    matching.norm(name)) else d
            partials.append((d, name))
            if want <= set(asm.find_sources(t, dirs + [p for p, _ in partials])):
                info(f"{u.need.label}: got {names}")
                if d not in dirs:
                    dirs.append(d)
                nzos.append(nzo)
                return
            warn(f"{rel.title} had no {names} that fits the torrent")
        warn(f"{u.need.label}: no post of this group has {names}")

    def split(u: Unit):
        kinds = {c.level for c in u.children}
        info(f"{u.need.label}: going to {'season' if 'season' in kinds else 'episode'} NZBs")
        fetch_extras(u)
        for c in u.children:
            d = next((d for d, _ in partials if satisfied(c, d)), None)
            if d:
                info(f"{c.need.label}: already in an earlier download ({os.path.basename(d)})")
                resolve(c, d, None)
            else:
                start(c)

    def start(u: Unit):
        if u.done:
            return
        mine = {f.relpath for f in u.files}
        if mine <= placed:
            info(f"{u.need.label}: already in place from an earlier run - not downloading it again")
            u.tried = used_before(cfg, u.need)
            resolve(u, None, None)
            return
        if u.children and not u.seeded and (mine & placed or seeded_below(u) or u.mixed):
            split(u)
            return
        advance(u)

    def settle_small(u: Unit, d: str | None, nzo: str | None, last: bool = False) -> bool:
        """Is all ``u`` lacks now within the nearly-complete limit? Then no other whole post
        is downloaded for it: what is small is fetched from trimmed NZBs, and the build goes
        on with what is here - complete, or stopping nearly complete with the rest to come
        over BitTorrent if you choose (or your tracker is set to always add). Asked to try
        whole posts (``opts.whole_posts``), it settles only when there are none left
        (``last``)."""
        missing = still_missing(u)
        if not only_small_missing(u, missing):
            return False
        if opts.whole_posts and not last:
            return False
        names = ", ".join(f"{f.name} ({gb(f.length)})" for f in missing[:3])
        info(f"{u.need.label}: only {names} missing now - "
             + ("no other whole post left to try" if last else "not downloading another whole post for that"))
        fetch_small(u, missing)
        if still_missing(u) and not last:
            settled.append({"label": u.need.label, "missing": sum(f.length for f in still_missing(u)),
                            "post": u.need.min_size})
        # everything downloaded that holds a file of the unit goes forward
        for x in [d] + looked + [p for p, _ in partials]:
            if x is None:
                continue
            if x not in dirs and os.path.isdir(x) and set(asm.find_sources(t, [x])) & \
                    {f.relpath for f in u.files}:
                dirs.append(x)
        resolve(u, None, nzo)
        return True

    def advance(u: Unit):
        # every post of this kind from the right group and resolution is tried before splitting
        while True:
            rel = next_candidate(u)
            if rel is None:
                if u.children:
                    split(u)
                    return
                if opts.whole_posts and settle_small(u, None, None, last=True):
                    return                   # every whole post tried: what is left is small
                rel = None if opts.unattended else ask_for_post(cfg, pr, u.need, u.tried, u.seeded)
                if rel is None:
                    raise Abort(f"no Usenet post could supply {u.need.label}")
            u.tried.append(rel)
            attempted.add(rel.guid)
            known = verdicts.seen(t.infohash, u.need.label, rel)
            if known and known.startswith("its archive holds a different release") and packed_inner(u):
                # judged before the video inside the torrent's scene RARs counted as a match:
                # look again (the first volume only)
                info(f"{rel.title} ({rel.indexer}): {known} on an earlier run - looking again, "
                     "now that a re-pack of the scene RARs' video counts")
                known = None
            if known:
                info(f"{rel.title} ({rel.indexer}): {known} on an earlier run - not asking the "
                     "indexer for it again")
                # but what that run downloaded is still here: looked at again (an encrypted
                # re-pack can be opened now, with the password its NZB carried), and it counts
                # towards the torrent either way
                old = reuse(cfg, sab, rel, lambda d: True)
                if old and old[1]:
                    if hasattr(pr, "kept"):
                        pr.kept(rel)             # its archive password, from the NZB kept here
                    if satisfied(u, old[1]):
                        resolve(u, old[1], old[0])
                        return
                    if all(p != old[1] for p, _ in partials):
                        partials.append((old[1], rel.title))
                        if settle_small(u, old[1], old[0]):
                            return
                continue
            if failed_before(cfg, sab, rel):
                continue
            if hasattr(pr, "kept"):
                pr.kept(rel)                     # an earlier download's archive password, from its NZB
            again = reuse(cfg, sab, rel, lambda d: satisfied(u, d))
            if again and again[1]:
                resolve(u, again[1], again[0])
                return
            if again:
                pending[again[0]] = (u, rel)
                return
            try:
                data = pr.fetch(rel)
            except ApiError as e:
                warn(f"{rel.indexer}: {e}")
                continue
            prev = earlier_post(cfg, sab, rel, data)
            if prev:
                state, pnzo, pd, why = prev
                if state == "downloading":
                    info(f"{why}; that one is still downloading - waiting for it instead")
                    pending[pnzo] = (u, rel)
                    return
                if state == "done" and satisfied(u, pd):
                    info(f"{why} - using that download")
                    resolve(u, pd, pnzo)
                    return
                info(f"{why}, and that one " + ("failed in SABnzbd" if state == "failed"
                                                else f"did not hold all of {u.need.label}") + " - skipping it")
                if state == "done":
                    verdicts.mark(t.infohash, u.need.label, rel,
                                  f"it is the same post as a download that did not hold all of {u.need.label}")
                _drop(pr, rel)
                continue
            peek = cfg.peek_archives if opts.peek is None else opts.peek
            if peek and worth_peeking(data, u.files) \
                    and peek_archive(cfg, pr, sab, rel, data, u.files, packed_inner(u)) is False:
                verdicts.mark(t.infohash, u.need.label, rel, "its archive holds a different release")
                _drop(pr, rel)
                continue
            try:
                nzo = sab_submit(cfg, pr, sab, rel, data, pp)
            except ApiError as e:
                warn(f"{rel.indexer}: {e}")
                continue
            pending[nzo] = (u, rel)
            return

    step("Finding the NZBs, closest match first")
    start(root)
    while pending:
        batch = dict(pending)
        pending.clear()
        results = sab_wait(sab, {nzo: rel.title for nzo, (_, rel) in batch.items()})
        for nzo, (status, slot_info) in results.items():
            u, rel = batch[nzo]
            if status in SAB_DONE:
                d = job_dir(cfg, slot_info, rel.title)
                if satisfied(u, d):
                    resolve(u, d, nzo)
                    continue
                warn(f"{rel.title} finished but does not hold all of {u.need.label}; "
                     "what it does hold is used if needed")
                verdicts.mark(t.infohash, u.need.label, rel,
                              f"it downloaded completely and did not hold all of {u.need.label}")
                partials.append((d, rel.title))
                if settle_small(u, d, nzo):
                    continue
            else:
                warn(f"SABnzbd could not complete {rel.title}")
            advance(u)
    rejected = [(d, title_) for d, title_ in partials if d not in dirs]
    return dirs, nzos, units, rejected


def preview_plan(cfg: Config, pr: Prowlarr, t: Torrent, title: str) -> Unit:
    """Show which NZBs each level would try, without downloading or opening any NZB."""
    root = build_units(t, title)
    step("Plan: closest match first")

    def show(u: Unit, depth: int):
        pad = "  " * depth
        found = [r for r in usenet_results(cfg, pr, u.need, []) if _fits(u.need, r) is None]
        what = {"whole": "whole torrent", "season": "season", "episode": "episode"}[u.level]
        info(f"{pad}{u.need.label} ({what}, {len(u.files)} file(s), {gb(u.need.min_size)}"
             + (f", {u.need.group.upper()}" if u.need.group else "") + (", mixed groups" if u.mixed else "")
             + f"): {len(found)} NZB(s)")
        for r in sorted(found, key=lambda r: -(r.grabs or 0))[:5]:
            info(f"{pad}    {r.title}  ({r.indexer}, {gb(r.size)}, {r.grabs} grabs)")
        for c in u.children:
            show(c, depth + 1)
    show(root, 0)
    return root


_POSTER_SUFFIX = re.compile(r"-(xpost|repost|obfuscated|postbot|scrambled)$", re.I)


def found_on_disk(cfg: Config, sab: SABnzbd, t: Torrent, related=()) -> tuple[list[str], set[str]]:
    """nzb2seed's own finished downloads that may hold this torrent's files - made for it,
    for a torrent of the same release (``related``: built from another tracker), or of a post
    named as the release - and which of its files they hold (name and size, as laying out
    matches them). Looked at before anything is searched for: a file already here is never
    looked for again. (folders, the torrent's relpaths they hold)"""
    want = matching.norm(release_title(t.name))
    hashes = {h for h in (t.infohash, *related) if h}
    records = ledger_for(cfg).all()
    for j in records:
        if j.get("torrent") and matching.norm(_POSTER_SUFFIX.sub("", j.get("title") or "")) == want:
            hashes.add(j["torrent"])
    dirs = []
    for j in records:
        if j.get("torrent") not in hashes:
            continue
        try:
            status, slot = sab.status(j["nzo"])
            if status not in SAB_DONE:
                continue
            d = job_dir(cfg, slot, j.get("title", ""))
        except (ApiError, Abort, KeyError):
            continue
        if os.path.isdir(d) and d not in dirs:
            dirs.append(d)
    if not dirs:
        return [], set()
    res = asm.assemble(t, dirs, PROBE_DIR, dry_run=True, log=lambda *_: None)
    lacking = {f.relpath for f in res.missing} | {f.relpath for f, _ in res.wrong_size}
    have = {f.relpath for f in t.real_files} - lacking
    return (dirs if have else []), have


def earlier_dirs(cfg: Config, sab: SABnzbd, t: Torrent) -> list[str]:
    """Folders of every finished download nzb2seed made for this torrent on any attempt:
    once the torrent is complete, their leftovers are cleaned up with the rest."""
    out = []
    for j in ledger_for(cfg).all():
        if j.get("torrent") != t.infohash:
            continue
        try:
            status, slot = sab.status(j["nzo"])
            if status in SAB_DONE:
                d = job_dir(cfg, slot, j.get("title", ""))
                if os.path.isdir(d) and d not in out:
                    out.append(d)
        except (ApiError, Abort):
            pass
    return out


def usenet_single(cfg: Config, opts: Options, pr: Prowlarr, sab: SABnzbd, t: Torrent,
                  group: list[Release], pp: int, rejected: list | None = None,
                  slots: list | None = None) -> tuple[list[str], list[str]]:
    """Build from one release (e.g. a movie or an episode) - see plan_and_fetch."""
    dirs, nzos, units, rej = plan_and_fetch(cfg, opts, pr, sab, t, t.name, list(group), pp)
    if slots is not None:
        slots.extend(units)
    if rejected is not None:
        rejected.extend(rej)
    return dirs, nzos


def usenet_multi(cfg: Config, pr: Prowlarr, sab: SABnzbd, t: Torrent, groups: list[list[Release]],
                 pp: int, slots: list | None = None, placed: set[str] = frozenset()
                 ) -> tuple[list[str], list[str]]:
    """Build from several picked releases (e.g. episodes of a pack) - see plan_and_fetch."""
    picks = [r for g in groups for r in g]
    dirs, nzos, units, _ = plan_and_fetch(cfg, Options(), pr, sab, t, t.name, picks, pp, placed)
    if slots is not None:
        slots.extend(units)
    return dirs, nzos


def next_post(cfg: Config, pr: Prowlarr, slot, ask: bool = True) -> Release | None:
    """The next post to try for a unit when repairing: queued ones, then other posts of the
    same release group (searched once), then whatever the person picks."""
    while True:
        while slot.queue:
            r = slot.queue.pop(0)
            if r.guid not in {x.guid for x in slot.tried}:
                return r
        if not slot.searched:
            slot.searched = True
            slot.queue = find_alternatives(cfg, pr, slot.need, slot.tried, slot.known)
            if slot.queue:
                continue
        return ask_for_post(cfg, pr, slot.need, slot.tried, slot.known) if ask else None


def remove_rejected(rejected: list[tuple[str, str]]):
    """Delete downloads of posts that fell short, now that the torrent is complete. Only
    job folders named after the release are removed - never a shared folder."""
    step("Removing posts that were tried and not used")
    for d, title in rejected:
        if os.path.isdir(d) and matching.norm(os.path.basename(d)).startswith(matching.norm(title)):
            shutil.rmtree(d)
            info(f"removed {d}")
        elif os.path.isfile(d):
            os.remove(d)
            info(f"removed {d}")


# ---------------------------------------------------------------- earlier downloads for a torrent

def saved_torrent(cfg: Config, title: str) -> Torrent | None:
    """The .torrent an earlier build saved for this release, if there is one."""
    want = matching.norm(title)
    try:
        names = os.listdir(cfg.torrent_dir)
    except OSError:
        return None
    for n in names:
        if n.endswith(".torrent") and matching.norm(n[:-8]) == want:
            try:
                with open(os.path.join(cfg.torrent_dir, n), "rb") as fh:
                    return parse(fh.read())
            except (OSError, ValueError):
                return None
    return None


def previous_downloads(cfg: Config, sab: SABnzbd, title: str) -> list[dict]:
    """NZBs nzb2seed downloaded on earlier attempts at this torrent, and what became of them.

    Matched by the torrent they were downloaded for, or by show + episode and the release
    group of the torrent's own file for that episode (from the saved .torrent), or - with no
    saved .torrent - by show + episode/season."""
    t = saved_torrent(cfg, title)
    n = matching.norm(title)
    m = _EP.search(n) or re.search(r"(?<![a-z0-9])s\d{1,4}(?![0-9e])", n)
    show = n[:m.start()] if m else n.split("-")[0]
    wanted: dict[str, str | None] = {}           # episode token -> release group (None = any)
    if t:
        for f in t.real_files:
            e = _EP.search(matching.norm(f.name))
            if e:
                wanted[e.group(0)] = group_of(f.name)
    out = []
    for j in ledger_for(cfg).all():
        jt = j.get("title", "")
        jn = matching.norm(jt)
        mine = t is not None and j.get("torrent") == t.infohash
        if not mine:
            if not jn.startswith(show) or not show:
                continue
            e = _EP.search(jn)
            if wanted:
                if not e or e.group(0) not in wanted:
                    continue
                g = wanted[e.group(0)]
                if g and group_of(jt) != g:
                    continue
            elif m and m.group(0) not in jn:
                continue
        state, note = "unknown", ""
        try:
            status, slot = sab.status(j["nzo"])
        except ApiError:
            status, slot = "Unknown", {}
        if status in SAB_FAILED:
            state, note = "failed", "failed in SABnzbd - skipped automatically"
        elif status.startswith("Queued:") or status not in SAB_DONE | {"Unknown"}:
            state, note = "downloading", f"still downloading ({slot.get('percentage', '?')}%)"
        elif status in SAB_DONE:
            try:
                d = job_dir(cfg, slot, jt)
                has = any(os.path.getsize(os.path.join(r, x)) >= 1 << 20
                          for r, _, xs in os.walk(d) for x in xs)
            except (Abort, OSError):
                has = False
            state, note = ("finished", "finished - reused automatically if it holds what the torrent needs") \
                if has else ("used", "finished - its files were moved into a torrent or removed")
        else:
            state, note = "gone", "no longer in SABnzbd"
        out.append({"title": jt, "indexer": j.get("indexer", ""), "size": j.get("size") or 0,
                    "nzo": j["nzo"], "state": state, "note": note,
                    "submitted": j.get("submitted")})
    return out


# ---------------------------------------------------------------- fixing pieces that fail

@dataclass
class Retry:
    """What bad-piece repair needs to fetch other posts."""
    cfg: Config
    pr: Prowlarr
    sab: SABnzbd
    pp: int
    slots: list
    unattended: bool = False


def inner_pieces(t: Torrent, f) -> range:
    """Pieces lying entirely inside file ``f``."""
    first = -(-f.offset // t.piece_length)
    last = (f.offset + f.length) // t.piece_length - 1
    return range(first, last + 1) if last >= first else range(0)


def copy_verifies(t: Torrent, f, path: str, placed: dict) -> bool:
    """Do all pieces inside ``f`` verify when ``f`` is read from ``path``?"""
    rng = inner_pieces(t, f)
    with PieceVerifier(t, lambda ff: path if ff.relpath == f.relpath else placed.get(ff.relpath)) as pv:
        for n, i in enumerate(rng):
            if n % 32 == 0:
                check_cancel()
                progress(f"checking {os.path.basename(path)}: {n}/{len(rng)} pieces")
            if not pv.check(i):
                end_progress()
                return False
    end_progress()
    return True


def repair_bad_pieces(rt: Retry, t: Torrent, res, bad: list[int], owned, job_dirs: list[str]) -> bool:
    """Replace the files that failing pieces point to with copies from other posts of the
    same release until every piece verifies. Only files nzb2seed placed are replaced; a
    copy is only swapped in when it makes the failing piece(s) pass. True = all fixed."""
    file_slot = {}
    for sl in rt.slots:
        sl.searched = False               # allow a fresh search for alternatives
    # the finest unit holding a file claims it: an episode NZB is the cheapest replacement
    order = {"episode": 0, "season": 1, "whole": 2}
    for sl in sorted(rt.slots, key=lambda x: order.get(x.level, 3)):
        for f in t.real_files:
            if sl.covers(f):
                file_slot.setdefault(f.relpath, sl)
    pool: dict[str, list] = {}            # relpath -> [(path, release)] copies whose insides verify
    exhausted: set[str] = set()
    by_rel = {f.relpath: f for f in t.real_files}

    def piece_ok(i: int, choice: dict) -> bool:
        with PieceVerifier(t, lambda ff: choice.get(ff.relpath, res.placed.get(ff.relpath))) as pv:
            return pv.check(i)

    def suspects(i: int) -> list:
        start, end = t.piece_span(i)
        fs = [f for f in t.real_files if f.offset < end and f.offset + f.length > start]
        # a file starting inside the piece first (re-muxed headers), then the others
        return sorted(fs, key=lambda f: (not (start <= f.offset < end), f.offset))

    def swap(rel: str, path: str, used: Release):
        dst = res.placed[rel]
        os.remove(dst)
        owned.forget_file(dst)
        asm._move(path, dst, owned)
        owned.save()
        info(f"replaced {by_rel[rel].name} with the copy from {used.title} ({used.indexer})")

    def fetch_copy(f) -> tuple[str, Release] | None:
        slot = file_slot[f.relpath]
        while True:
            rel = next_post(rt.cfg, rt.pr, slot, not rt.unattended)
            if rel is None:
                return None
            slot.tried.append(rel)
            if failed_before(rt.cfg, rt.sab, rel):
                continue
            step(f"Trying another copy of {f.name}: {rel.title} ({rel.indexer})")
            again = reuse(rt.cfg, rt.sab, rel, lambda d: supplies(t, d, f.name))
            if again and again[1]:
                d = again[1]
            else:
                try:
                    if again:
                        nzo = again[0]
                    else:
                        data = rt.pr.fetch(rel)
                        prev = earlier_post(rt.cfg, rt.sab, rel, data)
                        if prev and prev[0] != "downloading":
                            info(f"{prev[3]} - skipping it")
                            _drop(rt.pr, rel)
                            continue
                        nzo = prev[1] if prev else sab_submit(rt.cfg, rt.pr, rt.sab, rel, data, rt.pp)
                except ApiError as e:
                    warn(f"{rel.indexer}: {e}")
                    continue
                status, slot_info = sab_wait(rt.sab, {nzo: rel.title})[nzo]
                if status not in SAB_DONE:
                    continue
                d = job_dir(rt.cfg, slot_info, rel.title)
            if d not in job_dirs:
                job_dirs.append(d)        # ours: cleaned up with the rest
            src = asm.find_sources(t, [d]).get(f.relpath)
            if src is None and supplies(t, d, f.name):
                src = asm.find_sources(t, [d]).get(f.relpath)
            if src is None:
                warn(f"{rel.title} does not hold {f.name}")
                continue
            if not copy_verifies(t, f, src, res.placed):
                warn(f"the copy of {f.name} in {rel.title} differs inside the file too")
                continue
            info(f"the copy of {f.name} in {rel.title} verifies inside the file")
            return src, rel

    pending = sorted(set(bad))
    while pending:
        check_cancel()
        i = pending[0]
        fs = suspects(i)
        changeable = [f for f in fs if f.relpath in file_slot and owned.owns(res.placed[f.relpath])]
        options = [[None] + pool.get(f.relpath, []) if f in changeable else [None] for f in fs]
        found = None
        for combo in itertools.product(*options):
            choice = {f.relpath: c for f, c in zip(fs, combo) if c}
            if choice and piece_ok(i, {r: c[0] for r, c in choice.items()}):
                found = choice
                break
        if found:
            for rel, (path, used) in found.items():
                swap(rel, path, used)
                pool[rel] = [c for c in pool.get(rel, []) if c[0] != path]
            pending = [j for j in pending if not piece_ok(j, {})]
            info(f"piece {i} verifies now; {len(pending)} failing piece(s) left")
            continue
        todo = [f for f in changeable if f.relpath not in exhausted]
        if not todo:
            names = ", ".join(f.name for f in fs)
            warn(f"piece {i} still fails and there are no more copies of {names} to try")
            return False
        target = min(todo, key=lambda f: (len(pool.get(f.relpath, [])), changeable.index(f)))
        got = fetch_copy(target)
        if got is None:
            exhausted.add(target.relpath)
        else:
            pool.setdefault(target.relpath, []).append(got)
    return True


# ---------------------------------------------------------------- torrent side

def save_torrent(cfg: Config, data: bytes) -> tuple[Torrent, str]:
    t = parse(data)
    os.makedirs(cfg.torrent_dir, exist_ok=True)
    safe = re.sub(r'[<>:"/\\|?*]', "_", t.name)
    path = os.path.join(cfg.torrent_dir, f"{safe}.torrent")
    with open(path, "wb") as fh:
        fh.write(data)
    info(f"{t.name}: {len(t.real_files)} file(s), {gb(t.total_size)}, "
         f"piece {t.piece_length // 1024} KiB, infohash {t.infohash}"
         + (", private" if t.private else ""))
    info(f"saved {path}")
    return t, path


def show_layout(t: Torrent):
    dirs = sorted({"/".join(f.parts[1:-1]) for f in t.real_files if len(f.parts) > 2})
    if dirs:
        info("torrent subfolders: " + ", ".join(dirs))


def qbit_preflight(qb: QBittorrent, t: Torrent):
    qb.login()
    info(f"qBittorrent {qb.app_version()} (WebAPI {'.'.join(map(str, qb.api_version()))})")
    try:
        prefs = qb.preferences()
        if prefs.get("incomplete_files_ext"):
            warn("qBittorrent appends .!qB to incomplete files; the recheck still finds complete "
                 "files, but disable it if you see files renamed")
    except ApiError:
        pass
    existing = qb.info(t.infohash)
    if existing and existing.get("state") not in STOPPED:
        raise Abort(f"this torrent is already in qBittorrent and active ({existing.get('state')}); "
                    "stop it first so it cannot download anything")
    return existing


def _cancel_tick(_t):
    check_cancel()


def owned_record(cfg: Config, t: Torrent, opts: "Options | None" = None) -> asm.Owned:
    """The record of what nzb2seed placed for this torrent. It also remembers which part of
    nzb2seed made it: only builds the Automatic tab made are ever removed again by
    retention, so a build, season or assemble you asked for yourself is never swept away."""
    owned = asm.Owned(os.path.join(cfg.torrent_dir, f"{t.infohash}.owned.json"))
    if opts is not None:
        owned.source = "auto" if opts.unattended else "manual"
    return owned


_EXTRA_EXTS = {".nfo", ".txt", ".jpg", ".jpeg", ".png", ".srt", ".sub", ".idx", ".sfv", ".md5", ".url"}


def explain_missing(cfg: Config, t: Torrent, missing: list, wrong_size: list, short: int,
                    total: int) -> list[str]:
    """Why each missing file could not come from Usenet, and what can be done next - said in
    the log, so a build that stops short needs no digging to understand."""
    from . import metadata, scenefill
    out = []
    for f in missing:
        release, det = scenefill.details_for(t, scenefill.release_of(t, f), [f])
        ext = os.path.splitext(f.name)[1].lower()
        if det:
            listed = any(os.path.basename(x["name"]).lower() == f.name.lower() for x in metadata.release_files(det))
            if "sample" in f.name.lower() and ext in (".mkv", ".mp4", ".avi", ".m2ts", ".ts"):
                why = ("the release's Sample: srrDB never stores samples, and no Usenet post of the "
                       "release that nzb2seed tried had it")
            elif listed:
                why = "srrDB has it with the scene release, but it could not be fetched from srrDB this time"
            else:
                why = (f"not part of the scene release {release} as srrDB knows it - added for this "
                       "upload, so only the torrent has it")
        elif ext in _EXTRA_EXTS or f.length < 5 << 20:
            why = ("made by the group for this upload (a P2P release, in no pre database and not on "
                   "srrDB) - only the torrent has it, byte for byte")
        else:
            why = "no Usenet post that nzb2seed tried holds it"
        out.append(f"why {f.name} ({exact_gb(f.length)}) is missing: {why}")
    for f, size in wrong_size:
        out.append(f"why {f.name} is wrong: the Usenet copy is {size} bytes, the torrent's {f.length} - "
                   "another version of that file")
    pct = short / (total or 1) * 100
    within = pct < float(cfg.nearly_complete_percent or 5) and short < float(cfg.nearly_complete_mb or 200) * 1024 ** 2
    seeders = getattr(_this_build, "seeders", None)
    tracker = getattr(_this_build, "tracker", "") or "the tracker"
    tried_all = not getattr(_this_build, "settled", None)
    if seeders == 0:
        nxt = (f"Prowlarr reported no seeders on {tracker}, so the missing {exact_gb(short)} cannot come "
               "over BitTorrent from there - Look on other trackers" + ("" if tried_all else ", or Try whole posts"))
    elif within:
        nxt = (f"Add to torrent client downloads just the missing {exact_gb(short)} over BitTorrent from "
               f"{tracker}" + ("" if tried_all else " - or Try whole posts first, to look inside other posts"))
    else:
        nxt = (f"the missing {exact_gb(short)} is more than your limit of {float(cfg.nearly_complete_percent or 5):g}% "
               f"or {float(cfg.nearly_complete_mb or 200):g} MB - " + ("Try whole posts, " if not tried_all else "")
               + "Look on other trackers, or use Override to add it to the torrent client anyway")
    out.append("Next: " + nxt)
    return out


def adopt_owned(cfg: Config, t: Torrent, output_dir: str, owned: asm.Owned, qb) -> int:
    """Files nzb2seed placed for *another* torrent, exactly where this one's go - the same
    release from another tracker, say, after "Look on other trackers": another infohash,
    so another record, and they would read as someone else's. They are this build's to use
    when that torrent is not in qBittorrent (nothing uses them) and the files are as
    nzb2seed left them. Returns how many were taken over."""
    targets = {asm._key(asm.target_path(output_dir, f)) for f in t.real_files}
    root = asm._key(asm.torrent_root(t, output_dir))
    mine = os.path.abspath(owned.path or "")
    taken = 0
    for rec_path in sorted(glob.glob(os.path.join(glob.escape(cfg.torrent_dir), "*.owned.json"))):
        if os.path.abspath(rec_path) == mine:
            continue
        other = asm.Owned(rec_path)
        hits = [p for p in other.files if asm._key(p) in targets and other.unchanged(p) is not False]
        if not hits:
            continue
        ih = os.path.basename(rec_path)[:-len(".owned.json")]
        try:
            there = qb.info(ih) if qb is not None else None
        except ApiError:
            there = True                        # cannot tell: left as it is
        if there:
            info(f"{len(hits)} file(s) where this torrent's go belong to another torrent nzb2seed built "
                 f"({ih[:8]}...), which is in qBittorrent - left alone")
            continue
        for p in hits:
            other.forget_file(p)
            other.stamps.pop(p, None)
            owned.add_file(p)
        for d in [d for d in other.dirs if asm._key(d) == root or asm._key(d).startswith(root + os.sep)]:
            other.dirs.discard(d)
            owned.dirs.add(d)
        other.save()
        taken += len(hits)
        info(f"taking over {len(hits)} file(s) nzb2seed placed for another torrent of this release "
             f"({ih[:8]}..., not in qBittorrent)")
    return taken


def finish(cfg: Config, opts: Options, t: Torrent, torrent_path: str, source_dirs: list[str],
           output_dir: str, qb: QBittorrent | None, existing: dict | None,
           sources_are_ours: bool = True, retry: Retry | None = None,
           earlier_dirs: list[str] = (), keep_dirs: list[str] = (),
           ours_dirs: list[str] | None = None) -> dict:
    """``sources_are_ours``: the source folders are SABnzbd jobs this build submitted, so
    their leftovers may be deleted. False for folders the user pointed at (assemble)."""
    step(f"Laying out files for the torrent in {output_dir}")
    for d in source_dirs:
        info(f"from {d}")
    show_layout(t)
    last = [0.0]

    def copying(name, done, total):
        # SSD -> HDD copies; deliberately no cancel check here, a file is never left half-moved
        now = time.monotonic()
        if done == total:
            progress(f"copied {name} ({gb(total)}) to the other disk")
            end_progress()
        elif now - last[0] > 0.5:
            last[0] = now
            progress(f"copying {name}: {gb(done)} of {gb(total)}")
    # placing files writes to the output disk - and empties staging afterwards, so it
    # is never held back by staging being full
    space.wait_for_room(step, progress, role="output")
    owned = owned_record(cfg, t, opts)
    asm.OWNER = asm.parse_owner(cfg.file_owner)
    if not opts.dry_run:
        adopt_owned(cfg, t, output_dir, owned, qb)
    try:
        res = asm.assemble(t, source_dirs, output_dir, dry_run=opts.dry_run, log=line,
                           progress=copying, owned=owned, keep=keep_dirs)
    finally:
        if not opts.dry_run:
            owned.save()
    for f, p in res.conflicts:
        warn(f"IN THE WAY  {p} already exists with a different size and was not created by "
             "nzb2seed; it is left untouched")
    if res.conflicts:
        raise Abort("files that nzb2seed did not create are where the torrent's files must go; "
                    "nothing was moved or deleted")
    for n in res.notes:
        warn(n)
    for f in res.missing:
        warn(f"MISSING  {f.relpath} ({gb(f.length)})")
    for f, size in res.wrong_size:
        warn(f"WRONG SIZE  {f.relpath}: have {size}, need {f.length}")
    if opts.dry_run:
        info("dry run: nothing moved")
        asm.cleanup(t, res, (source_dirs if sources_are_ours else []) if ours_dirs is None else ours_dirs,
                    output_dir, owned, dry_run=True, log=line)
        return {"result": "dry run"}
    if not res.complete:
        total = sum(f.length for f in t.real_files) or 1
        short = sum(f.length for f in res.missing) + sum(f.length for f, _ in res.wrong_size)
        have = max(0.0, 1 - short / total)
        try:
            for text in explain_missing(cfg, t, res.missing, res.wrong_size, short, total):
                info(text)
        except Exception as e:                  # an explanation must never stop the result
            warn(f"could not say why: {e}")
        raise Incomplete(
            f"the NZB download(s) do not contain every file of the torrent: {exact_pct(have)} "
            f"of it is here and {exact_gb(short)} is missing; not adding it (nothing deleted; try "
            "post-processing 'unpack' if the post was packed differently)",
            have, t.infohash, torrent_path, map_path(output_dir, cfg.local_to_qbit),
            in_client=False, tracker=_tracker_now(), short=short)
    info(f"all {len(t.real_files)} file(s) present with the right size")

    retry_on = retry is not None and (cfg.retry_bad_pieces if opts.retry_bad is None else opts.retry_bad)
    # One full hashing pass is enough, and qBittorrent's recheck has to happen anyway - so
    # nothing is hashed here first unless asked for, or unless there is no qBittorrent to
    # ask. Failing pieces are repaired after that recheck, with only the pieces of the
    # replaced files hashed here (see repair_bad_pieces), and qBittorrent checks once more.
    # In practice SABnzbd's par2 repair means the data arrives sound: this saves a full
    # read of every build and costs nothing when something does need fixing.
    if opts.local_verify or cfg.local_verify or (retry_on and qb is None):
        step("Hashing all pieces locally")

        def tick(i, n, bad_so_far):
            check_cancel()
            progress(f"{i}/{n} pieces")
            m = bytearray(b"#" * i + b"." * (n - i))
            for b in bad_so_far:
                m[b] = ord("x")
            pieces(m.decode())
        bad = asm.verify_all(t, res, tick)
        end_progress()
        if bad:
            for f in asm.files_for_pieces(t, bad):
                warn(f"bad pieces in {f.relpath}")
            if not retry_on:
                raise Abort(f"{len(bad)} of {len(t.pieces)} pieces fail locally; not adding the torrent "
                            "(turn on 'try other posts' to replace the files automatically)")
            step(f"Replacing files behind {len(bad)} failing piece(s) with other posts")
            if not repair_bad_pieces(retry, t, res, bad, owned, source_dirs):
                raise Abort(f"pieces still fail after trying the other posts; not adding the torrent")
            pieces("#" * len(t.pieces))
        info("100% of pieces verified locally")

    if cfg.cleanup and not opts.no_cleanup:
        step("Removing files that are not part of the torrent")
        ours = (list(source_dirs) + [d for d in earlier_dirs if d not in source_dirs]) if sources_are_ours else []
        if ours_dirs is not None:
            ours = list(ours_dirs)            # e.g. only the downloads an assemble made
        removed = asm.cleanup(t, res, ours, output_dir, owned, log=line)
        owned.save()
        info(f"{len(removed)} file(s) removed" + ("" if sources_are_ours else
             " (your source folders are left as they are)"))

    if qb is None:
        info("qBittorrent step skipped")
        return {"result": "files ready", "progress": None}

    save_path = map_path(output_dir, cfg.local_to_qbit)
    step("Adding to qBittorrent (stopped)")
    if existing is None:
        with open(torrent_path, "rb") as fh:
            qb.add_stopped(fh.read(), os.path.basename(torrent_path), save_path,
                           cfg.qbit_category, ",".join(cfg.qbit_tags))
        tinfo = None
        for _ in range(20):
            tinfo = qb.info(t.infohash)
            if tinfo:
                break
            time.sleep(0.5)
        if not tinfo:
            raise Abort("qBittorrent accepted the torrent but it does not show up")
    else:
        info("already in qBittorrent (stopped); reusing it")
        tinfo = existing
    if tinfo.get("state") not in STOPPED and tinfo.get("state") not in ("checkingUP", "checkingDL",
                                                                         "checkingResumeData"):
        warn(f"torrent came up as {tinfo.get('state')}; stopping it")
        qb.stop(t.infohash)
    qb.wait_idle(t.infohash, on_tick=_cancel_tick)

    step("Pointing qBittorrent at the data")
    tinfo = qb.info(t.infohash)
    if tinfo.get("save_path", "").rstrip("/\\") != save_path.rstrip("/\\"):
        info(f"{tinfo.get('save_path')} -> {save_path}")
        qb.set_location(t.infohash, save_path)
        qb.wait_idle(t.infohash, on_tick=_cancel_tick)
    info(f"save path: {save_path}")

    step("Force recheck")
    qb.recheck(t.infohash)

    def piece_map(final=False):
        try:
            states = qb.piece_states(t.infohash)
        except ApiError:
            return
        pieces("".join("#" if p == 2 else ("x" if final else ".") for p in states))

    def tick(ti):
        check_cancel()
        progress(f"{ti.get('state')}: {ti.get('progress', 0) * 100:.1f}%")
        piece_map()
    tinfo = qb.wait_idle(t.infohash, on_tick=tick, min_wait=5)
    end_progress()
    piece_map(final=True)
    if tinfo.get("progress", 0) < 1 and retry_on:
        tinfo = _repair_after_recheck(qb, t, res, owned, retry, source_dirs, tick)
        piece_map(final=True)
    pct = tinfo.get("progress", 0) * 100
    if tinfo.get("progress", 0) < 1:
        for f in qb.files(t.infohash):
            if f.get("progress", 0) < 1:
                warn(f"{exact_pct(f.get('progress', 0)):>9}  {f.get('name')}")
        raise Incomplete(f"recheck finished at {exact_pct(tinfo.get('progress', 0))}; the torrent "
                         "stays stopped (no download, no H&R)",
                         tinfo.get("progress", 0), t.infohash, torrent_path, save_path,
                         in_client=True, tracker=_tracker_now(),
                         short=round(sum(f.length for f in t.real_files) * (1 - tinfo.get("progress", 0))))
    info("recheck: 100.0% - complete, built entirely from Usenet")

    if opts.start or cfg.start_when_complete:
        qb.start(t.infohash)
        info("started seeding")
        state = "seeding"
    else:
        info("left stopped; start it in qBittorrent to seed")
        state = "stopped"
    return {"result": f"100.0% ({state})", "progress": 1.0, "infohash": t.infohash}


def _repair_after_recheck(qb, t: Torrent, res, owned, retry: "Retry", source_dirs: list[str],
                          tick) -> dict:
    """qBittorrent's recheck found failing pieces: replace the files behind them from other
    posts, then have qBittorrent check again.

    The torrent is stopped the whole time - a stopped torrent never downloads, so swapping
    a file under it cannot fetch anything over BitTorrent. Choosing a replacement hashes
    only the pieces it has to (repair_bad_pieces), and qBittorrent's recheck of the whole
    torrent happens once at the end rather than after every file."""
    states = qb.piece_states(t.infohash)
    if len(states) != len(t.pieces):
        warn(f"qBittorrent reported {len(states)} pieces for a torrent of {len(t.pieces)}; "
             "not repairing from that")
        return qb.info(t.infohash) or {}
    bad = [i for i, st in enumerate(states) if st != 2]
    for f in asm.files_for_pieces(t, bad):
        warn(f"bad pieces in {f.relpath}")
    step(f"Replacing files behind {len(bad)} failing piece(s) with other posts")
    if not repair_bad_pieces(retry, t, res, bad, owned, source_dirs):
        warn("pieces still fail after trying the other posts")
        return qb.info(t.infohash) or {}
    step("Checking again in qBittorrent")
    qb.recheck(t.infohash)
    info_ = qb.wait_idle(t.infohash, on_tick=tick, min_wait=5)
    end_progress()
    return info_


# ---------------------------------------------------------------- whole runs

def site_of(host: str) -> str:
    """"tracker.example.org" -> "example.org" (and "x.example.co.uk" -> "example.co.uk")."""
    parts = [p for p in (host or "").lower().split(".") if p]
    keep = 3 if len(parts) > 2 and len(parts[-1]) == 2 and parts[-2] in ("co", "com", "org", "net", "ac") else 2
    return ".".join(parts[-keep:])


_sites_cache: dict = {"at": 0.0, "sites": []}


def tracker_name(trackers, prowlarr=None) -> str:
    """The tracker a torrent announces to, by the name Prowlarr gives its indexer (the name
    the rest of nzb2seed - and your "always add" list - uses), else by its site."""
    from .worth import tracker_of
    site = site_of(tracker_of(trackers))
    if not site:
        return ""
    if prowlarr is not None:
        if time.time() - _sites_cache["at"] > 3600:
            try:
                _sites_cache.update(at=time.time(), sites=prowlarr.indexer_sites())
            except (ApiError, OSError, ValueError, AttributeError):
                pass
        for name, urls in _sites_cache["sites"]:
            if any(site_of(urlsplit(u).hostname or "") == site for u in urls):
                return name
    return site


def local_release(t: Torrent, path: str) -> Release:
    """A Release standing in for a .torrent file the user already has."""
    return Release(title=t.name, protocol="torrent", indexer=os.path.basename(path), indexer_id=0,
                   size=t.total_size, guid=f"file:{t.infohash}", download_url="", info_url="",
                   publish_date="", grabs=None, seeders=None, files=len(t.real_files))


def execute_run(cfg: Config, opts: Options, tor_rel: Release, groups: list[list[Release]],
                torrent_data: bytes | None = None) -> dict:
    """Everything after the releases have been chosen. ``torrent_data``: a .torrent the
    user supplied, used instead of downloading one."""
    sab = SABnzbd(cfg.sab_url, cfg.sab_key)
    pr = grab.Source(cfg, Prowlarr(cfg.prowlarr_url, cfg.prowlarr_key), sab)
    qb = None if opts.no_qbit else QBittorrent(cfg.qbit_url, cfg.qbit_user, cfg.qbit_pass)
    info(f"SABnzbd {sab.version()}")
    if n := grab.remove_stray_checks(sab):
        info(f"removed {n} paused NZB-check job(s) an interrupted build left in SABnzbd")
    _sweep_peeks(cfg, sab)              # a build starting: what a restart left behind
    metadata.configure(cfg.flaresolverr_url, cfg.outbound_proxy)   # srrDB, for scene releases
    try:
        return _execute_run(cfg, opts, pr, sab, qb, tor_rel, groups, torrent_data)
    finally:
        pr.close()                      # paused NZB-check jobs of posts not used
        _sweep_peeks(cfg, sab)          # a build ending: its torrent is complete, or given up on
        metadata.close_session()


def _sweep_peeks(cfg: Config, sab: SABnzbd):
    try:
        jobs, freed = remove_stray_peeks(cfg, sab)
    except Exception:                   # housekeeping must never fail a build
        return
    if jobs or freed:
        info(f"removed {jobs} finished peek job(s) and {gb(freed)} they left on disk")


def _execute_run(cfg: Config, opts: Options, pr: Prowlarr, sab: SABnzbd, qb, tor_rel: Release,
                 groups: list[list[Release]], torrent_data: bytes | None) -> dict:
    if torrent_data is not None:
        step(f"Using .torrent file {tor_rel.indexer}")
        t, path = save_torrent(cfg, torrent_data)
    else:
        step(f"Downloading .torrent from {tor_rel.indexer}")
        t, path = save_torrent(cfg, pr.fetch(tor_rel))
    show_layout(t)
    _this_build.torrent = t.infohash
    _this_build.tracker = tor_rel.indexer or ""
    _this_build.settled = []
    if tor_rel.guid.startswith("file:"):
        # a .torrent that did not come from a Prowlarr search (automatic builds, uploads):
        # its "indexer" is only the file's name - the tracker is the one it announces to
        _this_build.tracker = tracker_name(t.trackers, getattr(pr, "pr", pr)) or _this_build.tracker
    report_torrent(t.infohash)
    _this_build.seeders = tor_rel.seeders if (tor_rel.seeders or 0) >= 0 and tor_rel.seeders is not None else None
    info(f"tracker: {_this_build.tracker or 'unknown'}"
         + (f" ({_this_build.seeders} seeder{'' if _this_build.seeders == 1 else 's'} reported)"
            if _this_build.seeders is not None else ""))
    existing = qbit_preflight(qb, t) if qb else None

    pp = choose_pp(opts, cfg, t)
    mode = opts.pp or cfg.post_processing
    why = "as configured" if mode in ("repair", "unpack") else \
        ("the torrent holds RAR archives" if pp == PP_REPAIR else "the torrent holds unpacked files")
    info(f"SABnzbd post-processing: {'+Repair' if pp == PP_REPAIR else '+Repair/Unpack'} ({why}); never +Delete")
    sab_preflight(sab, pp)

    # what is already here comes first: files nzb2seed placed (for this torrent, or for another
    # torrent of the release that is not in qBittorrent), then its downloads that hold some of
    # the files - only what is still missing is searched for, and nothing is looked for twice
    out_dir = opts.output_dir or cfg.output_dir
    if out_dir and not opts.dry_run:
        mine = owned_record(cfg, t, opts)
        if adopt_owned(cfg, t, out_dir, mine, qb):
            mine.save()
    placed = placed_before(cfg, opts, t)
    disk_dirs, on_disk = found_on_disk(cfg, sab, t, opts.related)
    fresh = on_disk - placed
    if fresh:
        files = [f for f in t.real_files if f.relpath in fresh]
        info(f"already on disk in nzb2seed's earlier downloads: {len(files)} of {len(t.real_files)} file(s), "
             f"{gb(sum(f.length for f in files))} - not looked for again")
    picks = [r for g in groups for r in g]
    dirs, nzos, slots, rejected = plan_and_fetch(cfg, opts, pr, sab, t, t.name, picks, pp, placed | on_disk)
    dirs = disk_dirs + [d for d in dirs if d not in disk_dirs]
    if not dirs and not placed:
        raise Abort("nothing was downloaded")

    output_dir = opts.output_dir or cfg.output_dir or os.path.dirname(os.path.abspath(dirs[0]))
    out = finish(cfg, opts, t, path, dirs, output_dir, qb, existing,
                 retry=Retry(cfg, pr, sab, pp, slots, opts.unattended), earlier_dirs=earlier_dirs(cfg, sab, t))
    if rejected and cfg.cleanup and not opts.no_cleanup and not opts.dry_run:
        remove_rejected(rejected)
    if cfg.sab_delete_history:
        for nzo in nzos:
            sab.delete_history(nzo)
    return out


def execute_assemble(cfg: Config, opts: Options, torrent_file: str, sources: list[str]) -> dict:
    with open(torrent_file, "rb") as fh:
        data = fh.read()
    step(f"Reading {torrent_file}")
    t, path = save_torrent(cfg, data) if not opts.dry_run else (parse(data), torrent_file)
    dirs = [os.path.abspath(d) for d in sources]
    for d in dirs:
        if not os.path.exists(d):
            raise Abort(f"{d} does not exist")
    if not dirs and not (opts.fetch_missing or opts.dry_run):
        raise Abort("no folders given and downloading from Usenet is switched off - nothing to assemble from")
    output_dir = opts.output_dir or cfg.output_dir or (os.path.dirname(dirs[0]) if dirs else "")
    if not output_dir:
        raise Abort("no folders given: set where the torrent's files go (or the output folder in Settings)")
    qb = existing = None
    if not opts.no_qbit and not opts.dry_run:
        qb = QBittorrent(cfg.qbit_url, cfg.qbit_user, cfg.qbit_pass)
        existing = qbit_preflight(qb, t)
    # files the torrent has unpacked, still inside RAR sets here (a scene release next to a
    # torrent of its unpacked videos): unpacked onto the output disk, never into your folders
    work = None
    missing = asm.assemble(t, dirs, PROBE_DIR, dry_run=True, log=lambda *_: None).missing
    if missing and not opts.dry_run and any(archives.archive_sets(d) for d in dirs):
        work = os.path.join(output_dir, f".nzb2seed-unpacked-{t.infohash[:12]}")
        step(f"Unpacking {len(missing)} file(s) the torrent has unpacked from the RAR sets")
        info(f"into {work} (removed afterwards; your folders are not written to)")
        # an interrupted earlier run: keep what it unpacked in full, drop a cut-off file
        # (the unpacker never overwrites, so a partial file would otherwise stay)
        sizes = {f.length for f in t.real_files}
        for root_, _, names in os.walk(work):
            for n in names:
                p = os.path.join(root_, n)
                if os.path.getsize(p) not in sizes:
                    os.remove(p)
                    info(f"removed the cut-off {n} an interrupted run left")
        for d in dirs:
            left = asm.assemble(t, dirs + [work], PROBE_DIR, dry_run=True, log=lambda *_: None).missing
            if not left:
                break
            unpack_from_archives(d, left, dest=work)
    avail = dirs + ([work] if work else [])
    # what the folders do not have at all: from Usenet, like a build - only those files
    fetched, retry, pr = [], None, None
    missing = asm.assemble(t, avail, PROBE_DIR, dry_run=True, log=lambda *_: None).missing
    try:
        if missing and opts.fetch_missing and not opts.dry_run:
            sab = SABnzbd(cfg.sab_url, cfg.sab_key)
            # nzb2seed's own earlier downloads of the release first: what they hold is not
            # searched for or downloaded again
            disk_dirs, on_disk = found_on_disk(cfg, sab, t)
            got = [f for f in missing if f.relpath in on_disk]
            if got:
                info(f"already on disk in nzb2seed's earlier downloads: {len(got)} of the {len(missing)} "
                     f"file(s) the folders lack, {gb(sum(f.length for f in got))} - not looked for again")
                avail = avail + [d for d in disk_dirs if d not in avail]
                fetched = list(disk_dirs)
                missing = [f for f in missing if f.relpath not in on_disk]
        if missing and opts.fetch_missing and not opts.dry_run:
            step(f"Downloading the {len(missing)} file(s) the folders do not have from Usenet" if dirs
                 else f"No folders given: downloading all {len(missing)} file(s) from Usenet")
            for f in missing[:10]:
                info(f"needed: {f.relpath} ({gb(f.length)})")
            pr = grab.Source(cfg, Prowlarr(cfg.prowlarr_url, cfg.prowlarr_key), sab)
            grab.remove_stray_checks(sab)
            metadata.configure(cfg.flaresolverr_url, cfg.outbound_proxy)
            _this_build.torrent = t.infohash
            pp = choose_pp(opts, cfg, t)
            sab_preflight(sab, pp)
            have = {f.relpath for f in t.real_files} - {f.relpath for f in missing}
            new, _, units, _ = plan_and_fetch(cfg, opts, pr, sab, t, t.name, [], pp, have)
            fetched = fetched + [d for d in new if d not in fetched]
            retry = Retry(cfg, pr, sab, pp, units)
        elif missing and not opts.dry_run:
            info(f"{len(missing)} file(s) are in none of the folders; downloading them from Usenet is switched off")
        return finish(cfg, opts, t, path, avail + [d for d in fetched if d not in avail], output_dir, qb, existing,
                      sources_are_ours=False, keep_dirs=dirs, ours_dirs=fetched, retry=retry)
    finally:
        if pr:
            pr.close()
            metadata.close_session()
        if work:
            shutil.rmtree(work, ignore_errors=True)
