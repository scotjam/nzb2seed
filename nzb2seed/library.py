"""The Library tab: seed a TV library, season by season, from the trackers ticked on the tab.

Most of a library downloaded from Usenet (by Sonarr, or by hand) is in no torrent: it
earns nothing, and can't be hit-and-run either. This works through it one season at a
time. The tab keeps an order: "your files" (the library's own release, on any tracker
ticked) and the ticked trackers, each season taking the first of them that has a usable
pack - see ``order``:

* **The same release on a tracker** (its season pack has the files you already have,
  byte for byte): the library's files are hard-linked into the torrent's layout and
  rechecked - nothing is downloaded but the small files the library lacks (.nfo, Sample),
  and the library files are not changed.
* **Short of 100.0%** (a small file the library and Usenet lack): handled like any build
  that stops short - downloaded over BitTorrent only within that tracker's limit with 10% of it to
  spare, and when it is ticked to always add (Settings), otherwise looked for on the other trackers.
* **Already in the torrent client** (any season pack of it, from any tracker, in any
  folder): left alone - unless "redownload for this tracker" is ticked, or you ask for it
  on the season with "Download again for this tracker".
* **Another release only**: its season pack is built from Usenet like any other build,
  within a daily download budget. Once it seeds at 100.0%, the season is offered as a
  **swap** on the tab: the library's old files make way for hard links to the torrent's.
  A swap only ever happens when you approve it, and the old files are kept aside for
  ``library_keep_replaced_days`` before they are deleted.

Only single-season packs are used (an episode on its own earns next to nothing), and only
packs with every episode the library has of that season, so a swap never loses one.

What happened to each season is kept in ``library.json`` next to the config.
"""
from __future__ import annotations

import dataclasses
import os
import re
import shutil
import threading
import time
from collections import Counter
from dataclasses import dataclass, field

from . import clients
from . import torrent as torrent_mod
from .clients import ApiError, Prowlarr
from .config import tracker_key as config_key
from .report import Cancelled, check_cancel, info, step, warn

VIDEO = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".wmv", ".mov", ".mpg"}
POLL_SECONDS = 60
RESCAN_HOURS = 12
UNREACHABLE_RETRY = 15 * 60          # a season cut short by a service not answering waits this long


def unreachable(e: BaseException) -> bool:
    """qBittorrent, SABnzbd or Prowlarr not answering at all - a passing outage (a VPN
    reconnect, a container restart), not something wrong with the season."""
    import requests
    if isinstance(e, (requests.ConnectionError, requests.Timeout, ConnectionError, TimeoutError)):
        return True
    text = str(e)
    return any(x in text for x in ("Connection refused", "Max retries exceeded", "timed out",
                                   "Failed to establish a new connection"))


CHECKING_RETRY = 5 * 60


def still_checking(e: BaseException) -> bool:
    """The build stopped because its torrent is in qBittorrent being checked."""
    text = str(e)
    return "already in qBittorrent and active" in text and "checking" in text


TV_CATEGORIES = [5000]
REPLACED_DIR = ".nzb2seed-replaced"

_SEASON_DIR = re.compile(r"^(?:season|series|s)[\s._-]*0*(\d{1,3})$", re.I)
_EP = re.compile(r"(?<![a-z0-9])s(\d{1,3})[\s._-]*e(\d{1,4})", re.I)
_RES = re.compile(r"(?<![a-z0-9])(2160|1080|720|576|480)[pi](?![a-z0-9])", re.I)
_YEAR = re.compile(r"(?<![0-9])\(?((?:19|20)\d\d)\)?(?![0-9])")
_REMUX = re.compile(r"(?<![a-z])remux(?![a-z])", re.I)

# what a season can be: the order they show on the tab
WAITING, SEARCHING, NO_PACK, MATCHING, BUILDING, BUDGET = (
    "waiting", "searching", "no_pack", "matching", "building", "budget")
SEEDING, SWAP, SWAPPED, KEPT, FAILED, SKIPPED, SEEDED, SHORT = (
    "seeding", "swap", "swapped", "kept", "failed", "skipped", "already_seeded", "short")
IN_CLIENT = "in_client"             # qBittorrent has a pack of it already (another release or tracker)
SEEDED_STOPPED = "complete_stopped"  # the library's own files are a complete torrent's - stopped
STOPPED_STATES = ("stoppedUP", "pausedUP")
MOVING, MOVED, ORIGINAL_DELETED = "moving", "moved", "original_deleted"   # handed to cross-eyed (see CrossEyed)
CE_FAILED = {"failed", "rolled_back", "mirror_failed", "delete_interrupted"}
CE_WAITING = {"awaiting_user", "awaiting_permission"}
OPEN = {WAITING, BUDGET}            # still to be tried by the loop
FINISHED = {SEEDING, SWAP, SWAPPED, KEPT, MOVING, MOVED, ORIGINAL_DELETED}   # its torrent is complete

# ---------------------------------------------------------------- which trackers, in what order

OWN = "your files"                  # in the order: the library's own release, on any tracker ticked
MAX_TRACKERS = 50
DRY_RUN_WAIT = ("waiting - cross-eyed is in dry-run mode, so nothing is moved into the library; "
                "switch it to live and it is moved by itself")


def order(cfg) -> list[str]:
    """The Library's order: "your files" and the ticked trackers, first choice first.
    With one tracker (the default, ``library_indexer``): your files, then that tracker -
    as it always was. With several (``library_multi``): ``library_order``, where "your
    files" is at the top unless moved."""
    raw = list(getattr(cfg, "library_order", None) or []) if getattr(cfg, "library_multi", False) else []
    if not raw and getattr(cfg, "library_indexer", ""):
        raw = [OWN, cfg.library_indexer]
    out: list[str] = []
    for x in raw:
        x = str(x).strip()
        if x and x not in out and (x == OWN or config_key(x) not in {config_key(y) for y in out if y != OWN}):
            out.append(x)
    if not any(x != OWN for x in out):
        return []                       # no tracker ticked
    if OWN not in out:
        out.insert(0, OWN)
    return out[:MAX_TRACKERS + 1]


def trackers(cfg) -> list[str]:
    """The ticked trackers, first choice first."""
    return [x for x in order(cfg) if x != OWN]


def is_ours(cfg, name: str) -> bool:
    """Is ``name`` (as Prowlarr or qBittorrent names it) one of the ticked trackers?"""
    return bool(name) and config_key(name) in {config_key(t) for t in trackers(cfg)}


def trackers_text(cfg, joiner: str = " or ") -> str:
    names = trackers(cfg)
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + joiner + names[-1] if names else "no tracker"


def searched_on(o: dict | None) -> set[str]:
    """The tracker keys a season's search covered (an older offer: those of its order, or
    its one tracker)."""
    o = o or {}
    if "searched" in o:
        return set(o["searched"])
    names = o.get("order") or ([o["indexer"]] if o.get("indexer") else [])
    return {config_key(n) for n in names if n != OWN}


def offer_current(o: dict | None, cfg) -> bool:
    """Was this season's search made with the order the tab has now? (An offer from before
    there was a list names its one tracker.)"""
    if not o:
        return False
    was = o.get("order") or ([OWN, o["indexer"]] if o.get("indexer") else [])
    return was == order(cfg)


# ---------------------------------------------------------------- names

def key_of(text: str) -> str:
    """'All Show Foxtrot & Golf (2020)' -> 'all.show.foxtrot.and.golf.2020'"""
    t = (text or "").lower().replace("&", " and ").replace("'", "").replace("’", "")
    return re.sub(r"[^a-z0-9]+", ".", t).strip(".")


def resolution(name: str) -> str:
    m = _RES.search(name or "")
    return f"{m.group(1)}p" if m else ""


def group(name: str) -> str:
    from .pipeline import group_of
    stem = os.path.splitext(name)[0] if os.path.splitext(name)[1].lower() in VIDEO else name
    return (group_of(stem) or "").lower()


def show_part(name: str) -> str:
    """The show as a release names it: 'Alpha.2009.S04E05.Title...' -> 'alpha.2009'."""
    k = key_of(name)
    m = re.search(r"(?<![a-z0-9])s\d{1,3}(?:e\d{1,4})?(?![0-9])", k)
    return k[:m.start()].strip(".") if m else ""


def without_year(show: str) -> str:
    return re.sub(r"\.(?:19|20)\d\d$", "", show)


def pack_season(title: str, shows: set[str], strict: bool = True) -> int | None:
    """The season of a single-season pack of one of ``shows`` - None for an episode, a
    multi-season pack, or another show. A year in the title must be one the library's own
    names carry: 'No Show Hotel 2013' is the remake, not the 1986 show. ``strict=False``
    lets an unknown year through - for a pack whose files then prove it the same show."""
    k = key_of(title)
    years = {y for n in shows for y in re.findall(r"(?:^|\.)((?:19|20)\d\d)(?=\.|$)", n)}
    for show in sorted(shows, key=len, reverse=True):
        if not k.startswith(show + "."):
            continue
        rest = k[len(show) + 1:]
        y = re.match(r"((?:19|20)\d\d)\.", rest)
        if y:
            if strict and y.group(1) not in years:
                continue                                    # another show of that name
            rest = rest[y.end():]
        m = re.match(r"(?:s|season\.)0*(\d{1,3})(?![0-9])", rest)
        if not m:
            continue
        after = rest[m.end():]
        if (re.match(r"\.?e\d", after)                      # an episode
                or re.match(r"(?:-|\.to\.|\.)?s\d", after)    # S01-S02, S01.S02
                or re.match(r"-\d{1,3}(?![0-9p])", after)):    # S01-03
            return None
        return int(m.group(1))
    return None


# ---------------------------------------------------------------- the library on disk

@dataclass
class EpFile:
    path: str
    size: int
    episode: str            # "s04e05"
    inode: tuple            # (dev, ino)
    links: int


@dataclass
class Season:
    key: str                # the season folder (or "<show folder>#s04" for loose episodes)
    show: str               # the show folder's name
    show_dir: str
    season: int
    dir: str                # where its episodes live
    files: list[EpFile] = field(default_factory=list)

    @property
    def size(self) -> int:
        return sum(f.size for f in self.files)

    def names(self) -> set[str]:
        """The show as the folder and the files name it - what a pack's title is held to."""
        out = {key_of(self.show), key_of(_YEAR.sub("", self.show))}
        out |= {show_part(os.path.basename(f.path)) for f in self.files}
        return {n for n in out if n}

    def release(self) -> tuple[str, str]:
        """The (group, resolution) most of its episodes are."""
        c = Counter((group(os.path.basename(f.path)), resolution(os.path.basename(f.path))) for f in self.files)
        return c.most_common(1)[0][0] if c else ("", "")

    def query(self) -> str:
        names = Counter(show_part(os.path.basename(f.path)) for f in self.files)
        best = names.most_common(1)[0][0] if names and names.most_common(1)[0][0] else key_of(self.show)
        return f"{best.replace('.', ' ')} S{self.season:02d}"


_RELEASE_SHOW = re.compile(r"^(.+?)[\s._-]+(?:(?:19|20)\d\d[\s._-]+)?s\d{1,3}(?:[\s._-]*e\d{1,4}|[\s._-]*-[\s._-]*s?\d{1,3})?(?![0-9])", re.I)


def show_name(folder: str) -> str:
    """The show a folder holds, by name: a release's folder lying in the library's top
    ('Show.Bravo.S06E01.1080p.WEB.x265-YLLSpAL', 'Show.Delta.S01-S07.COMPLETE...')
    is named after its show ('Show Bravo'); a show's own folder is left as it is."""
    m = _RELEASE_SHOW.match(folder)
    if not m:
        return folder
    name = re.sub(r"[._]+", " ", m.group(1)).strip()
    y = re.search(r"[\s._(-]((?:19|20)\d\d)[\s._-]+s\d", folder, re.I)
    if y and y.group(1) not in name:
        name += f" ({y.group(1)})"
    return name or folder


def scan(roots: list[str]) -> list[Season]:
    """Every season in the library: <root>/<show>/<Season N>/... , and episodes lying
    directly in a show's folder (grouped by their SxxEyy)."""
    out: list[Season] = []
    for root in roots:
        try:
            shows = sorted(os.listdir(root), key=str.lower)
        except OSError:
            continue
        for show in shows:
            show_dir = os.path.join(root, show)
            if show.startswith(".") or not os.path.isdir(show_dir):
                continue
            loose: dict[int, Season] = {}
            show = show_name(show)
            for entry in sorted(os.listdir(show_dir)):
                p = os.path.join(show_dir, entry)
                m = _SEASON_DIR.match(entry)
                if os.path.isdir(p) and m:
                    s = Season(p, show, show_dir, int(m.group(1)), p, _episodes(p, int(m.group(1))))
                    if s.files:
                        out.append(s)
                elif os.path.isfile(p):
                    f = _ep_file(p)
                    if f:
                        n = int(f.episode[1:f.episode.index("e")])
                        loose.setdefault(n, Season(f"{show_dir}#s{n:02d}", show, show_dir, n, show_dir)).files.append(f)
            out += [loose[n] for n in sorted(loose)]
    return out


def _ep_file(path: str) -> EpFile | None:
    if os.path.splitext(path)[1].lower() not in VIDEO or "sample" in os.path.basename(path).lower():
        return None
    m = _EP.search(os.path.basename(path))
    if not m:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    return EpFile(path, st.st_size, f"s{int(m.group(1)):02d}e{int(m.group(2)):02d}", (st.st_dev, st.st_ino), st.st_nlink)


def _episodes(d: str, season: int) -> list[EpFile]:
    out = []
    for root, dirs, names in os.walk(d):
        dirs[:] = [x for x in dirs if not x.startswith(".")]
        for n in sorted(names):
            f = _ep_file(os.path.join(root, n))
            if f and int(f.episode[1:f.episode.index("e")]) == season:
                out.append(f)
    return out


def seeded(s: Season, in_client: set[str], inodes: set | None = None) -> bool:
    """Is every episode already in a torrent? A file lying inside a torrent's own folder
    counts, and so does one hard-linked to a torrent's copy. ``inodes``: (device, inode)
    of every file of the complete torrents qBittorrent lists - a hard link counts only
    when its other name is one of those (a leftover build folder no torrent uses does
    not). None: not known, any second name counts."""
    def one(f: EpFile) -> bool:
        if f.links > 1:
            if inodes is None:
                return True
            try:
                st = os.stat(f.path)
            except OSError:
                return False
            if (st.st_dev, st.st_ino) in inodes:
                return True
        p = os.path.normcase(os.path.abspath(f.path))
        return any(p == c or p.startswith(c + os.sep) for c in in_client)
    return bool(s.files) and all(one(f) for f in s.files)


def held_seasons(title: str, shows: set[str]) -> set[int]:
    """The seasons of one of ``shows`` a torrent called ``title`` holds whole: a season pack,
    or a multi-season pack (S01-S03). An episode holds none. A year in the title must be
    one the library names - unless the library names none."""
    k = key_of(title)
    years = {y for n in shows for y in re.findall(r"(?:^|\.)((?:19|20)\d\d)(?=\.|$)", n)}
    for show in sorted(shows, key=len, reverse=True):
        if not k.startswith(show + "."):
            continue
        rest = k[len(show) + 1:]
        y = re.match(r"((?:19|20)\d\d)\.", rest)
        if y:
            if years and y.group(1) not in years:
                continue                                    # another show of that name
            rest = rest[y.end():]
        m = re.match(r"(?:s|season\.)0*(\d{1,3})(?![0-9])", rest)
        if not m:
            continue
        a, after = int(m.group(1)), rest[m.end():]
        if re.match(r"\.?e\d", after):
            return set()                                    # an episode
        r = re.match(r"(?:-s?|\.to\.s?|\.s)0*(\d{1,3})(?![0-9p])", after)
        b = int(r.group(1)) if r else a
        return set(range(a, b + 1)) if b >= a else {a}
    return set()


def holders(s: Season, packs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The (torrent name, tracker) of every torrent in the client holding season ``s``."""
    names = s.names()
    return [(n, t) for n, t in packs if s.season in held_seasons(n, names)]


# ---------------------------------------------------------------- the tracker's packs

@dataclass
class Pick:
    release: clients.Release
    same: bool              # the same group and resolution as the library's season
    unsure: bool = False    # carries a year the library never names: only usable if identical


def candidates(s: Season, results: list[clients.Release]) -> list[Pick]:
    """The tracker's single-season packs of this season, best first: the library's own
    release (to link rather than download), then the library's resolution, then the size
    nearest the library's (a remux is not a stand-in for an encode), then most seeders."""
    shows = s.names()
    grp, res = s.release()
    remux = any(_REMUX.search(os.path.basename(f.path)) for f in s.files)
    out = []
    for r in results:
        if r.protocol != "torrent" or pack_season(r.title, shows, strict=False) != s.season:
            continue
        unsure = pack_season(r.title, shows) != s.season
        if bool(_REMUX.search(r.title)) != remux:
            continue
        same = bool(grp) and group(r.title) == grp and resolution(r.title) == res
        if unsure and not same:
            continue                    # can only ever be used as the library's own release
        out.append(Pick(r, same, unsure))

    def rank(p: Pick):
        r = p.release
        return (not p.same, resolution(r.title) != res,
                abs((r.size or 0) - s.size) / max(s.size, 1), -(r.seeders or 0))
    return sorted(out, key=rank)


def covers(t: torrent_mod.Torrent, s: Season) -> list[str]:
    """Episodes of the season the library has and the torrent does not ([] = it has all)."""
    have = set()
    for f in t.real_files:
        m = _EP.search(f.name)
        if m and os.path.splitext(f.name)[1].lower() in VIDEO:
            have.add(f"s{int(m.group(1)):02d}e{int(m.group(2)):02d}")
    return sorted({f.episode for f in s.files} - have)


def identical(t: torrent_mod.Torrent, s: Season) -> bool:
    """Does the library hold this torrent's videos already - every one of them, by size?
    Equal sizes for every episode of a release are as good as the same files; the recheck
    that follows is the proof."""
    sizes = Counter(f.size for f in s.files)
    vids = [f for f in t.real_files if os.path.splitext(f.name)[1].lower() in VIDEO
            and "sample" not in f.relpath.lower()]
    if not vids:
        return False
    want = Counter(f.length for f in vids)
    return all(sizes[n] >= k for n, k in want.items())


# ---------------------------------------------------------------- swapping files in

def swap_plan(t: torrent_mod.Torrent, torrent_root: str, s: Season) -> list[tuple[list[str], str, str]]:
    """[(library files to set aside, torrent file, where its link goes)] - per episode the
    torrent has. An episode whose library file IS the torrent's (one file, two names) needs
    nothing."""
    by_ep: dict[str, list[EpFile]] = {}
    for f in s.files:
        by_ep.setdefault(f.episode, []).append(f)
    plan = []
    for f in t.real_files:
        m = _EP.search(f.name)
        if not m or os.path.splitext(f.name)[1].lower() not in VIDEO or "sample" in f.relpath.lower():
            continue
        ep = f"s{int(m.group(1)):02d}e{int(m.group(2)):02d}"
        src = os.path.join(torrent_root, *f.parts[1:]) if t.multi_file else torrent_root
        try:
            st = os.stat(src)
        except OSError:
            continue
        old = by_ep.get(ep, [])
        if any(o.inode == (st.st_dev, st.st_ino) for o in old):
            continue
        aside = []
        for o in old:
            stem = os.path.splitext(os.path.basename(o.path))[0]
            folder = os.path.dirname(o.path)
            aside += [os.path.join(folder, n) for n in sorted(os.listdir(folder))
                      if n == os.path.basename(o.path) or (n.startswith(stem + ".") and os.path.isfile(os.path.join(folder, n)))]
        plan.append((sorted(set(aside)), src, os.path.join(s.dir, f.name)))
    return plan


def files_of(paths: set[str], dev: int) -> set:
    """(device, inode) of every file under ``paths`` (torrents' content) on drive ``dev``."""
    out = set()
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            continue
        if st.st_dev != dev:
            continue
        if not os.path.isdir(p):
            out.add((st.st_dev, st.st_ino))
            continue
        for root, _, names in os.walk(p):
            for n in names:
                try:
                    st = os.stat(os.path.join(root, n))
                except OSError:
                    continue
                out.add((st.st_dev, st.st_ino))
    return out


def moves_done(jobs: list[dict]) -> bool:
    """cross-eyed's jobs of a folder move, but for its last step (removing the old folder):
    all done - the torrent repointed and rechecked in its new folder."""
    return all(j.get("state") == "done" for j in jobs if j.get("job_type") != "folder_finalize")


def torrent_label(r: dict | None, unknown: bool = False) -> str:
    """What qBittorrent is doing with a torrent, in a few words."""
    if unknown:
        return "qBittorrent not answering"
    if not r:
        return "not in qBittorrent"
    st, pct = r.get("state") or "", f"{(r.get('progress') or 0) * 100:.1f}%"
    if st in ("uploading", "stalledUP", "forcedUP", "queuedUP"):
        return "seeding"
    if st in STOPPED_STATES:
        return "stopped (complete)"
    if st.startswith("checking") or st == "moving":
        return f"checking ({pct})" if st != "moving" else "moving its files"
    if st in ("stoppedDL", "pausedDL"):
        return f"stopped at {pct}"
    if st in ("downloading", "stalledDL", "forcedDL", "queuedDL", "metaDL", "forcedMetaDL"):
        return f"downloading ({pct})"
    if st == "missingFiles":
        return "files missing"
    return st or "unknown"


def seeded_from(plan, in_client: set[str]) -> list[str]:
    """The files a swap would set aside that a torrent in the client seeds from, in place
    (its folder is in the library). Moving one would break that torrent. (A file that is
    only hard-linked to a torrent's copy is fine: the torrent keeps its own name.)"""
    out = []
    for aside, _src, dst in plan:
        for p in [*aside, dst]:
            n = os.path.normcase(os.path.abspath(p))
            if any(n == c or n.startswith(c + os.sep) for c in in_client):
                out.append(p)
    return out


def replaced_dir(cfg, s: Season) -> str:
    root = next((r for r in cfg.library_roots if os.path.abspath(s.dir).startswith(os.path.abspath(r))), s.show_dir)
    rel = os.path.relpath(s.dir, os.path.dirname(os.path.abspath(root)))
    return os.path.join(os.path.dirname(os.path.abspath(root)), REPLACED_DIR, time.strftime("%Y%m%d"), rel)


def do_swap(cfg, t: torrent_mod.Torrent, torrent_root: str, s: Season) -> tuple[int, int]:
    """Set the old files aside and link the torrent's into their place. Nothing is deleted
    here - the old files wait in the replaced folder. Returns (episodes, files set aside)."""
    plan = swap_plan(t, torrent_root, s)
    for _aside, src, dst in plan:
        if os.stat(src).st_dev != os.stat(s.dir).st_dev:
            raise ValueError(f"{src} is on another disk than the library - a hard link is not possible")
    holding = replaced_dir(cfg, s)
    moved = 0
    for aside, src, dst in plan:
        for p in aside:
            target = os.path.join(holding, os.path.relpath(p, s.dir))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            os.replace(p, target)
            moved += 1
        if os.path.lexists(dst):
            os.replace(dst, os.path.join(holding, os.path.basename(dst) + ".in-the-way"))
            moved += 1
        os.link(src, dst)
    return len(plan), moved


def purge_replaced(cfg, now: float | None = None) -> int:
    """Delete what was set aside longer ago than library_keep_replaced_days."""
    days = float(cfg.library_keep_replaced_days or 0)
    if days <= 0:
        return 0
    cutoff = time.strftime("%Y%m%d", time.localtime((now or time.time()) - days * 86400))
    gone = 0
    for root in cfg.library_roots:
        base = os.path.join(os.path.dirname(os.path.abspath(root)), REPLACED_DIR)
        try:
            dated = os.listdir(base)
        except OSError:
            continue
        for d in dated:
            if re.fullmatch(r"\d{8}", d) and d < cutoff:
                shutil.rmtree(os.path.join(base, d), ignore_errors=True)
                gone += 1
    return gone


# ---------------------------------------------------------------- state

class State:
    """library.json: {"seasons": {key: {...}}, "spent": [[time, bytes], ...]}"""

    def __init__(self, path: str):
        import json
        self.path = path
        self.lock = threading.Lock()
        self.on_save = None
        try:
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            d = {}
        self.seasons: dict[str, dict] = d.get("seasons", {})
        self.spent: list = d.get("spent", [])
        self.scanned_at: float = d.get("scanned_at", 0)
        for it in self.seasons.values():
            if it.get("status") in (SEARCHING, MATCHING, BUILDING):     # cut off by a restart
                it["status"] = WAITING
            elif it.get("status") == MOVING and not it.get("batch"):   # handing over to cross-eyed
                it.update(status=SWAP, move_started=None, why="ready to move - a restart cut off handing it to cross-eyed")

    def save(self):
        import json
        with self.lock:
            tmp = f"{self.path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"seasons": self.seasons, "spent": self.spent, "scanned_at": self.scanned_at}, fh, indent=1)
            os.replace(tmp, self.path)
        if self.on_save:
            self.on_save()

    def update(self, key: str, **kw):
        with self.lock:
            self.seasons.setdefault(key, {}).update(kw, updated=time.time())
        self.save()

    def spent_today(self, now: float | None = None) -> int:
        now = now or time.time()
        with self.lock:
            self.spent = [x for x in self.spent if now - x[0] < 86400]
            return sum(b for _, b in self.spent)

    def spend(self, n: int):
        with self.lock:
            self.spent.append([time.time(), int(n)])
        self.save()


# ---------------------------------------------------------------- the worker

class Library:
    def __init__(self, app, state_path: str):
        self.app = app
        self.state = State(state_path)
        self.seasons: dict[str, Season] = {}
        self.current: str | None = None
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.ce_dry: bool | None = None     # cross-eyed's dry-run mode, as last asked (None: not known)
        threading.Thread(target=self._loop, name="library", daemon=True).start()

    def _loop(self):
        while not self.stop.is_set():
            try:
                self.poll()
            except Exception as e:              # never let the worker die
                print(f"library: {type(e).__name__}: {e}", flush=True)
            hk = getattr(self.app, "hook", None)
            if hk is not None:
                try:
                    hk.tick(self.busy())        # held cross-seed calls run once it is idle
                except Exception as e:          # noqa: BLE001
                    print(f"hook: {type(e).__name__}: {e}", flush=True)
            self.wake.wait(POLL_SECONDS)
            self.wake.clear()

    def busy(self) -> bool:
        """Switched on and building or moving a season: the disk is in use, so qBittorrent's
        on-finished calls (cross-seed) are held back - see hook.py."""
        cfg = self.app.cfg
        if not cfg.library_enabled or not getattr(cfg, "library_hold_hook", True):
            return False
        return bool(self.current) or any(it.get("status") in (SEARCHING, MATCHING, BUILDING, MOVING)
                                         for it in self.state.seasons.values())

    # -- scanning
    def client(self):
        """What qBittorrent holds: (the folders its torrents are in, as this machine sees
        them; [(torrent name, tracker)]) - None when it cannot be reached."""
        from .pathmap import map_path
        from .pipeline import tracker_name
        cfg = self.app.cfg
        back = [(b, a) for a, b in (cfg.local_to_qbit or [])]
        try:
            rows = self.app._qbit().torrents()
        except (ApiError, OSError, AttributeError):
            return None
        pr = Prowlarr(cfg.prowlarr_url, cfg.prowlarr_key) if cfg.prowlarr_url else None
        paths = {os.path.normcase(os.path.abspath(map_path(r.get("content_path") or "", back)))
                 for r in rows if r.get("content_path")}
        # the complete ones (100.0%, not missing their files): what "already seeded" means
        def done(r):
            return r.get("content_path") and (r.get("progress") or 0) >= 1 and r.get("state") not in ("missingFiles", "error")
        self._complete = {os.path.normcase(os.path.abspath(map_path(r["content_path"], back))) for r in rows if done(r)}
        self._seeding = {os.path.normcase(os.path.abspath(map_path(r["content_path"], back))) for r in rows
                         if done(r) and r.get("state") not in STOPPED_STATES}
        packs = [(r.get("name") or "", tracker_name([r.get("tracker")], pr) if r.get("tracker") else "")
                 for r in rows]
        return paths, packs

    def in_client(self) -> set[str]:
        c = self.client()
        return c[0] if c else set()

    def client_status(self, s: Season, held, cfg) -> tuple[str, str]:
        """WAITING, SEEDED (the library's own files are in a torrent) or IN_CLIENT (a pack
        of it is, another copy) - and why."""
        paths, packs = held
        complete = getattr(self, "_complete", None)
        if complete is None:                    # not known: any torrent's folder counts
            if seeded(s, paths):
                return SEEDED, ""
        elif seeded(s, self._seeding, getattr(self, "_inodes_seeding", None)):
            return SEEDED, "seeding in qBittorrent"
        elif seeded(s, complete, getattr(self, "_inodes", None)):
            return SEEDED_STOPPED, "complete in qBittorrent, but stopped - start it there to seed it"
        have = holders(s, packs)
        if not have:
            return WAITING, ""
        froms = sorted({t or "an unknown tracker" for _, t in have}, key=str.lower)
        mine = any(is_ours(cfg, t) for _, t in have)
        if cfg.library_redownload and not mine:
            return WAITING, (f"in qBittorrent from {', '.join(froms)} - downloaded again for "
                             f"{trackers_text(cfg)}, as ticked")
        names = sorted({n for n, _ in have})
        return IN_CLIENT, (f"in qBittorrent already ({'; '.join(names[:3])}{' ...' if len(names) > 3 else ''}"
                           f" - from {', '.join(froms)})")

    def rescan(self):
        cfg = self.app.cfg
        found = scan(cfg.library_roots)
        asked = self.client()
        held = asked or (set(), [])
        self._inodes = None
        if asked is not None and found:
            try:
                dev = os.stat(found[0].dir).st_dev
                self._inodes = files_of(getattr(self, "_complete", set()), dev)
                self._inodes_seeding = files_of(getattr(self, "_seeding", set()), dev)
            except OSError:
                pass
        self.seasons = {s.key: s for s in found}
        with self.state.lock:
            for s in found:
                it = self.state.seasons.setdefault(s.key, {"status": WAITING})
                it.update(show=s.show, season=s.season, dir=s.dir, episodes=len(s.files), size=s.size,
                          release="-".join(x for x in s.release() if x))
                if it.get("status") in (WAITING, SEEDED, SEEDED_STOPPED, IN_CLIENT) and not it.get("again"):
                    if self._own_build(s, it) is not False:
                        continue                # its own torrent: settled, or built again
                    if asked is None:
                        continue                # qBittorrent did not answer: left as it was
                    it["status"], it["why"] = self.client_status(s, held, cfg)
            for it in self.state.seasons.values():
                if _SEASON_DIR.match(it.get("show") or "") and it.get("dir"):
                    it["show"] = os.path.basename(os.path.dirname(it["dir"].rstrip("/"))) or it["show"]
            for s in found:
                it = self.state.seasons[s.key]
                if it.get("status") == SHORT and it.get("infohash") and it.get("root"):
                    self._settle(s, it)
                elif it.get("status") == FAILED and it.get("infohash"):
                    self._own_build(s, it)      # failed, yet its torrent finished at 100.0%
            for k in [k for k, it in self.state.seasons.items() if k not in self.seasons
                      and it.get("status") in (WAITING, SEEDED, SEEDED_STOPPED, NO_PACK, BUDGET)]:
                self.state.seasons.pop(k)          # the folder is gone
            self.state.scanned_at = time.time()
            finished = {f"library: {it.get('show')} S{int(it['season']):02d}": it.get("status")
                        for it in self.state.seasons.values()
                        if it.get("status") in FINISHED and it.get("season") is not None}
        self.state.save()
        self.close_cut_off_jobs(finished)

    CUT_OFF = "finished after nzb2seed restarted"

    def close_cut_off_jobs(self, finished: dict[str, str]):
        """A season's jobs a restart cut off, once the season got there anyway (its torrent
        settled when nzb2seed was back, or a later job built it): they are only copies of
        one outcome. Any other job of the season stays and they go; with none, the newest
        stays - marked done - and the rest go. ``finished``: {job title: the season's status}."""
        jobs = getattr(self.app, "jobs", None)
        if not jobs or not finished:
            return
        cut = lambda j: j.status == "interrupted" or (j.status == "done" and (j.result or "").startswith(self.CUT_OFF))
        lock = getattr(self.app, "lock", None) or threading.Lock()
        changed = False
        with lock:
            by_title: dict[str, list] = {}
            for j in jobs.values():
                if j.title in finished:
                    by_title.setdefault(j.title, []).append(j)
            for title_, js in by_title.items():
                gone = [j for j in js if cut(j) and j.status in ("interrupted", "done")]
                if not gone:
                    continue
                if len(gone) == len(js):              # nothing else records it: the newest stays
                    keep = max(gone, key=lambda j: j.id)
                    gone.remove(keep)
                    if keep.status != "done":
                        keep.status = "done"
                        keep.result = (f"{self.CUT_OFF} - the Library tab has it as "
                                       + finished[title_].replace("_", " "))
                        changed = True
                for j in gone:
                    jobs.pop(j.id, None)
                    changed = True
        if changed and getattr(self.app, "store", None):
            self.app.store.changed(urgent=True)

    def _own_build(self, s: Season, it: dict) -> bool | None:
        """A season whose own build is in qBittorrent already - a build a restart cut off
        after adding its torrent: at 100.0% it is settled (and started) like any finished
        build; short of it, it is built again (the torrent and its files are reused). Its
        own torrent is never a reason to call the season "in the client". True: settled
        here; None: its own torrent, short - to be built again; False: none of its own."""
        h = it.get("infohash")
        if not h or it.get("status") not in (WAITING, IN_CLIENT, SEEDED, SEEDED_STOPPED, FAILED):
            return False
        if not it.get("root"):                  # recorded at the end of a build: a cut-off one has none
            out = output_dir(self.app.cfg)
            try:
                t = self._torrent(h)
            except ValueError:
                return False
            if not out:
                return False
            it["root"] = torrent_mod_root(t, out)
        try:
            info_ = self.app._qbit().info(h) or {}
        except (ApiError, OSError, AttributeError):
            return False
        if not info_:
            return False
        if it.get("status") == FAILED and (info_.get("progress", 0) < 1 or "checking" in (info_.get("state") or "")):
            return False                        # failed for real: Try again is yours to press
        if info_.get("progress", 0) < 1:
            if it.get("status") != WAITING:
                it.update(status=WAITING, why="its torrent is in qBittorrent short of 100.0% - built again")
            return None
        self._settle(s, it, info_)
        return True

    def _settle(self, s: Season, it: dict, info_: dict | None = None):
        """A season that stopped short: once its torrent is complete in qBittorrent (the rest
        came over BitTorrent), it is seeding - or ready to swap, if its files are not the
        library's. (Held under the state lock by rescan.)"""
        if info_ is None:
            try:
                info_ = self.app._qbit().info(it["infohash"]) or {}
            except (ApiError, OSError, AttributeError):
                return
        if info_.get("progress", 0) < 1:
            return
        if info_.get("state") in ("stoppedUP", "pausedUP") and getattr(self.app.cfg, "start_when_complete", False):
            try:
                self.app._qbit().start(it["infohash"])      # "start seeding after a 100% recheck"
            except (ApiError, OSError):
                pass
        try:
            t = self._torrent(it["infohash"])
        except ValueError:
            return
        if swap_plan(t, it["root"], s):
            it.update(status=SWAP, why="seeding at 100.0% - swap the library's files for the torrent's?")
        else:
            it.update(status=SEEDING, why="seeding from the library's own files")

    # -- the next season
    def poll(self):
        cfg = self.app.cfg
        if not cfg.library_roots:
            return
        if not self.seasons or time.time() - self.state.scanned_at > RESCAN_HOURS * 3600:
            self.rescan()
        purge_replaced(cfg)
        self.check_dry_run(cfg)
        self.follow_moves(cfg)
        self.auto_moves(cfg)
        if not cfg.library_enabled or not trackers(cfg) or self.current:
            return
        try:
            if self.app.guard().holding():
                return                          # the free-space guard is holding downloads back
        except (OSError, ValueError):
            pass
        self.reopen_no_pack(cfg)
        budget_left = float(cfg.library_daily_gb or 0) * 1024 ** 3 - self.state.spent_today()
        held = None
        for key in sorted(self.seasons, key=lambda k: (self.seasons[k].show.lower(), self.seasons[k].season)):
            it = self.state.seasons.get(key, {})
            if it.get("status") == BUDGET:
                # waits until what it needs is free - not started (and stopped) over and over
                need = it.get("need") or (it.get("offer") or {}).get("size") or 0
                if budget_left <= 0 or need > budget_left:
                    continue
            if it.get("status") in OPEN and (it.get("retry_at") or 0) > time.time():
                continue                        # a service did not answer: waits a little
            if it.get("status") in OPEN:
                own = self._own_build(self.seasons[key], it)
                if own:
                    self.state.save()
                    continue
                if own is None:                 # its own torrent, short of 100%: build it again
                    self.start(key)
                    return
                if not it.get("again"):
                    # added to qBittorrent since the last scan? then it is left alone
                    held = held or self.client()
                    if held is None:
                        return                  # qBittorrent cannot be reached: try later
                    status, why = self.client_status(self.seasons[key], held, cfg)
                    if status != WAITING:
                        self.state.update(key, status=status, why=why)
                        continue
                self.start(key)
                return



    def reopen_no_pack(self, cfg) -> int:
        """Seasons none of the trackers had go back in the queue once a tracker their
        search did not cover is ticked - not otherwise (nothing changed: nothing to find)."""
        now = {config_key(n) for n in trackers(cfg)}
        n = 0
        one = getattr(cfg, "library_indexer", "")
        for key, it in list(self.state.seasons.items()):
            if it.get("status") != NO_PACK:
                continue
            if not it.get("offer") and one:
                # from before searches were recorded, when there was only ever the one tracker
                it["offer"] = {"searched": [config_key(one)], "none": True}
            if now - searched_on(it.get("offer")):
                self.state.update(key, status=WAITING, offer=None,
                                  why="a tracker was ticked since its search - searched again")
                n += 1
        if n:
            info(f"{n} season(s) with no pack go back in the queue for the newly ticked tracker(s)")
        return n

    def start(self, key: str):
        s = self.seasons[key]
        self.current = key
        title = f"library: {s.show} S{s.season:02d}"
        done = threading.Event()

        def fn(cfg):
            from . import inbox
            inbox._auto.on = True          # its searches count against the automatic budget
            try:
                return self.run(cfg, key)
            finally:
                inbox._auto.on = False
                self.current = None
                done.set()
                self.wake.set()
        self.app.start_job(title, "library", fn)

    def run(self, cfg, key: str) -> dict:
        from .pipeline import Abort, Incomplete, Options
        s = self.seasons[key]
        st = self.state
        step(f"{s.show}, season {s.season}: {len(s.files)} episode(s), {s.size / 1024 ** 3:.1f} GB "
             f"in {s.dir}")
        grp, res = s.release()
        info(f"the library's release: {grp or '?'} {res or ''}".rstrip())
        out_dir = output_dir(cfg)
        if not out_dir:
            raise ValueError("choose where the torrents go (Library tab) - the usual torrents folder "
                             "is not on the library's drive")
        chosen = self.kept_pick(cfg, key)
        if chosen:
            info(f"{chosen[0].release.indexer}'s pack, as found before: {chosen[0].release.title}")
        else:
            st.update(key, status=SEARCHING, why="")
            chosen = self.look(cfg, key)
        if chosen is None:
            o = st.seasons[key].get("offer") or {}
            st.update(key, status=NO_PACK, why=o.get("why") or f"{trackers_text(cfg)} has no usable season pack of it")
            return {"result": "no usable season pack", "extra": {"nothing_built": True}}
        p, data, t, same = chosen
        opts = Options(unattended=True, start=True, fetch_missing=True, output_dir=out_dir)
        while True:
            try:
                out = self._make(cfg, key, opts, p, data, t, same, s, out_dir)
            except Incomplete:
                raise
            except Abort as e:
                nxt = None if same else self.next_pack(cfg, key, p, t, e)
                if nxt is None:
                    if not (unreachable(e) or still_checking(e)):
                        st.update(key, status=FAILED, why=str(e))
                    raise
                p, data, t, same = nxt
                continue
            break
        if out is None:
            return {"result": "waiting for the daily download budget", "extra": {"nothing_built": True}}
        root = torrent_mod_root(t, out_dir)
        plan = swap_plan(t, root, s)
        if plan and seeded_from(plan, self.in_client()):
            st.update(key, status=SEEDING, root=root, why="seeding at 100.0% - the library's files stay as they are, "
                      "as another torrent seeds from them")
        elif plan:
            st.update(key, status=SWAP, root=root, why="seeding at 100.0% - swap the library's files for the torrent's?")
        else:
            st.update(key, status=SEEDING, root=root, why="seeding from the library's own files")
        return out

    def next_pack(self, cfg, key: str, p, t, e):
        """A pack Usenet cannot supply, and that BitTorrent would not finish by itself (its
        tracker not ticked always add, no seeders, or more missing than its limit - else the
        build stops nearly complete instead) hands over to the season's next usable pack, if
        a ticked tracker has one. That pack is not tried again on a later run either."""
        from . import pipeline
        if not str(e).startswith("no Usenet post could supply"):
            return None
        skip = set(self.state.seasons.get(key, {}).get("no_usenet") or []) | {t.infohash}
        self.state.update(key, no_usenet=sorted(skip))
        if not any(j.get("torrent") == t.infohash for j in pipeline.ledger_for(cfg).all()):
            self.state.spend(-t.total_size)     # nothing was downloaded for it
        warn(f"{p.release.title}: {e} - trying the season's next pack")
        return self.look(cfg, key, skip)

    def _make(self, cfg, key, opts, p, data, t, same, s, out_dir):
        """Link or build one pack: what the builder returns, None while over the budget."""
        from . import pipeline
        from .pipeline import Abort, Incomplete, execute_assemble
        st = self.state
        tmp = kept_torrent(cfg, t.infohash)
        st.update(key, torrent=p.release.title, infohash=t.infohash, tracker=p.release.indexer)
        try:
            if same:
                step(f"Linking the library's files into {p.release.title}")
                st.update(key, status=MATCHING)
                # what a build of a searched release records: the tracker's limits and the
                # other-tracker lookup go by these when it stops short
                pipeline._this_build.tracker = p.release.indexer or ""
                pipeline._this_build.seeders = p.release.seeders if (p.release.seeders or 0) >= 0 else None
                out = execute_assemble(cfg, opts, tmp, [s.dir])
            else:
                out = self.build(cfg, opts, key, t, data, p)
        except Cancelled:
            st.update(key, status=WAITING, why="cancelled")
            raise
        except Incomplete as e:
            # the job runner decides what happens next, as for any build that stops short
            st.update(key, status=SHORT, root=torrent_mod_root(t, out_dir),
                      why=f"{pipeline.exact_pct(e.have)} here - the rest goes over BitTorrent only within "
                          f"{p.release.indexer}'s limit with 10% of it to spare, and if it is ticked to always add; otherwise "
                          "it is looked for on your other trackers (see its job)")
            raise
        except (Abort, ApiError, OSError, ValueError) as e:
            if unreachable(e):
                # tried again by itself: what was built is reused, nothing is searched again
                st.update(key, status=WAITING, retry_at=time.time() + UNREACHABLE_RETRY,
                          why=f"a service did not answer ({e}) - tried again by itself in "
                              f"{UNREACHABLE_RETRY // 60} minutes")
            elif still_checking(e):
                # its own torrent, added on an earlier try, is being rechecked: once that is
                # done it is settled (and started) as a finished build - see _own_build
                st.update(key, status=WAITING, retry_at=time.time() + CHECKING_RETRY,
                          why="its torrent is being checked in qBittorrent - looked at again "
                              f"in {CHECKING_RETRY // 60} minutes")
            elif not isinstance(e, Abort):
                st.update(key, status=FAILED, why=str(e))
            raise                               # an Abort: run() decides (the next pack, or failed)
        return out

    def look(self, cfg, key: str, skip=frozenset()):
        """The season's one search of the ticked trackers (one Prowlarr search for all of
        them): the pack it would use - (pick, .torrent bytes, torrent, the library's very
        files?) or None - kept, with its .torrent saved, as the season's "offer". Packs in
        ``skip`` (infohashes no Usenet post supplied) are passed over. The tab's
        order decides: "your files" takes the library's own release from any ticked tracker;
        a tracker takes its own best usable pack. The first of them with one wins."""
        s = self.seasons[key]
        q = s.query()
        names = trackers(cfg)
        info(f"searching {', '.join(names)} for \"{q}\"")
        pr = Prowlarr(cfg.prowlarr_url, cfg.prowlarr_key)
        ids = self.indexer_ids(cfg)
        found = [p for p in candidates(s, pr.search(q, list(ids.values()), TV_CATEGORIES))
                 if config_key(p.release.indexer) in ids]
        base = {"order": order(cfg), "searched": sorted(ids), "at": time.time()}
        if not found:
            self.state.update(key, offer=dict(base, none=True, why=f"{trackers_text(cfg)} has no single-season pack of it"))
            return None
        for p in found[:8]:
            info(f"  {'same release ' if p.same else ''}{p.release.title} ({p.release.indexer}, "
                 f"{p.release.size / 1024 ** 3:.1f} GB, {p.release.seeders} seeders)")
        fetched: dict = {}

        def get(p: Pick):
            """(.torrent bytes, torrent) of a pack with every episode the library has, else None."""
            k = (config_key(p.release.indexer), p.release.guid or p.release.title)
            if k not in fetched:
                check_cancel()
                fetched[k] = None
                try:
                    data = pr.fetch(p.release)
                    t = torrent_mod.parse(data)
                except (ApiError, OSError, ValueError, torrent_mod.TorrentError) as e:
                    warn(f"could not get {p.release.title} ({p.release.indexer}): {e}")
                    return None
                if t.infohash in skip:
                    info(f"{p.release.title} ({p.release.indexer}): no Usenet post supplied it before - not used")
                    return None
                lacking = covers(t, s)
                if lacking:
                    info(f"{p.release.title} ({p.release.indexer}) lacks {', '.join(lacking[:6])} - not used")
                    return None
                fetched[k] = (data, t)
            return fetched[k]

        def own_files():
            for p in found[:6]:                 # ranked: the library's group and resolution first
                got = get(p)
                if got and identical(got[1], s):
                    return p, got[0], got[1], True
            return None

        def best_on(tracker: str):
            chosen = None
            for p in [p for p in found if config_key(p.release.indexer) == config_key(tracker)][:6]:
                got = get(p)
                if not got:
                    continue
                if identical(got[1], s):
                    return p, got[0], got[1], True
                if p.unsure:
                    info(f"{p.release.title} is not the library's files, and its year is not the "
                         "library's - not used")
                    continue
                if chosen is None:
                    chosen = (p, got[0], got[1], False)
                if not p.same:
                    break                       # the rest are ranked lower still
            return chosen

        chosen = None
        for entry in order(cfg):
            chosen = own_files() if entry == OWN else best_on(entry)
            if chosen:
                if len(names) > 1:
                    info(f"chosen: {chosen[0].release.title} from {chosen[0].release.indexer}"
                         + (" - your files" if chosen[3] else ""))
                break
        if chosen is None:
            self.state.update(key, offer=dict(base, none=True, why=f"no pack on {trackers_text(cfg)} has every episode the library has"))
            return None
        p, data, t, same = chosen
        path = kept_torrent(cfg, t.infohash)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        self.state.update(key, offer=dict(base, title=p.release.title, size=t.total_size, files=len(t.real_files),
                                          seeders=p.release.seeders, same=same, infohash=t.infohash,
                                          tracker=p.release.indexer))
        return chosen

    def kept_pick(self, cfg, key: str):
        """The pack the season's earlier search chose, from its saved .torrent - None if
        there is none (for the order the tab has now)."""
        it = self.state.seasons.get(key, {})
        o = it.get("offer") or {}
        if not offer_current(o, cfg) or not o.get("infohash") or o["infohash"] in (it.get("no_usenet") or []):
            return None
        try:
            with open(kept_torrent(cfg, o["infohash"]), "rb") as fh:
                data = fh.read()
            t = torrent_mod.parse(data)
        except (OSError, ValueError, torrent_mod.TorrentError):
            return None
        r = clients.Release(title=o["title"], protocol="torrent", indexer=o.get("tracker") or o.get("indexer") or "",
                            indexer_id=0, size=t.total_size, guid="", download_url="", info_url="", publish_date="",
                            grabs=None, seeders=o.get("seeders"), files=o.get("files"))
        return Pick(r, bool(o.get("same")), False), data, t, bool(o.get("same"))

    def build(self, cfg, opts, key, t, data, p) -> dict:
        from .pipeline import execute_run, local_release
        left = float(cfg.library_daily_gb or 0) * 1024 ** 3 - self.state.spent_today()
        if t.total_size > left:
            daily = float(cfg.library_daily_gb or 0)
            gb = t.total_size / 1024 ** 3
            why = (f"needs {gb:.1f} GB from Usenet - more than the whole daily budget ({daily:.0f} GB)"
                   if gb > daily else f"needs {gb:.1f} GB from Usenet - waits until that much of today's budget is free")
            self.state.update(key, status=BUDGET, need=t.total_size, why=why)
            info("over today's download budget - it waits")
            return None
        step(f"Building {p.release.title} from Usenet")
        self.state.update(key, status=BUILDING)
        self.state.spend(t.total_size)
        rel = local_release(t, p.release.title + ".torrent")
        if p.release.seeders is not None and p.release.seeders >= 0:
            rel = dataclasses.replace(rel, seeders=p.release.seeders)
        return execute_run(cfg, opts, rel, [], torrent_data=data)

    def indexer_ids(self, cfg) -> dict[str, int]:
        """{tracker key: Prowlarr id} of the ticked trackers Prowlarr has; one it no longer
        has is left out (said in the log), none at all is an error."""
        have = {config_key(name): i for i, name in Prowlarr(cfg.prowlarr_url, cfg.prowlarr_key).indexers() if name}
        out, gone = {}, []
        for name in trackers(cfg):
            k = config_key(name)
            if k in have:
                out[k] = have[k]
            else:
                gone.append(name)
        if not out:
            raise ValueError(f"Prowlarr has no torrent indexer called {trackers_text(cfg)}")
        if gone:
            warn(f"Prowlarr has no torrent indexer called {', '.join(gone)} - left out of the search")
        return out

    # -- the tab's buttons
    def approve(self, key: str) -> dict:
        it = self.state.seasons.get(key)
        s = self.seasons.get(key)
        if not it or it.get("status") != SWAP or not s:
            raise ValueError("that season is not waiting for a swap")
        if self.app.cfg.library_cross_eyed_url:
            if getattr(self, "ce_dry", None):
                raise ValueError("cross-eyed is in dry-run mode - switch it to live in its header first")
            return self.move_in(key, it, s)        # your files are not touched: no seeding check
        t = self._torrent(it.get("infohash") or "")
        busy = seeded_from(swap_plan(t, it["root"], s), self.in_client())
        if busy:
            raise ValueError(f"not swapped: a torrent in qBittorrent seeds from {os.path.basename(busy[0])}"
                             f"{f' (and {len(busy) - 1} more)' if len(busy) > 1 else ''} - moving it would break that torrent")
        n, moved = do_swap(self.app.cfg, t, it["root"], s)
        self.state.update(key, status=SWAPPED, why=f"{n} episode(s) now the torrent's files; {moved} old "
                          f"file(s) set aside for {self.app.cfg.library_keep_replaced_days:g} days")
        self.rescan()
        return self.state.seasons[key]

    def move_in(self, key: str, it: dict, s: "Season") -> dict:
        """Approve with cross-eyed: the torrent's folder - with every cross-seed copy linked
        to it - moves into the show's folder under its own name, next to your season
        folder, which stays as it is until you delete it. Approved at once; handing it to
        cross-eyed runs in the background (it can take a while - see hand_over)."""
        root = it.get("root") or ""
        if not os.path.isdir(root):
            raise ValueError(f"the torrent's folder {root} is not there")
        self.state.update(key, status=MOVING, batch=None, move_started=False,
                          moved_to=os.path.join(os.path.dirname(s.dir.rstrip("/")), os.path.basename(root)),
                          why="approved - handing it to cross-eyed")
        threading.Thread(target=self.hand_over, args=(key,), name="library-move", daemon=True).start()
        return self.state.seasons[key]

    def hand_over(self, key: str):
        """Queue the move in cross-eyed. Its picture of qBittorrent is refreshed only when
        it does not know the torrent yet, and only while none of its jobs is running: a
        refresh rewrites its whole database, and a move in flight then fails on "database
        is locked"."""
        cfg = self.app.cfg
        it, s = self.state.seasons.get(key) or {}, self.seasons.get(key)
        try:
            if not s:
                raise ValueError("the season is no longer in the library")
            root, show_dir = it.get("root") or "", os.path.dirname(s.dir.rstrip("/"))
            ce = CrossEyed(cfg.library_cross_eyed_url, cfg.library_cross_eyed_prefix)
            if ce.dry_run():
                self.state.update(key, status=SWAP, batch=None, move_started=None, why=DRY_RUN_WAIT)
                return
            a = ce.preview(root, show_dir)
            if not any(x.get("hash") == it.get("infohash") for x in a.get("inside") or []):
                for _ in range(120):                    # up to an hour
                    if not self._still_handing(key):
                        return
                    if ce.active() == 0:
                        break
                    self.state.update(key, why="approved - waiting for cross-eyed to finish its other moves")
                    time.sleep(30)
                else:
                    raise ValueError("cross-eyed stayed busy for an hour")
                ce.refresh()
                a = ce.preview(root, show_dir)
            if not any(x.get("hash") == it.get("infohash") for x in a.get("inside") or []):
                raise ValueError(f"cross-eyed does not see the torrent in {root}")
            why = list(a.get("blockers") or [])
            if a.get("conflicts"):
                why.append(f"{os.path.basename(root)} is already in {show_dir}")
            if a.get("ghosts"):
                why.append("cross-eyed lists ghost torrents there - use its Folder move page")
            if a.get("copy_bytes"):
                why.append(f"it would copy {a['copy_bytes'] / 1024 ** 3:.1f} GB instead of moving in place")
            if why or not a.get("movable"):
                raise ValueError("cross-eyed would not move it: " + ("; ".join(why) or "not movable"))
            if not self._still_handing(key):
                return                                  # cancelled meanwhile
            batch = ce.move(root, show_dir)
            dest = ce.local(a.get("target_host") or "") or os.path.join(show_dir, os.path.basename(root))
            self.state.update(key, batch=batch, moved_to=dest,
                              why=f"approved - cross-eyed is moving it into {show_dir} (batch {batch})")
        except Exception as e:                          # back to ready, saying why
            if self._still_handing(key):
                self.state.update(key, status=SWAP, batch=None, move_started=None,
                                  why=f"not moved - {e}")

    def _still_handing(self, key: str) -> bool:
        it = self.state.seasons.get(key) or {}
        return it.get("status") == MOVING and not it.get("batch")

    def cancel_move(self, key: str) -> dict:
        """Cancel an approved move - only while cross-eyed has not started it (being handed
        over, or every job still pending, e.g. held in dry-run). Once it has, the folder may
        be renamed already, and pointing the torrents back would leave them without files."""
        it = self.state.seasons.get(key)
        if not it or it.get("status") != MOVING:
            raise ValueError("that season is not being moved")
        if it.get("batch"):
            cfg = self.app.cfg
            ce = CrossEyed(cfg.library_cross_eyed_url, cfg.library_cross_eyed_prefix)
            jobs, _ = ce.batch(it["batch"])
            if any(j.get("state") != "pending" for j in jobs):
                self.state.update(key, move_started=True)
                raise ValueError("cross-eyed has started moving it - it can no longer be cancelled from here")
            if jobs:
                ce.cancel(it["batch"])
                left, _ = ce.batch(it["batch"])
                if left:
                    raise ValueError("cross-eyed did not cancel the move - see its Jobs page")
        self.state.update(key, status=SWAP, batch=None, move_started=None, moved_to=None,
                          why="move cancelled - nothing was changed; seeding at 100.0% from its own folder")
        return self.state.seasons[key]

    def follow_moves(self, cfg):
        """Seasons cross-eyed is moving. qBittorrent has the last word: the torrent seeding
        at 100.0% from its new folder is a finished move, whatever cross-eyed's jobs say (its
        last step - removing the old folder - may be stuck, or its own bookkeeping may have
        failed after the move itself worked)."""
        from .clients import CHECKING
        from .pathmap import map_path
        # moved ones too: a move is only finished once qBittorrent has rechecked it there
        moving = [(k, it) for k, it in self.state.seasons.items()
                  if it.get("status") in (MOVING, MOVED) and it.get("batch")]
        if not moving or not cfg.library_cross_eyed_url:
            return
        ce = CrossEyed(cfg.library_cross_eyed_url, cfg.library_cross_eyed_prefix)
        back = [(b, a) for a, b in (getattr(cfg, "local_to_qbit", None) or [])]
        qb = self.app._qbit()
        for key, it in moving:
            try:
                jobs, dry = ce.batch(it["batch"])
                info_ = qb.info(it.get("infohash") or "") or {}
            except (ApiError, ValueError, OSError):
                return
            states = {j.get("state") for j in jobs}
            started = bool(states - {"pending"})
            if it.get("move_started") != started:
                self.state.update(key, move_started=started)
            there = bool(info_) and os.path.normpath(map_path(info_.get("content_path") or "", back)) \
                == os.path.normpath(it.get("moved_to") or "")
            checking = info_.get("state") in CHECKING
            # cross-eyed repoints the torrent first and has it rechecked after: until its
            # move jobs are done, 100.0% in the new folder is qBittorrent's old count, unchecked
            rechecked = moves_done(jobs)
            if there and info_.get("progress", 0) >= 1 and not checking and rechecked:
                if it.get("status") == MOVED:
                    continue
                if info_.get("state") in ("stoppedUP", "pausedUP") and getattr(cfg, "start_when_complete", False):
                    try:
                        qb.start(it["infohash"])
                    except (ApiError, OSError):
                        pass
                left = [j for j in jobs if j.get("state") != "done"]
                note = (" (cross-eyed's last step is waiting on its Jobs page - the move itself is done)"
                        if left else "")
                self.state.update(key, status=MOVED, root=it.get("moved_to"),
                                  why=f"seeding at 100.0% from {it.get('moved_to')} - your original season "
                                      f"folder can be deleted{note}")
                self.state.scanned_at = 0              # the moved folder is a season of its own now
                continue
            if it.get("status") == MOVED:          # marked moved too early: not finished yet
                self.state.update(key, status=MOVING)
            if there and not rechecked and not states & CE_FAILED:
                why = ("moved - qBittorrent is checking it in its new folder" if checking else
                       "moved - waiting for qBittorrent to recheck it in its new folder")
            elif there:
                why = ("moved - qBittorrent is checking it in its new folder" if checking else
                       f"moved, but it is at {info_.get('progress', 0) * 100:.2f}% in its new folder - "
                       f"see cross-eyed's Jobs page (batch {it['batch']})")
            elif states & CE_FAILED and os.path.isdir(it.get("root") or ""):
                bad = sorted(states & CE_FAILED)
                self.state.update(key, status=SWAP, batch=None, move_started=None,
                                  why=f"cross-eyed could not move it ({', '.join(bad)}, batch {it['batch']}) - "
                                      "nothing was moved; see its Jobs page, then approve again")
                continue
            elif states & CE_FAILED:
                why = (f"cross-eyed's move stopped half-way ({', '.join(sorted(states & CE_FAILED))}) - the folder "
                       f"moved but the torrent is not pointed at it yet; see its Jobs page (batch {it['batch']})")
            elif states & CE_WAITING:
                why = f"cross-eyed needs your answer on its Jobs page (batch {it['batch']})"
            elif dry and not started:
                why = (f"queued in cross-eyed, held by its dry-run mode - switch it to live and it is moved "
                       f"by itself (batch {it['batch']})")
            elif not started:
                why = f"approved - queued in cross-eyed, waiting its turn (batch {it['batch']})"
            else:
                why = f"approved - cross-eyed is moving it (batch {it['batch']})"
            if it.get("why") != why:
                self.state.update(key, why=why)

    def move_verified(self, it: dict) -> bool:
        """cross-eyed's move jobs for the season are done (its torrent rechecked in the new
        folder). Not asked when there is no cross-eyed or batch."""
        cfg = self.app.cfg
        if not it.get("batch") or not getattr(cfg, "library_cross_eyed_url", ""):
            return True
        try:
            jobs, _ = CrossEyed(cfg.library_cross_eyed_url, cfg.library_cross_eyed_prefix).batch(it["batch"])
        except (ApiError, ValueError, OSError):
            return False
        return moves_done(jobs)

    def check_dry_run(self, cfg) -> bool | None:
        """Ask cross-eyed whether it is in dry-run mode (it then only queues moves and waits):
        moves are not offered while it is."""
        self.ce_dry = None
        if getattr(cfg, "library_cross_eyed_url", ""):
            try:
                self.ce_dry = CrossEyed(cfg.library_cross_eyed_url, cfg.library_cross_eyed_prefix).dry_run()
            except (ApiError, ValueError, OSError):
                pass
        return self.ce_dry

    def auto_moves(self, cfg):
        """The Library tab's two ticks (cross-eyed only): seasons ready to swap are moved into
        the library one at a time, and moved seasons' original folders are deleted - each
        with the same checks as the buttons."""
        if not getattr(cfg, "library_cross_eyed_url", ""):
            return
        if getattr(cfg, "library_auto_delete", False):
            for key, it in list(self.state.seasons.items()):
                if it.get("status") != MOVED:
                    continue
                try:
                    self.delete_original(key)
                except (ValueError, OSError):
                    pass                        # not yet (still checking, ...) - tried again next time
        if getattr(cfg, "library_auto_move", False) and not getattr(self, "ce_dry", None):
            if any(it.get("status") == MOVING for it in self.state.seasons.values()):
                return                          # one move at a time
            for key in sorted(self.state.seasons, key=lambda k: (self.state.seasons[k].get("show") or "").lower()):
                it = self.state.seasons[key]
                why = it.get("why") or ""
                if it.get("status") != SWAP or why.startswith(("not moved", "move cancelled")):
                    continue                    # a move that failed or you cancelled waits for you
                try:
                    self.approve(key)
                except ValueError as e:
                    self.state.update(key, why=f"not moved - {e}")
                    continue
                return

    def torrent_states(self) -> dict:
        """infohash -> qBittorrent's row, kept 20 seconds (the tab asks often)."""
        c = getattr(self, "_rows", None)
        if c and time.time() - c[0] < 20:
            return c[1]
        try:
            rows = {r.get("hash"): r for r in self.app._qbit().torrents()}
        except (ApiError, OSError, AttributeError):
            rows = None
        self._rows = (time.time(), rows)
        return rows

    def delete_original(self, key: str) -> dict:
        """A moved season: your original season folder goes, once its torrent is seen
        seeding at 100.0% from the library and nothing in qBittorrent has files in it."""
        from .pathmap import map_path
        it, s = self.state.seasons.get(key), self.seasons.get(key)
        if not it or it.get("status") != MOVED:
            raise ValueError("that season has not been moved into the library")
        folder = (s.dir if s else it.get("dir") or "").rstrip("/")
        if not folder or not os.path.isdir(folder):
            raise ValueError("the original folder is gone already")
        cfg = self.app.cfg
        back = [(b, a) for a, b in (cfg.local_to_qbit or [])]
        try:
            info_ = self.app._qbit().info(it.get("infohash") or "") or {}
        except (ApiError, OSError, AttributeError):
            raise ValueError("qBittorrent cannot be reached - nothing deleted") from None
        where = os.path.normpath(map_path(info_.get("content_path") or "", back))
        if info_.get("progress", 0) < 1 or where != os.path.normpath(it.get("moved_to") or "") \
                or (info_.get("state") or "").startswith("checking"):
            raise ValueError("its torrent is not seeding at 100.0% from the library - nothing deleted")
        if not self.move_verified(it):
            raise ValueError("cross-eyed has not finished the move (qBittorrent's recheck in the new folder) - "
                             "nothing deleted")
        paths = self.in_client()
        f = os.path.normcase(os.path.abspath(folder))
        busy = [p for p in paths if p == f or p.startswith(f + os.sep)]
        if not paths or busy:
            raise ValueError("a torrent in qBittorrent has files in it - nothing deleted" if busy
                             else "qBittorrent cannot be reached - nothing deleted")
        size = sum(os.path.getsize(os.path.join(dp, n)) for dp, _, ns in os.walk(folder) for n in ns)
        shutil.rmtree(folder)
        self.state.update(key, status=ORIGINAL_DELETED,
                          why=f"original folder deleted ({size / 1024 ** 3:.1f} GB freed) - the torrent seeds "
                              f"from {it.get('moved_to')}")
        self.state.scanned_at = 0
        self.wake.set()
        return self.state.seasons[key]

    def keep(self, key: str):
        it = self.state.seasons.get(key)
        if not it or it.get("status") != SWAP:
            raise ValueError("that season is not waiting for a swap")
        self.state.update(key, status=KEPT, why="kept the library's files; the torrent seeds its own copy")

    def retry(self, key: str):
        if key not in self.state.seasons or key == self.current:
            raise ValueError("nothing to retry")
        self.state.update(key, status=WAITING, why="", offer=None)     # a new search
        self.wake.set()

    def skip(self, key: str):
        if key == self.current:
            raise ValueError("it is being worked on - cancel its job first")
        self.state.update(key, status=SKIPPED, why="skipped", again=False)

    def check(self, key: str):
        """A season left alone (qBittorrent has it): search the tracker for it once, as
        asked - its pack is kept, .torrent saved, and "Download again" then uses it."""
        cfg = self.app.cfg
        if key not in self.seasons or key == self.current:
            raise ValueError("that season cannot be looked up now")
        if not trackers(cfg):
            raise ValueError("tick a tracker first")
        self.look(cfg, key)

    def again(self, key: str):
        """A season already in the client: get it for the ticked trackers all the same."""
        if key not in self.state.seasons or key == self.current:
            raise ValueError("nothing to download again")
        self.state.update(key, status=WAITING, again=True,
                          why=f"downloading again for {trackers_text(self.app.cfg)}, as you asked")
        self.wake.set()

    def _torrent(self, infohash: str) -> torrent_mod.Torrent:
        try:
            with open(kept_torrent(self.app.cfg, infohash), "rb") as fh:
                return torrent_mod.parse(fh.read())
        except (OSError, ValueError, torrent_mod.TorrentError):
            raise ValueError("the torrent file of that season is gone") from None

    def _new_files(self, it: dict) -> tuple:
        """(bytes, files) of the season's torrent - the new copy: from its kept .torrent,
        else the tracker's offer when that is the same torrent."""
        h = it.get("infohash")
        if not h:
            return None, None
        cache = self.__dict__.setdefault("_new_cache", {})
        if h not in cache:
            try:
                t = self._torrent(h)
                cache[h] = (t.total_size, len(t.real_files))
            except ValueError:
                o = it.get("offer") or {}
                same = o.get("infohash") == h or (o.get("title") and o.get("title") == it.get("torrent"))
                cache[h] = (o.get("size"), o.get("files")) if same else (None, None)
        return cache[h]

    def items(self) -> dict:
        out = [dict(v, key=k, running=k == self.current) for k, v in self.state.seasons.items()]
        rows = self.torrent_states() if any(x.get("infohash") for x in out) else {}
        cfg = getattr(self.app, "cfg", None)
        for x in out:
            x["offer_current"] = offer_current(x.get("offer"), cfg) if cfg is not None else False
            if x.get("torrent"):
                x["new_size"], x["new_files"] = self._new_files(x)
            if x.get("infohash") and x.get("status") not in (WAITING, IN_CLIENT, SEEDED, SEEDED_STOPPED, NO_PACK, SKIPPED):
                x["qbit"] = torrent_label(None if rows is None else rows.get(x["infohash"]), rows is None)
                if x["qbit"] == "not in qBittorrent" and x.get("status") in (SEARCHING, MATCHING, BUILDING, BUDGET):
                    x["qbit"] = "not added yet - added once the build is done"
        counts = Counter(x.get("status") for x in out)
        return {"seasons": sorted(out, key=lambda x: ((x.get("show") or "").lower(), x.get("season") or 0)),
                "counts": dict(counts),
                "deletable_bytes": sum(x.get("size") or 0 for x in out if x.get("status") == MOVED), "spent_today": self.state.spent_today(),
                "scanned_at": self.state.scanned_at, "current": self.current,
                "cross_eyed_dry_run": bool(getattr(self, "ce_dry", None)) if getattr(cfg, "library_cross_eyed_url", "") else False}


class CrossEyed:
    """cross-eyed, the companion to cross-seed: its folder move takes a torrent's folder -
    and every cross-seed copy linked to it - to another place, repoints and rechecks each
    torrent, and only then lets go of the old place. Paths are this machine's; cross-eyed
    sees them under ``prefix``."""

    def __init__(self, url: str, prefix: str = "/host"):
        import requests
        self.url, self.prefix = url.rstrip("/"), (prefix or "").rstrip("/")
        self.s = requests.Session()

    def host(self, path: str) -> str:
        return self.prefix + os.path.abspath(path)

    def local(self, host_path: str) -> str:
        return host_path[len(self.prefix):] if self.prefix and host_path.startswith(self.prefix + "/") else host_path

    def _req(self, method: str, path: str, **kw):
        import requests
        try:
            r = self.s.request(method, self.url + path, allow_redirects=False, **kw)
        except requests.RequestException as e:
            raise ApiError(f"cross-eyed cannot be reached: {e}") from None
        if r.status_code >= 400:
            raise ApiError(f"cross-eyed answered {r.status_code} to {path}")
        return r

    def refresh(self):
        self._req("POST", "/refresh", timeout=900)

    def preview(self, source: str, target: str) -> dict:
        r = self._req("GET", "/api/folder-move/preview", timeout=600,
                      params={"source": self.host(source), "target": self.host(target)})
        return r.json().get("a") or {}

    def move(self, source: str, target: str) -> str:
        from urllib.parse import parse_qs, urlsplit
        r = self._req("POST", "/folder-move", timeout=600,
                      data={"source": self.host(source), "target": self.host(target)})
        q = parse_qs(urlsplit(r.headers.get("Location", "")).query)
        if q.get("highlight"):
            return q["highlight"][0]
        raise ValueError("cross-eyed did not queue the move" + (f": {q['notice'][0]}" if q.get("notice") else ""))

    def active(self) -> int:
        """How many of its jobs are running now."""
        return int(self._req("GET", "/api/jobs", timeout=120).json().get("active_count") or 0)

    def cancel(self, batch_id: str):
        """Drop the batch's jobs that have not started (and roll back any that have)."""
        self._req("POST", "/jobs/cancel-active", timeout=120, data={"batch_id": batch_id})

    def open_jobs(self) -> tuple[dict[str, dict], bool]:
        """({infohash: job} of its file jobs not finished with, is it in dry-run?). Every
        state but done and rolled back counts: a failed job can leave a safety copy linked to
        the torrent's files, a move still finishing, or a torrent stopped on purpose - and a
        retry starts again from what it recorded of the torrent."""
        d = self._req("GET", "/api/jobs", timeout=120).json()
        out: dict[str, dict] = {}
        for j in (d.get("file_jobs") or []) + (d.get("rolledback_jobs") or []):
            h = (j.get("info_hash") or "").lower()
            if h and j.get("job_type") != "search" and j.get("state") not in ("done", "rolled_back"):
                out.setdefault(h, j)
        return out, bool(d.get("dry_run"))

    def download_check(self, infohash: str) -> dict:
        """Is it safe to let this torrent download what it is missing? cross-eyed works it
        out from piece hashes: {"verdict": complete | in_job | repairable |
        shared_breaks_others | shared_other_names | shared_same_version | not_shared |
        unknown, "summary": ..., ...}."""
        return self._req("GET", f"/api/torrents/{infohash.lower()}/download-check", timeout=600).json()

    def repair(self, infohash: str) -> dict:
        """Queue a repair of its damaged pieces from other copies on disk - nothing is
        downloaded. {"ok", "job_id", "dry_run", "notice"}; ApiError with cross-eyed's reason
        when it will not (a job already in play, a torrent it does not know yet)."""
        import requests
        try:
            r = self.s.post(f"{self.url}/api/torrents/{infohash.lower()}/repair", timeout=120,
                            allow_redirects=False)
        except requests.RequestException as e:
            raise ApiError(f"cross-eyed cannot be reached: {e}") from None
        try:
            d = r.json()
        except ValueError:
            d = {}
        if r.status_code >= 400:
            raise ApiError(d.get("error") or f"cross-eyed answered {r.status_code}")
        return d

    def dry_run(self) -> bool:
        """Is cross-eyed in dry-run mode (moves queued, run only by hand)?"""
        return bool(self._req("GET", "/api/jobs", timeout=120).json().get("dry_run"))

    def batch(self, batch_id: str) -> tuple[list[dict], bool]:
        d = self._req("GET", "/api/jobs", timeout=120, params={"batch_id": batch_id}).json()
        jobs = [j for j in (d.get("file_jobs") or []) + (d.get("rolledback_jobs") or [])
                if j.get("batch_id") == batch_id]
        return jobs, bool(d.get("dry_run"))


def same_drive(a: str, b: str) -> bool:
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def output_dir(cfg) -> str:
    """Where the Library's torrents go: the folder chosen for it - or, with none chosen, the
    usual torrents folder, but only when that is on the library's drive (the library's
    files are hard-linked there, and hard links do not cross drives). "" = none usable."""
    roots = cfg.library_roots or []
    if cfg.library_output_dir:
        return cfg.library_output_dir
    if roots and cfg.output_dir and same_drive(cfg.output_dir, roots[0]):
        return cfg.output_dir
    return ""


def kept_torrent(cfg, infohash: str) -> str:
    return os.path.join(cfg.torrent_dir, "library", f"{infohash}.torrent")


def torrent_mod_root(t: torrent_mod.Torrent, out_dir: str) -> str:
    from .assemble import torrent_root
    return torrent_root(t, out_dir)
