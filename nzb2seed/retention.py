"""Removing builds again after a while.

Off unless you switch it on. When it is on, a torrent the **Automatic tab** built is
removed from qBittorrent once it has been there for longer than ``retention_days``, and
the files nzb2seed itself placed for it are deleted.

Three rules keep this safe:

* only builds the Automatic tab made (releases autobrr handed over) are considered. A
  build, season or assemble you asked for yourself is never removed, however old it is -
  each record says which part of nzb2seed made it, and records written before that was
  recorded fall back to the inbox's own list of what it built;
* the torrent has to have the ``<infohash>.owned.json`` record a build writes, and its
  files have to still be where nzb2seed put them, unchanged since it wrote them;
* only the files and folders listed in that record are deleted. Anything else in the save
  path - files you put there, a season's metadata folder, another torrent's data sharing
  the folder - is left exactly where it is, and a folder is only removed once it is empty.

qBittorrent is told to remove the torrent *without* deleting files, so the deleting is
done here, from the record, and never by hash over a folder nzb2seed does not own.
"""
from __future__ import annotations

import json
import os
import time

from . import assemble as asm
from .clients import ApiError, QBittorrent
from .config import Config

SUFFIX = ".owned.json"


class Build:
    """One torrent nzb2seed built, as the retention sweep sees it."""

    def __init__(self, infohash: str, record: str, info: dict | None):
        self.infohash = infohash
        self.record = record                      # path of its <infohash>.owned.json
        self.info = info or {}                    # what qBittorrent knows, {} when it is gone
        # when it was added to qBittorrent, or the record's own age when qBittorrent has no
        # date for it (an old torrent, or one removed by hand). Read once, here, so the age
        # can still be reported after the record has been deleted.
        self.added = float(self.info.get("added_on") or 0)
        if not self.added:
            try:
                self.added = os.path.getmtime(record)
            except OSError:
                self.added = time.time()

    @property
    def name(self) -> str:
        return self.info.get("name") or self.infohash

    def age_days(self, now: float | None = None) -> float:
        return ((now or time.time()) - self.added) / 86400


def records(cfg: Config) -> list[str]:
    try:
        names = sorted(os.listdir(cfg.torrent_dir))
    except OSError:
        return []
    return [os.path.join(cfg.torrent_dir, n) for n in names if n.endswith(SUFFIX)]


def automatic(cfg: Config) -> set[str]:
    """Infohashes the Automatic tab built, from the inbox's own list - so builds made
    before records carried a source are still recognised as automatic."""
    path = os.path.join(os.path.dirname(os.path.abspath(cfg.path)), "inbox.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return {str(k).lower() for k in (json.load(fh).get("items") or {})}
    except (OSError, ValueError):
        return set()


def made_automatically(cfg: Config, infohash: str, record: str, seen: set[str]) -> bool:
    """Only what the Automatic tab built is ever removed again. A build, season or assemble
    you asked for yourself is never swept away, however old it is."""
    source = asm.Owned(record).source
    if source:
        return source == "auto"
    return infohash in seen        # older records: the inbox still lists what it built


def builds(cfg: Config, qb: QBittorrent | None) -> list[Build]:
    """Every automatic build nzb2seed has a record for, with what qBittorrent knows of it."""
    known: dict[str, dict] = {}
    if qb is not None:
        try:
            known = {str(t.get("hash", "")).lower(): t for t in qb.torrents()}
        except (ApiError, OSError):
            known = {}
    seen = automatic(cfg)
    out = []
    for path in records(cfg):
        infohash = os.path.basename(path)[:-len(SUFFIX)].lower()
        if not made_automatically(cfg, infohash, path, seen):
            continue
        out.append(Build(infohash, path, known.get(infohash)))
    return out


def due(cfg: Config, qb: QBittorrent | None, now: float | None = None,
        force: bool = False, days: float | None = None) -> list[Build]:
    """Builds old enough to remove.

    Empty while retention is off - except with ``force``, which answers "what would go if
    it were on?" without switching anything on. ``days`` tries an age without saving it,
    and 0 days means every automatic build, however new."""
    age = cfg.retention_days if days is None else days
    if (not cfg.retention_enabled and not force) or age < 0:
        return []
    return [b for b in builds(cfg, qb) if b.age_days(now) >= age]


def backfill(cfg: Config, qb: QBittorrent | None, builds_: list[Build], log=print) -> int:
    """Record sizes and times for builds made before nzb2seed kept them - but only where
    qBittorrent proves nothing was ever downloaded over BitTorrent for that torrent.

    Those files can only have come from Usenet, so what is on disk now *is* what nzb2seed
    wrote, and stamping it is a fact rather than a guess. A torrent that has downloaded
    even one byte is left unstamped, and so stays out of every sweep."""
    n = 0
    for b in builds_:
        owned = asm.Owned(b.record)
        if not owned.files or owned.stamps or not b.info:
            continue
        if float(b.info.get("downloaded") or 0) > 0:
            log(f"{b.name}: BitTorrent has downloaded part of this one - leaving it unstamped")
            continue
        if not any(os.path.exists(f) for f in owned.files):
            continue                      # its files moved: _elsewhere handles that
        owned.save()                      # save() stamps every file that is there
        n += 1
        log(f"{b.name}: recorded sizes and times ({len(owned.stamps)} file(s); "
            f"qBittorrent has downloaded nothing for it)")
    return n


def _rmdirs(dirs: set[str], log) -> int:
    """Remove the folders a build created, innermost first, and only while empty."""
    n = 0
    for d in sorted(dirs, key=len, reverse=True):
        try:
            if os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
                n += 1
        except OSError as e:
            log(f"  left {d} ({e})")
    return n


def _elsewhere(b: Build, owned: "asm.Owned") -> str:
    """Is qBittorrent seeding this torrent from somewhere other than where nzb2seed put the
    files? Then the record describes a folder that is no longer the torrent's data - moved
    into a library, say - and removing the torrent would stop a healthy seed while freeing
    nothing. The path it names is reported so you can see where it went."""
    if not owned.files:
        return ("its record lists no files, so there is nothing of it to delete - "
                "remove the torrent yourself if you want it gone")
    if any(os.path.exists(f) for f in owned.files):
        return ""
    where = b.info.get("content_path") or b.info.get("save_path") or ""
    return ("none of the files nzb2seed placed are there any more" +
            (f"; qBittorrent is seeding it from {where}" if where else "") +
            " - it was moved, so removing the torrent would stop a healthy seed and free nothing")


def verify(owned: "asm.Owned") -> tuple[list[str], str]:
    """Which recorded files are still exactly as nzb2seed wrote them, and why the build
    cannot be removed if any are not.

    A file whose size or modification time has changed was written by something else -
    almost always qBittorrent re-downloading a piece over BitTorrent. Removing that
    torrent would throw away a real download and can leave a hit-and-run, so the whole
    build is left alone rather than only that file."""
    changed, unknown = [], []
    for f in sorted(owned.files):
        if not os.path.exists(f):
            continue                       # gone already: nothing to delete, nothing downloaded
        state = owned.unchanged(f)
        if state is None:
            unknown.append(f)
        elif not state:
            changed.append(f)
    if changed:
        return [], (f"{len(changed)} file(s) have changed since nzb2seed wrote them "
                    f"(e.g. {os.path.basename(changed[0])}) - BitTorrent has re-downloaded "
                    f"part of this torrent, so removing it could leave a hit-and-run")
    if unknown:
        return [], (f"{len(unknown)} file(s) were written before nzb2seed recorded sizes and "
                    f"times, so it cannot tell whether BitTorrent has re-downloaded any of "
                    f"them - remove this one by hand if you are sure")
    return [f for f in sorted(owned.files) if os.path.isfile(f) and not os.path.islink(f)], ""


def remove(cfg: Config, qb: QBittorrent | None, b: Build, log=print, dry_run: bool = False) -> dict:
    """Remove one build: out of qBittorrent first (so it stops seeding), then the files
    nzb2seed placed for it - but only while every one of them is still untouched."""
    owned = asm.Owned(b.record)
    why = _elsewhere(b, owned)
    files, why = ([], why) if why else verify(owned)
    gone = [f for f in owned.files if not os.path.exists(f)]
    freed = sum(os.path.getsize(f) for f in files)
    what = {"infohash": b.infohash, "name": b.name, "files": len(files), "bytes": freed,
            "missing": len(gone), "dirs": 0, "from_qbit": False, "skipped": why}
    if why:
        log(f"{b.name}: left alone - {why}")
        return what
    if dry_run:
        return what
    if qb is not None and b.info:
        try:
            qb.remove(b.infohash)                 # never with its files: they go from the record
            what["from_qbit"] = True
        except (ApiError, OSError) as e:
            log(f"{b.name}: could not remove it from qBittorrent ({e}) - files left alone")
            return what
    for f in files:
        try:
            os.remove(f)
        except OSError as e:
            log(f"  kept {f} ({e})")
            what["files"] -= 1
            what["bytes"] -= os.path.getsize(f) if os.path.exists(f) else 0
    what["dirs"] = _rmdirs(set(owned.dirs), log)
    try:
        os.remove(b.record)
    except OSError:
        pass
    torrent = os.path.join(cfg.torrent_dir, f"{b.infohash}.torrent")
    if os.path.isfile(torrent):
        try:
            os.remove(torrent)
        except OSError:
            pass
    return what


def gb(n: int) -> str:
    return f"{n / 1024**3:.2f} GB"


def sweep(cfg: Config, qb: QBittorrent | None, log=print, dry_run: bool = False,
          force: bool = False, days: float | None = None) -> dict:
    """One pass: remove every build past its retention age. With ``dry_run`` nothing is
    touched, and ``force``/``days`` let that preview run while retention is still off."""
    items = due(cfg, qb, force=force, days=days)
    # stamping only adds knowledge - it never touches media - so it runs for a preview too,
    # otherwise the preview keeps reporting builds it could have judged
    backfill(cfg, qb, items, log)
    done, kept, freed = [], [], 0
    for b in items:
        what = remove(cfg, qb, b, log, dry_run)
        if what["skipped"]:
            kept.append(what)
            continue
        done.append(what)
        freed += what["bytes"]
        log(f"{'would remove' if dry_run else 'removed'} {what['name']} "
            f"({b.age_days():.0f} days old, {what['files']} file(s), {gb(what['bytes'])})")
    return {"removed": done, "kept": kept, "bytes": freed, "dry_run": dry_run,
            "days": cfg.retention_days if days is None else days,
            "was_off": not cfg.retention_enabled}


def state(cfg: Config, qb: QBittorrent | None, days: float | None = None) -> dict:
    """What the Settings page shows: how many builds are held, and what is next to go.
    ``due`` answers for the age given (or configured) whether retention is on or not."""
    all_builds = builds(cfg, qb)
    nxt = min(all_builds, key=lambda b: b.age_days(), default=None) if all_builds else None
    return {
        "enabled": cfg.retention_enabled,
        "days": cfg.retention_days if days is None else days,
        "builds": len(all_builds),
        "due": len(due(cfg, qb, force=True, days=days)),
        "oldest_days": round(max((b.age_days() for b in all_builds), default=0), 1),
        "next_name": nxt.name if nxt else "",
    }
