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
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time

from . import originals
from .clients import CHECKING, ApiError

INTERVAL = 30 * 60
FIRST_ROUND = 3 * 60
STOPPED = {"stoppedDL", "pausedDL"}
ORIG = ".nzb2seed-orig"


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
        self.log: list = d.get("log", [])

    def save(self):
        tmp = f"{self.path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"tried": self.tried, "pending": self.pending, "log": self.log[-200:]}, fh, indent=1)
        os.replace(tmp, self.path)

    def note(self, text: str):
        print(f"mend: {text}", flush=True)
        self.log.append([time.time(), text])
        self.save()


def _inode(path: str):
    st = os.stat(path)
    return st.st_dev, st.st_ino


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
                whole.setdefault(r.get("name") or "", []).append(r)
        busy = self.busy()
        try:
            originals.prune(cfg)
        except OSError as e:
            print(f"mend: could not let go of old originals: {e}", flush=True)
        kept = originals.kept_roots(cfg)
        for r in rows:
            if (r.get("state") in STOPPED and 0 < r.get("progress", 0) < 1 and r["hash"] not in busy
                    and (whole.get(r.get("name") or "") or (r.get("name") or "") in kept)):
                try:
                    self.mend(qb, r, whole.get(r["name"], []), back, map_path)
                except (ApiError, OSError) as e:
                    self.state.note(f"{r['name']}: {e}")

    def mend(self, qb, r: dict, others: list[dict], back, map_path) -> bool:
        h = r["hash"]
        here = map_path(r.get("save_path") or "", back)
        before = {f["name"]: f for f in qb.files(h)}
        tried = set(self.state.tried.get(h, []))
        swaps = []                                          # (file name, local path, the copy's mark)
        listed: dict[str, dict] = {}
        for name, f in before.items():
            if f.get("progress", 0) >= 1 or f.get("priority", 1) == 0:
                continue
            dst = os.path.join(here, *name.split("/"))
            if not os.path.isfile(dst):
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
                    x = listed[o["hash"]].get(name)
                    if x and x.get("size") == f.get("size"):
                        yield (os.path.join(map_path(o.get("save_path") or "", back), *name.split("/")),
                               f"{o.get('name') or o['hash']}'s files")

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
            return False
        if (qb.info(h) or {}).get("state") not in STOPPED:
            return False                                    # started meanwhile
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
        return info.get("progress", 0) >= 1
