"""Automatic builds: torrents autobrr drops into a folder are built from Usenet by themselves.

autobrr (in the same network as the other services) fetches the .torrent from the tracker
and, through a "watch folder" action nzb2seed sets up on the filters you choose, saves it in
the inbox. nzb2seed never contacts the tracker. Each torrent becomes one automatic job:

* the Usenet post often appears minutes to hours after the announce, so the build is tried
  again every ``auto_retry_minutes`` until ``auto_wait_hours`` have passed. Both are
  short by default - a try at once, then every 2 minutes for 14 minutes - because a
  release that reaches Usenet at all is there within minutes of the torrent (often
  before it), while one that is missing after that is usually a tracker's own encode
  that will never be posted, and every further look only costs indexer searches;
* it never asks anything (no pick lists) and never downloads over BitTorrent: the torrent is
  added stopped, rechecked, and started only at exactly 100.0% (``auto_start``);
* at most ``auto_parallel`` builds run at once, and automatic builds make at most
  ``auto_searches_per_hour`` Prowlarr searches (manual work is not counted or slowed).

What happened to each torrent is kept in ``inbox.json`` next to the config, so a restart
picks up where it was. Handled .torrent files move to ``<inbox>/.done`` or ``.failed``.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time

from . import clients
from . import space
from .clients import ApiError
from .pipeline import Abort, Options, execute_run, local_release
from .report import Cancelled, check_cancel, info, step, warn
from .torrent import TorrentError, parse

POLL_SECONDS = 20
QUEUE, DONE, FAILED = ".queue", ".done", ".failed"
# a build that ended like this may work later, once the post is on Usenet
_NOT_YET = re.compile(r"no Usenet post could supply|nothing was downloaded|none found|"
                      r"do not contain every file|no other .* posts? of", re.I)

_auto = threading.local()                       # set in automatic jobs' threads


class Budget:
    """At most ``per_hour`` Prowlarr searches an hour for automatic builds; a search over the
    budget waits (cancellable) until the oldest of the last hour is an hour old."""

    def __init__(self, per_hour_fn):
        self.per_hour_fn = per_hour_fn
        self.times: list[float] = []
        self.lock = threading.Lock()

    def __call__(self):
        if not getattr(_auto, "on", False):
            return                              # manual searches are not budgeted
        while True:
            with self.lock:
                now = time.time()
                self.times = [t for t in self.times if now - t < 3600]
                limit = max(1, int(self.per_hour_fn() or 1))
                if len(self.times) < limit:
                    self.times.append(now)
                    return
                wait = 3600 - (now - self.times[0])
            info(f"search budget used ({limit} an hour) - waiting {int(wait // 60) + 1} min")
            _sleep(min(wait + 1, 60))


def _sleep(seconds: float):
    end = time.time() + seconds
    while time.time() < end:
        check_cancel()
        time.sleep(min(5, max(0.0, end - time.time())))


class State:
    """inbox.json: {infohash: {name, file, status, first_seen, attempts, next_try, why, job}}."""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        try:
            with open(path, encoding="utf-8") as fh:
                self.items: dict[str, dict] = json.load(fh).get("items", {})
        except (OSError, ValueError):
            self.items = {}
        for it in self.items.values():
            if it.get("status") == "building":      # the GUI stopped mid-build: try again
                it["status"] = "queued"

    def save(self):
        with self.lock:
            tmp = f"{self.path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"items": self.items}, fh, indent=1)
            os.replace(tmp, self.path)

    def update(self, h: str, **kw):
        with self.lock:
            self.items.setdefault(h, {}).update(kw)
        self.save()


def retry_gap(cfg, attempts: int) -> float:
    """How long to wait before looking for this release again, in seconds.

    Measured against real releases: one that reaches Usenet at all is there within
    minutes of the torrent - often before it - so the looks are close together, where
    they can actually find something. The gap doubles up to ``auto_retry_minutes``;
    with the two set the same (the default 2 minutes) it is simply a fixed gap."""
    first = max(0.01, float(cfg.auto_retry_first_minutes))   # never a busy loop
    cap = max(first, float(cfg.auto_retry_minutes))
    return min(first * 2 ** max(0, attempts - 1), cap) * 60


def detect_autobrr_folder() -> tuple[str, str] | None:
    """(inbox as this machine sees it, the same as autobrr sees it) - from the autobrr
    container's config mount, when autobrr runs in Docker on this machine."""
    try:
        out = subprocess.run(["docker", "ps", "--format", "{{.Names}}\t{{.Image}}"],
                             capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    names = [n for n, img in (line.split("\t", 1) for line in out.splitlines() if "\t" in line)
             if "autobrr" in img.lower() or n.lower() == "autobrr"]
    for name in names:
        try:
            mounts = json.loads(subprocess.run(["docker", "inspect", "-f", "{{json .Mounts}}", name],
                                               capture_output=True, text=True, timeout=20).stdout or "[]")
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
        for m in mounts:
            if m.get("Destination") == "/config" and m.get("Source"):
                return os.path.join(m["Source"], "nzb2seed-inbox"), "/config/nzb2seed-inbox"
    return None


class Inbox:
    def __init__(self, app, state_path: str):
        self.app = app
        self.state = State(state_path)
        self.budget = Budget(lambda: self.app.cfg.auto_searches_per_hour)
        clients.SEARCH_GATE = self.budget
        self.slots = threading.Semaphore(1)
        self.slot_count = 1
        self.running: dict[str, int] = {}       # infohash -> job id
        self._said_full = False                 # so the disk warning is said once
        self.stop = threading.Event()
        threading.Thread(target=self._loop, name="inbox", daemon=True).start()

    # -------------------------------------------------------------- the watcher
    def _loop(self):
        while not self.stop.wait(POLL_SECONDS):
            try:
                self.poll()
            except Exception as e:              # never let the watcher die
                print(f"inbox: {type(e).__name__}: {e}", flush=True)

    def poll(self):
        cfg = self.app.cfg
        if not cfg.auto_enabled or not cfg.auto_folder:
            return
        if self.no_room(cfg):
            return                      # the disk is too full: new builds wait their turn
        self._said_full = False
        folder = cfg.auto_folder
        os.makedirs(os.path.join(folder, QUEUE), exist_ok=True)
        if cfg.auto_parallel != self.slot_count:
            self.slots = threading.Semaphore(max(1, int(cfg.auto_parallel)))
            self.slot_count = cfg.auto_parallel
        for n in sorted(os.listdir(folder)):
            p = os.path.join(folder, n)
            # (a file younger than a few seconds may still be being written by autobrr)
            if n.lower().endswith(".torrent") and os.path.isfile(p) and time.time() - os.path.getmtime(p) > 5:
                self.take(p)
        # torrents waiting for their post whose job is gone (e.g. after a restart)
        for h, it in list(self.state.items.items()):
            if it.get("status") in ("queued", "waiting") and h not in self.running:
                self.start(h)

    def no_room(self, cfg) -> bool:
        """Is the free-space guard holding downloads back? Then nothing new is started -
        a build downloads tens of gigabytes, which is exactly what there is no room for."""
        if not space.limits_set(cfg):
            return False
        try:
            if not self.app.guard().holding():
                return False
        except (OSError, ValueError):
            return False
        if not self._said_full:
            self._said_full = True
            print("inbox: waiting for disk space before starting anything new", flush=True)
        return True

    def take(self, path: str):
        """Claim a new .torrent from the inbox (move it into .queue) and start its job."""
        folder = self.app.cfg.auto_folder
        try:
            with open(path, "rb") as fh:
                data = fh.read()
            t = parse(data)
        except (OSError, TorrentError, ValueError) as e:
            self._move(path, FAILED)
            print(f"inbox: {os.path.basename(path)} is not a usable .torrent ({e})", flush=True)
            return
        dst = os.path.join(folder, QUEUE, f"{t.infohash}.torrent")
        os.replace(path, dst)
        it = self.state.items.get(t.infohash)
        if it and it.get("status") in ("done", "queued", "waiting", "building"):
            return                              # the same torrent again: already handled
        self.state.update(t.infohash, name=t.name, file=dst, status="queued", first_seen=time.time(),
                          attempts=0, next_try=0, why="", source=os.path.basename(path))
        self.start(t.infohash)

    def _move(self, path: str, where: str):
        d = os.path.join(self.app.cfg.auto_folder, where)
        os.makedirs(d, exist_ok=True)
        try:
            os.replace(path, os.path.join(d, os.path.basename(path)))
        except OSError:
            pass

    # -------------------------------------------------------------- one torrent
    def start(self, h: str):
        it = self.state.items[h]
        job = self.app.start_job(f"auto: {it.get('name', h)}", "auto", lambda cfg: self.run(cfg, h))
        self.running[h] = job.id
        self.state.update(h, job=job.id)

    def run(self, cfg, h: str) -> dict:
        _auto.on = True
        try:
            return self._run(h)
        finally:
            _auto.on = False
            self.running.pop(h, None)

    def _run(self, h: str) -> dict:
        it = self.state.items[h]
        with open(it["file"], "rb") as fh:
            data = fh.read()
        t = parse(data)
        step(f"Automatic build of {t.name}")
        while True:
            cfg = self.app.cfg
            deadline = it.get("first_seen", time.time()) + cfg.auto_wait_hours * 3600
            wait = it.get("next_try", 0) - time.time()
            if wait > 0:
                info(f"next try at {time.strftime('%H:%M', time.localtime(it['next_try']))}")
                self.state.update(h, status="waiting")
                _sleep(wait)
            info("waiting for a free build slot" if self.slot_count > 1 else "waiting for the build slot")
            while not self.slots.acquire(timeout=5):
                check_cancel()
            try:
                attempts = it.get("attempts", 0) + 1
                self.state.update(h, status="building", attempts=attempts)
                opts = Options(unattended=True, start=cfg.auto_start)
                out = execute_run(cfg, opts, local_release(t, it["file"]), [], torrent_data=data)
            except Cancelled:
                self.state.update(h, status="failed", why="cancelled")
                self._move(it["file"], FAILED)
                raise
            except (Abort, ApiError, OSError, ValueError) as e:
                why = str(e)
                gap = retry_gap(cfg, attempts)
                # try again while still inside the window, so the last try lands on the
                # deadline rather than a gap short of it
                if _NOT_YET.search(why) and time.time() < deadline:
                    nxt = time.time() + gap
                    warn(f"not complete from Usenet yet ({why}) - trying again at "
                         f"{time.strftime('%H:%M', time.localtime(nxt))}")
                    self.state.update(h, status="waiting", next_try=nxt, why=why)
                    continue
                self.state.update(h, status="failed", why=why)
                self._move(it["file"], FAILED)
                raise Abort(f"gave up: {why}") from None
            finally:
                self.slots.release()
            self.state.update(h, status="done", why=out.get("result", "done"))
            self._move(it["file"], DONE)
            return out

    # -------------------------------------------------------------- for the GUI
    def items(self) -> list[dict]:
        out = [dict(v, infohash=h, running=h in self.running) for h, v in self.state.items.items()]
        return sorted(out, key=lambda x: -(x.get("first_seen") or 0))

    def retry(self, h: str):
        it = self.state.items.get(h)
        if not it or h in self.running:
            raise ValueError("nothing to retry")
        failed = os.path.join(self.app.cfg.auto_folder, FAILED, os.path.basename(it["file"]))
        if not os.path.exists(it["file"]) and os.path.exists(failed):
            os.makedirs(os.path.dirname(it["file"]), exist_ok=True)
            shutil.move(failed, it["file"])
        self.state.update(h, status="queued", first_seen=time.time(), next_try=0, why="")
        self.start(h)

    def forget(self, h: str):
        if h in self.running:
            raise ValueError("stop its job first")
        with self.state.lock:
            self.state.items.pop(h, None)
        self.state.save()
