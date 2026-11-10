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

A torrent cross-eyed has a job open on (anything but done or rolled back - a failed one
too: it can leave a safety copy linked to the files, or a torrent stopped on purpose) is
left alone, as the files of others' copies to try, and mend says which job it waits on -
and when cross-eyed is in dry-run, that the job only runs once you run it there.

Before downloading the rest, mend asks cross-eyed (its download-check, worked out from
piece hashes): "repairable" - cross-eyed repairs it from other copies on disk, nothing is
downloaded; "in_job" - left alone; "shared_breaks_others" (its files are shared with
torrents that verify those bytes as they are) - it gets a copy of its own of each file it
writes into and downloads the rest, one torrent of a name at a time: the others of that name
wait (up to LEAD_WAIT) for it to be whole, then its files are swapped into them as above;
"shared_same_version" - downloaded into the
shared files, which fixes them all; "not_shared", "shared_other_names" (a library or
backup holds the files too) or no answer - each file it writes into gets bytes of its own
first. cross-eyed not answering: no files are swapped in that round either.

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
CHECK_AGAIN = 6 * 3600       # cross-eyed's download-check (and a repair asked for) kept this long
FIRST_ROUND = 3 * 60
LEAD_WAIT = 24 * 3600        # a same-named torrent downloading the rest is waited for this long
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
        self.waiting: dict = d.get("waiting", {})             # what was last said of each torrent left alone
        self.checked: dict = d.get("checked", {})             # infohash -> cross-eyed's last download-check
        self.repairs: dict = d.get("repairs", {})             # infohash -> when cross-eyed was asked to repair it
        self.log: list = d.get("log", [])
        self.on_note = None                                   # told of each note (the page follows the log)

    def save(self):
        tmp = f"{self.path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"tried": self.tried, "pending": self.pending, "started": self.started,
                       "waiting": self.waiting, "checked": self.checked, "repairs": self.repairs,
                       "log": self.log[-200:]}, fh, indent=1)
        os.replace(tmp, self.path)

    def note(self, text: str):
        print(f"mend: {text}", flush=True)
        self.log.append([time.time(), text])
        self.save()
        if self.on_note:
            try:
                self.on_note()
            except Exception:                   # noqa: BLE001 - telling the page never stops mend
                pass


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

    @property
    def flagged(self) -> dict[str, str]:
        """What was last said of each torrent left alone (said again only when it changes):
        its infohash, or "src:" + it for one not used as another's source; "" for cross-eyed
        not answering. Kept in mend.json, so a restart still says when one is picked up again."""
        return self.state.waiting

    def say(self, key: str, text: str):
        if self.flagged.get(key) != text:
            self.flagged[key] = text
            self.state.note(text)

    def picked_up(self, rows: list[dict], moving: dict):
        """Torrents left alone for cross-eyed whose job is over now: said, and forgotten."""
        names = {r["hash"].lower(): r.get("name") or r["hash"][:8] for r in rows}
        for key in [k for k in self.flagged if k and not k.startswith("dl:")]:
            h = key[4:] if key.startswith("src:") else key
            if h.lower() in moving:
                continue
            self.flagged.pop(key, None)
            if h.lower() in names:
                self.state.note(f"{names[h.lower()]}: cross-eyed's job on it is over - "
                                + ("mend can use its files again" if key.startswith("src:") else
                                   "mend looks at it again"))
        self.state.save()

    def cross_eyed(self, cfg):
        """({infohash: open job}, dry-run?) from cross-eyed; ({}, False) when it is not set
        up; None when it is set up and did not answer (said once until it answers again)."""
        url = getattr(cfg, "library_cross_eyed_url", "")
        if not url:
            return {}, False
        from .library import CrossEyed
        try:
            out = CrossEyed(url, getattr(cfg, "library_cross_eyed_prefix", "")).open_jobs()
        except (ApiError, OSError, ValueError) as e:
            if self.flagged.get("") != "down":
                self.flagged[""] = "down"
                self.state.note(f"cross-eyed did not answer ({e}) - until it does, no files are swapped in "
                                "and a download only goes into files of their own")
            return None
        self.flagged.pop("", None)
        return out

    def left_alone(self, r: dict, job: dict, dry: bool):
        """Say (once, until it changes) why a torrent is left alone."""
        state = job.get("state") or "?"
        what = f"cross-eyed job {job.get('id')} ({job.get('job_type') or 'move'})"
        if dry and state not in ("failed", "awaiting_user", "mirror_failed", "delete_interrupted"):
            text = (f"{r['name']}: left alone - {what} is waiting to run, and cross-eyed is in dry-run: "
                    "it only runs once you run it there or switch cross-eyed to live")
        else:
            text = (f"{r['name']}: left alone - {what} is {state.replace('_', ' ')}"
                    + ("; a failed job can leave a safety copy linked to its files" if state == "failed" else "")
                    + ". Once it is done, rolled back or cleared in cross-eyed, mend looks at it again")
        self.say(r["hash"], text)

    def round(self):
        from .pathmap import map_path
        cfg = self.app.cfg
        qb = self.app._qbit()
        if qb is None:
            return
        ce = self.cross_eyed(cfg)
        down = ce is None                           # set up, but not answering
        moving, dry = ({}, False) if down else ce
        back = [(b, a) for a, b in (cfg.local_to_qbit or [])]
        rows = qb.torrents()
        self.picked_up(rows, moving)
        try:
            from . import btledger
            btledger.observe(cfg, rows)             # what each started torrent has downloaded
        except Exception:                           # bookkeeping never stops a round
            pass
        whole: dict[str, list[dict]] = {}
        held: dict[str, list[dict]] = {}            # whole, but cross-eyed is working on them
        for r in rows:
            if r.get("progress", 0) >= 1 and r.get("state") not in CHECKING:
                (held if r["hash"].lower() in moving else whole).setdefault(
                    title(r.get("name") or ""), []).append(r)
        busy = self.busy()
        try:
            originals.prune(cfg)
        except OSError as e:
            print(f"mend: could not let go of old originals: {e}", flush=True)
        kept = originals.kept_roots(cfg)
        self.rows = rows                            # finish_rest looks for a same-named lead
        for r in rows:
            if not (r.get("state") in STOPPED and 0 < r.get("progress", 0) < 1 and r["hash"] not in busy):
                continue
            if r["hash"].lower() in moving:
                self.left_alone(r, moving[r["hash"].lower()], dry)
                continue
            for o in held.get(title(r.get("name") or ""), []):
                j = moving[o["hash"].lower()]
                self.say("src:" + o["hash"],
                         f"{o['name']}: the complete copy is not used to finish the stopped one - "
                         f"cross-eyed job {j.get('id')} ({j.get('job_type') or 'move'}) is "
                         f"{(j.get('state') or '?').replace('_', ' ')}. Once it is done, rolled back or cleared "
                         "in cross-eyed, mend can use its files again")
            try:
                got = "none"
                if not down and (whole.get(title(r.get("name") or "")) or (r.get("name") or "") in kept):
                    got = self.mend(qb, r, whole.get(title(r["name"]), []), back, map_path)
                if got == "none":
                    self.finish_rest(qb, r, back, map_path, down)
            except (ApiError, OSError) as e:
                self.state.note(f"{r['name']}: {e}")

    def finish_rest(self, qb, r: dict, back, map_path, down: bool):
        """Nothing mend has finishes it: cross-eyed is asked first (see module notes) - it
        may repair the torrent from other copies on disk, or say whether downloading the
        rest would change files other torrents need as they are."""
        cfg, h = self.app.cfg, r["hash"]
        v = None if down else self.ask_cross_eyed(cfg, r)
        verdict, summary = (v or {}).get("verdict"), (v or {}).get("summary") or ""
        if verdict == "complete":
            return
        if verdict == "in_job":
            self.say("dl:" + h, f"{r['name']}: left alone - {summary}")
            return
        if verdict == "repairable":
            self.ask_repair(cfg, r, summary)
            return
        if verdict == "shared_breaks_others":
            # its version of a shared file differs from the one the others verify: it gets a
            # copy of its own and downloads the rest. One torrent of a name at a time - the
            # others of that name are usually short of the same bytes, and once the lead is
            # whole its files are swapped into them (mend), instead of a copy each.
            lead = self.lead(r)
            if lead:
                self.say("dl:" + h, f"{r['name']}: waiting for the copy on {lead} to finish "
                                    "downloading the rest - its files are then tried here first")
                return
            if not self.download_rest(qb, r, back, map_path):
                self.say("dl:" + h, f"{r['name']}: not downloaded - {summary}; giving it a copy of "
                                    "its own and downloading the rest needs its tracker ticked "
                                    "\"always add\", within its limit, and room on the drive")
            return
        # shared with torrents needing the same bytes: written where they are, fixing them
        # all. Anything else - its own files, files a library or backup holds too, or no
        # answer - gets bytes of its own first: never written into a file with other names
        self.download_rest(qb, r, back, map_path, in_place=verdict == "shared_same_version")

    def lead(self, r: dict) -> str:
        """Another torrent of the same name that mend started downloading the rest, not yet
        whole and started under LEAD_WAIT ago: its tracker ("" - none to wait for)."""
        name = title(r.get("name") or "")
        for o in getattr(self, "rows", None) or []:
            h = o["hash"]
            if h == r["hash"] or h not in self.state.started or o.get("progress", 0) >= 1:
                continue
            if title(o.get("name") or "") == name and time.time() - self.state.started[h] < LEAD_WAIT:
                return self.tracker(o) or o.get("tracker") or h
        return ""

    def ask_cross_eyed(self, cfg, r: dict) -> dict | None:
        """cross-eyed's download-check, kept a while (the same progress, under CHECK_AGAIN):
        its piece check reads the files. None: not set up, or no answer."""
        url = getattr(cfg, "library_cross_eyed_url", "")
        if not url:
            return None
        h, prog = r["hash"], round(float(r.get("progress", 0)), 6)
        c = self.state.checked.get(h)
        if c and c.get("progress") == prog and time.time() - c.get("at", 0) < CHECK_AGAIN \
                and c.get("verdict") != "in_job":
            return c
        from .library import CrossEyed
        try:
            d = CrossEyed(url, getattr(cfg, "library_cross_eyed_prefix", "")).download_check(h)
        except (ApiError, OSError, ValueError):
            return None
        c = {"verdict": d.get("verdict") or "unknown", "summary": d.get("summary") or "",
             "job_id": d.get("job_id"), "progress": prog, "at": time.time()}
        self.state.checked[h] = c
        self.state.save()
        return c

    def ask_repair(self, cfg, r: dict, summary: str):
        """cross-eyed has the missing pieces right in other copies on disk: it repairs the
        torrent - nothing downloaded, nothing counted on the tracker. Asked once a while."""
        h = r["hash"]
        if time.time() - self.state.repairs.get(h, 0) < CHECK_AGAIN:
            return
        from .library import CrossEyed
        self.state.repairs[h] = time.time()
        try:
            d = CrossEyed(cfg.library_cross_eyed_url, getattr(cfg, "library_cross_eyed_prefix", "")).repair(h)
        except (ApiError, OSError) as e:
            self.state.note(f"{r['name']}: {summary} - but cross-eyed would not repair it: {e}")
            return
        self.state.note(f"{r['name']}: {summary} - cross-eyed is repairing it from them (job "
                        f"{d.get('job_id')}); nothing is downloaded"
                        + ("; cross-eyed is in dry-run, so the repair waits until you run it there or "
                           "switch it to live" if d.get("dry_run") else ""))

    def tracker(self, r: dict) -> str:
        """The torrent's tracker, by the name Prowlarr gives it (the name Settings uses)."""
        if not r.get("tracker"):
            return ""
        from .clients import Prowlarr
        from .pipeline import tracker_name
        cfg = self.app.cfg
        pr = Prowlarr(cfg.prowlarr_url, cfg.prowlarr_key) if getattr(cfg, "prowlarr_url", "") else None
        return tracker_name([r["tracker"]], pr)

    def download_rest(self, qb, r: dict, back, map_path, in_place: bool = False) -> bool:
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
        if not in_place:
            # every file qBittorrent may write into gets bytes of its own first (see originals)
            _, why = originals.ready_to_download(cfg, qb, h, lambda n: locate(info, n, back, map_path),
                                                 "downloaded into over BitTorrent by mend")
            if why:
                self.say("dl:" + h, f"{r['name']}: not started - {why}")
                return False
        btledger.record_start(cfg, h, tracker, info, short)
        qb.start(h)
        self.state.started[h] = time.time()
        self.state.note(f"{r['name']}: nothing here finishes it - downloading the last "
                        f"{(1 - frac) * 100:.4f}% from {tracker} (within its limit: "
                        f"{config_mod.limit_text(cfg, tracker)})"
                        + ("; into the files it shares with other torrents, which need the same bytes"
                           if in_place else ""))
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
