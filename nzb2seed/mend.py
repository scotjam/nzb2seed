"""Finish stopped torrents from another torrent's good files.

A torrent left stopped short of 100% (a cross-seed partial match, or a Library season whose
episode differs from the tracker's) often has its missing files whole in another torrent of
the same name in qBittorrent - another tracker's copy, or the copy nzb2seed built for another
tracker from Usenet - or in the version of a file nzb2seed kept before a repair or a
BitTorrent download changed it (originals.py), which is tried first. Every so often each
such torrent gets that version of its incomplete files and a recheck:

* the file is replaced by a hard link to the other copy (or a copy of it, from another drive)
  - never written into, so the library and any torrent sharing the old file keep theirs;
* a file that comes out no better is put back as it was;
* at 100.0% the torrent is started - it only seeds, nothing was downloaded.

A copy that was tried is not tried again. ``mend.json`` keeps what was tried and done.

With nothing here left to try, the rest can come from the torrent's own tracker: a tracker
ticked "always add" in Settings, and only while what is missing - with what earlier starts
of the torrent downloaded - is within the limit set for it (the same check as a build
handed to qBittorrent). Before it starts, each short file gets bytes of its own (shared
with a library file or other torrents, it would change theirs too), and its present
version is kept as an original. A torrent is started this way once; one stopped again
after that is left as it is, and nothing starts while the space guard is holding downloads.

Torrents match by name, ignoring a video extension (one tracker names a single-file torrent
after its file, another after a folder holding it); files match by path, else by file name
and size. A torrent's files are looked for where qBittorrent has them: an unfinished
torrent's may still be in its download folder rather than where it will be saved.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time

from . import btledger, originals
from . import config as config_mod
from .clients import CHECKING, ApiError

INTERVAL = 30 * 60
FIRST_ROUND = 3 * 60
STOPPED = {"stoppedDL", "pausedDL"}
ORIG = ".nzb2seed-orig"
VIDEO = (".mkv", ".mp4", ".m4v", ".avi", ".ts", ".m2ts")


class State:
    def __init__(self, path: str):
        self.path = path
        try:
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            d = {}
        self.tried: dict[str, list] = d.get("tried", {})      # infohash -> ["file|dev:ino", ...]
        self.pending: list = d.get("pending", [])             # [[file, its original kept aside]] mid-swap
        self.started: dict = d.get("started", {})             # infohash -> when its tracker was asked for the rest
        self.log: list = d.get("log", [])

    def save(self):
        tmp = f"{self.path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"tried": self.tried, "pending": self.pending, "started": self.started,
                       "log": self.log[-200:]}, fh, indent=1)
        os.replace(tmp, self.path)

    def note(self, text: str):
        print(f"mend: {text}", flush=True)
        self.log.append([time.time(), text])
        self.save()


def _inode(path: str):
    st = os.stat(path)
    return st.st_dev, st.st_ino


def title(name: str) -> str:
    """A torrent's name without a video extension."""
    low = name.lower()
    for ext in VIDEO:
        if low.endswith(ext):
            return name[:-len(ext)]
    return name


def locate(r: dict, name: str, back, map_path) -> str | None:
    """Where torrent ``r``'s file ``name`` is on this machine: under the folder its content
    is in now, else its save or download folder."""
    cp = (r.get("content_path") or "").rstrip("/")
    bases = [os.path.dirname(cp), cp, r.get("save_path") or "", r.get("download_path") or ""]
    for b in dict.fromkeys(x for x in bases if x):
        p = os.path.join(map_path(b, back), *name.split("/"))
        if os.path.isfile(p):
            return p
    return None


def same_file(files: dict[str, dict], name: str, size) -> dict | None:
    """Another torrent's entry for file ``name``: the same path, else the one file of the
    same name and size."""
    if name in files:
        return files[name] if files[name].get("size") == size else None
    base = name.rsplit("/", 1)[-1]
    hits = [x for n, x in files.items() if n.rsplit("/", 1)[-1] == base and x.get("size") == size]
    return hits[0] if len(hits) == 1 else None


def put_in(src: str, dst: str):
    """Make ``dst`` the file ``src`` is - a hard link on the same drive, else a copy - by
    replacing the name, never writing into the old file. The old file is kept as
    ``dst + ORIG`` (a second name for it)."""
    keep = dst + ORIG
    if os.path.lexists(keep):
        os.remove(keep)
    os.link(dst, keep)
    tmp = dst + ".nzb2seed-new"
    if os.path.lexists(tmp):
        os.remove(tmp)
    if os.stat(src).st_dev == os.stat(os.path.dirname(dst)).st_dev:
        os.link(src, tmp)
    else:
        shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


def put_back(dst: str):
    os.replace(dst + ORIG, dst)


def let_go(dst: str):
    if os.path.lexists(dst + ORIG):
        os.remove(dst + ORIG)


class Mender:
    def __init__(self, app, state_path: str):
        self.app = app
        self.state = State(state_path)
        self.stop = threading.Event()
        self._undo_pending()
        threading.Thread(target=self._loop, name="mend", daemon=True).start()

    def _undo_pending(self):
        """A swap a restart cut off: the old files go back (the next round tries again)."""
        for dst, _ in self.state.pending:
            try:
                if os.path.lexists(dst + ORIG):
                    put_back(dst)
            except OSError as e:
                print(f"mend: could not put back {dst}: {e}", flush=True)
        self.state.pending = []
        self.state.save()

    def _loop(self):
        self.stop.wait(FIRST_ROUND)
        while not self.stop.is_set():
            try:
                self.round()
            except Exception as e:              # never let the worker die
                print(f"mend: {type(e).__name__}: {e}", flush=True)
            self.stop.wait(INTERVAL)

    def busy(self) -> set[str]:
        """Torrents a job of nzb2seed's is working on - left alone."""
        return {j.infohash for j in list(getattr(self.app, "jobs", {}).values())
                if getattr(j, "infohash", "") and j.status in ("running", "waiting")}

    def round(self):
        from .pathmap import map_path
        cfg = self.app.cfg
        qb = self.app._qbit()
        if qb is None:
            return
        back = [(b, a) for a, b in (cfg.local_to_qbit or [])]
        rows = qb.torrents()
        try:
            from . import btledger
            btledger.observe(cfg, rows)             # what each started torrent has downloaded
        except Exception:                           # bookkeeping never stops a round
            pass
        whole: dict[str, list[dict]] = {}
        for r in rows:
            if r.get("progress", 0) >= 1 and r.get("state") not in CHECKING:
                whole.setdefault(title(r.get("name") or ""), []).append(r)
        busy = self.busy()
        try:
            originals.prune(cfg)
        except OSError as e:
            print(f"mend: could not let go of old originals: {e}", flush=True)
        kept = originals.kept_roots(cfg)
        for r in rows:
            if not (r.get("state") in STOPPED and 0 < r.get("progress", 0) < 1 and r["hash"] not in busy):
                continue
            try:
                got = "none"
                if whole.get(title(r.get("name") or "")) or (r.get("name") or "") in kept:
                    got = self.mend(qb, r, whole.get(title(r["name"]), []), back, map_path)
                if got == "none":
                    self.download_rest(qb, r, back, map_path)
            except (ApiError, OSError) as e:
                self.state.note(f"{r['name']}: {e}")

    def tracker(self, r: dict) -> str:
        """The torrent's tracker, by the name Prowlarr gives it (the name Settings uses)."""
        if not r.get("tracker"):
            return ""
        from .clients import Prowlarr
        from .pipeline import tracker_name
        cfg = self.app.cfg
        pr = Prowlarr(cfg.prowlarr_url, cfg.prowlarr_key) if getattr(cfg, "prowlarr_url", "") else None
        return tracker_name([r["tracker"]], pr)

    def download_rest(self, qb, r: dict, back, map_path) -> bool:
        """Start a torrent nothing here can finish, to download the rest from its tracker -
        ticked "always add", within its limit, once (see the module's notes)."""
        cfg, h = self.app.cfg, r["hash"]
        if h in self.state.started:
            return False
        guard = getattr(self.app, "guard", None)
        if guard is not None and guard().holding():
            return False                                    # the disk is low
        tracker = self.tracker(r)
        want = config_mod.tracker_key(tracker)
        if not want or not any(config_mod.tracker_key(t) == want for t in cfg.nearly_auto_trackers or []):
            return False
        info = qb.info(h) or r
        if info.get("state") not in STOPPED:
            return False
        frac = info.get("progress", 0)
        total = info.get("total_size") or info.get("size")
        short = info.get("amount_left")
        if short is None and total:
            short = round(total * (1 - frac))
        before = btledger.taken(cfg, h, info)
        if not config_mod.within_limit(cfg, tracker, *btledger.need(frac, short, total, before),
                                       config_mod.AUTO_HEADROOM):
            return False
        # every short file needs bytes of its own before qBittorrent writes into it
        files = []
        for f in qb.files(h):
            if f.get("progress", 0) >= 1 or f.get("priority", 1) == 0:
                continue
            p = locate(info, f["name"], back, map_path)
            if p:
                files.append((f["name"], p))
        shared = [(n, p) for n, p in files if os.stat(p).st_nlink > 1]
        for d in {os.path.dirname(p) for _, p in shared}:
            room = sum(os.path.getsize(p) for _, p in shared if os.path.dirname(p) == d)
            if shutil.disk_usage(d).free < room * 1.05 + 1024 ** 3:
                self.state.started[h] = time.time()
                self.state.note(f"{r['name']}: not started - no room to give its short files a copy "
                                "of their own (they are shared with other files)")
                return False
        from .assemble import own_copy
        for n, p in files:
            try:
                originals.keep_before_writes(cfg, p, n, h, "downloaded into over BitTorrent by mend")
            except OSError:
                pass
            own_copy(p)                                     # nothing to do once it has its own
        btledger.record_start(cfg, h, tracker, info, short)
        qb.start(h)
        self.state.started[h] = time.time()
        self.state.note(f"{r['name']}: nothing here finishes it - downloading the last "
                        f"{(1 - frac) * 100:.4f}% from {tracker} (within its limit: "
                        f"{config_mod.limit_text(cfg, tracker)})")
        return True

    def mend(self, qb, r: dict, others: list[dict], back, map_path) -> str:
        """"done": finished; "tried": copies were tried (more may be left for the next
        round); "none": nothing here left to try."""
        h = r["hash"]
        before = {f["name"]: f for f in qb.files(h)}
        tried = set(self.state.tried.get(h, []))
        swaps = []                                          # (file name, local path, the copy's mark)
        listed: dict[str, dict] = {}
        for name, f in before.items():
            if f.get("progress", 0) >= 1 or f.get("priority", 1) == 0:
                continue
            dst = locate(r, name, back, map_path)
            if dst is None:
                continue

            def sources():
                """A kept original of this file first, then another torrent's copy. A torrent
                stuck a little short was usually linked from the files already here - the
                other torrents' version - so the version it has not had yet is the original
                kept before a repair or download changed them."""
                for p in originals.candidates(self.app.cfg, name, f.get("size")):
                    yield p, "the version kept before it was changed"
                for o in others:
                    if o["hash"] not in listed:
                        listed[o["hash"]] = {x["name"]: x for x in qb.files(o["hash"])}
                    x = same_file(listed[o["hash"]], name, f.get("size"))
                    p = x and locate(o, x["name"], back, map_path)
                    if p:
                        yield p, f"{o.get('name') or o['hash']}'s files"

            for src, label in sources():
                try:
                    ino = _inode(src)
                except OSError:
                    continue
                mark = f"{name}|{ino[0]}:{ino[1]}"
                if ino == _inode(dst) or mark in tried:
                    continue
                if os.stat(src).st_dev != os.stat(os.path.dirname(dst)).st_dev:
                    free = shutil.disk_usage(os.path.dirname(dst)).free
                    if free < f["size"] * 1.1 + 1024 ** 3:
                        continue                            # no room for a copy
                swaps.append((name, dst, src, mark, label))
                break
        if not swaps:
            return "none"
        if (qb.info(h) or {}).get("state") not in STOPPED:
            return "tried"                                  # started meanwhile
        self.state.pending = [[dst, dst + ORIG] for _, dst, *_ in swaps]
        self.state.save()
        done = []
        try:
            for name, dst, src, mark, _ in swaps:
                put_in(src, dst)
                done.append((name, dst))
                self.state.tried.setdefault(h, []).append(mark)
            qb.recheck(h)
            info = qb.wait_idle(h, timeout=12 * 3600, min_wait=10)
            after = {f["name"]: f for f in qb.files(h)}
            # no better: the old file goes back (it is the library's, or shared with a torrent)
            worse = [(n, d) for n, d in done
                     if after.get(n, {}).get("progress", 0) <= before[n].get("progress", 0)]
            for n, d in done:
                if (n, d) in worse:
                    put_back(d)
                else:
                    let_go(d)
            if worse:
                qb.recheck(h)
                info = qb.wait_idle(h, timeout=12 * 3600, min_wait=10)
        except Exception:
            for _, d in done:
                if os.path.lexists(d + ORIG):
                    put_back(d)
            raise
        finally:
            self.state.pending = []
            self.state.save()
        better = len(done) - len(worse)
        pct = info.get("progress", 0) * 100
        if info.get("progress", 0) >= 1:
            qb.start(h)
            self.state.note(f"{r['name']}: finished from {swaps[0][4]} - 100.0%, seeding "
                            f"(was {r['progress'] * 100:.4f}%)")
            lib = getattr(self.app, "library", None)
            if lib:                                         # a Library season that was short settles
                lib.state.scanned_at = 0
                lib.wake.set()
        else:
            self.state.note(f"{r['name']}: tried {len(done)} file(s) from {swaps[0][4]}, {better} "
                            f"better - now {pct:.4f}% (was {r['progress'] * 100:.4f}%)")
        return "done" if info.get("progress", 0) >= 1 else "tried"
