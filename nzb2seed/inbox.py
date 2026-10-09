"""Automatic builds: torrents autobrr drops into a folder are built from Usenet by themselves.

autobrr (in the same network as the other services) fetches the .torrent from the tracker
and, through a "watch folder" action nzb2seed sets up on the filters you choose, saves it in
the inbox. nzb2seed never contacts the tracker. Each torrent becomes one automatic job:

* the Usenet post often appears minutes to hours after the announce, so the build is tried
  again every ``auto_retry_minutes`` until ``auto_wait_hours`` have passed - by default
  a try at once, then every 15 minutes for an hour: a release that reaches Usenet at all
  is usually there within the hour (a fresh one can take half an hour or more), while one
  still missing after that is usually a tracker's own encode that will never be posted,
  and every further look only costs indexer searches;
* it never asks anything (no pick lists) and never downloads over BitTorrent: the torrent is
  added stopped, rechecked, and started only at exactly 100.0% (``auto_start``);
* at most ``auto_parallel`` builds run at once, and automatic builds make at most
  ``auto_searches_per_hour`` Prowlarr searches (manual work is not counted or slowed).

What happened to each torrent is kept in ``inbox.json`` next to the config, so a restart
picks up where it was. Handled .torrent files move to ``<inbox>/.done`` or ``.failed``.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import subprocess
import threading
import time

from . import clients
from . import space
from . import worth
from . import rules as rules_mod
from . import lookup as lookup_mod
from .clients import ApiError
from .pipeline import Abort, Incomplete, Options, execute_run, local_release
from .usenet_odds import odds_for
from .report import Cancelled, check_cancel, info, step, warn
from .torrent import TorrentError, parse

POLL_SECONDS = 20
STALE_HOURS = 24    # a torrent not yet building by then is stopped (it can still be tried again)
QUEUE, DONE, FAILED = ".queue", ".done", ".failed"
# a build that ended like this may work later, once the post is on Usenet
_NOT_YET = re.compile(r"no Usenet post could supply|nothing was downloaded|none found|"
                      r"do not contain every file|no other .* posts? of", re.I)

_auto = threading.local()                       # set in automatic jobs' threads


def age_text(minutes: float) -> str:
    """How long ago, in the unit that reads best: "40 minutes", "5 hours", "782 days"."""
    if minutes < 120:
        return f"{minutes:.0f} minute{'' if round(minutes) == 1 else 's'}"
    if minutes < 48 * 60:
        return f"{minutes / 60:.0f} hours"
    return f"{minutes / 1440:.0f} days"


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
        self.on_save = None
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
        if self.on_save:
            self.on_save()                     # the Automatic tab follows live

    def update(self, h: str, **kw):
        with self.lock:
            self.items.setdefault(h, {}).update(kw)
        self.save()

    def move_on(self, h: str, **kw) -> bool:
        """update(), unless the torrent was stopped meanwhile (Clear queue, the 24-hour
        stop, a full queue): then nothing changes and False says the job must end."""
        with self.lock:
            it = self.items.setdefault(h, {})
            if it.get("stopped"):
                return False
            it.update(kw)
        self.save()
        return True


def _ago(seconds: float) -> str:
    """ "35 days", "5 hours", "40 minutes" """
    for unit, n in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= n:
            k = int(seconds // n)
            return f"{k} {unit}{'s' if k != 1 else ''}"
    return "moments"


def retry_gap(cfg, attempts: int) -> float:
    """How long to wait before looking for this release again, in seconds.

    Measured against real releases: one that reaches Usenet at all is there within
    minutes of the torrent - often before it - so the looks are close together, where
    they can actually find something. The gap doubles up to ``auto_retry_minutes``;
    with the two set the same (the default 15 minutes) it is simply a fixed gap."""
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
        self.stop_stale()                   # first: a full disk must not keep old ones alive
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
        # torrents waiting for their post whose job is gone (e.g. after a restart),
        # highest priority first so a rule near the top of the list really is built first
        waiting = [(h, it) for h, it in self.state.items.items()
                   if it.get("status") in ("queued", "waiting") and h not in self.running]
        odds = odds_for(cfg)
        waiting.sort(key=lambda x: (rules_mod.sort_key(
            cfg, rules_mod.Release(name=x[1].get("name") or "", size=x[1].get("size") or 0,
                                   tracker=x[1].get("tracker") or "",
                                   first_seen=x[1].get("first_seen") or 0),
            x[1].get("first_seen") or 0)[0], -odds.rate(x[1].get("name") or ""), x[1].get("first_seen") or 0))
        for h, _ in waiting:
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
        never = odds_for(self.app.cfg).never(t.name) if self.app.cfg.auto_skip_unposted else None
        if never:
            # listed and stopped, so it can still be tried again - but it costs no search,
            # no build slot, and leaves no "no Usenet post" job behind
            self.state.update(t.infohash, name=t.name, file=dst, status="failed", first_seen=time.time(),
                              attempts=0, next_try=0, why=f"not tried: {never}", stopped=True,
                              source=os.path.basename(path), size=t.total_size,
                              tracker=worth.tracker_of(t.trackers))
            self._move(dst, FAILED)
            return
        cap = int(self.app.cfg.auto_queue_max or 0)
        full = cap > 0 and len(self.pending()) >= cap
        if full and not self.app.cfg.auto_queue_keep_older:
            # a fresh release usually pays back best: the oldest waiting ones make room
            # (stopped, still listed, and can be tried again)
            for h in sorted(self.pending(), key=lambda h: self.state.items[h].get("first_seen") or 0):
                if len(self.pending()) < cap:
                    break
                self.stop_one(h, "stopped: made room in the queue for a newer torrent")
            full = len(self.pending()) >= cap
        self.state.update(t.infohash, name=t.name, file=dst, status="queued", first_seen=time.time(),
                          attempts=0, next_try=0, why="", stopped=False, source=os.path.basename(path),
                          size=t.total_size, tracker=worth.tracker_of(t.trackers))
        if full:
            # listed, stopped, and can be tried again - but it does not join the queue
            self.stop_one(t.infohash, f"not queued: the queue already holds {cap}, the most "
                                      "allowed at a time, and older ones are kept (Automatic tab)")
            return
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
        fn = (lambda cfg: self.run(cfg, h))
        old = self.app.jobs.get(it.get("job") or -1)
        if old is not None and old.kind == "auto" and old.status == "interrupted":
            job = self.app.resume_job(old, fn)      # a restart cut it off: the same job goes on
        else:
            job = self.app.start_job(f"auto: {it.get('name', h)}", "auto", fn)
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
        rel = rules_mod.Release(name=t.name, size=t.total_size,
                                tracker=worth.tracker_of(t.trackers),
                                first_seen=it.get("first_seen") or time.time())
        limit = float(getattr(self.app.cfg, "auto_max_age_days", 0) or 0)
        known = None

        def look():
            """Ask Prowlarr what the tracker says about it - one search (remembered for a while)."""
            found = lookup_mod.find(self.app.prowlarr(), self.app.tracker_cache(), t.name,
                                    want_counts=rules_mod.wants_counts(self.app.cfg), log=warn)
            if found:
                rel.published, rel.seeders = found.published, found.seeders
                rel.leechers, rel.grabs = found.leechers, found.grabs
                if found.published:
                    info(f"the tracker posted it {age_text(found.age_min)} ago"
                         + (f", {found.seeders} seeders" if found.seeders >= 0 else "")
                         + (f", {found.leechers} leechers" if found.leechers >= 0 else "")
                         + (f", grabbed {found.grabs} times" if found.grabs >= 0 else ""))
            return found

        def skip(why: str) -> dict:
            warn(f"not building it: {why}")
            self.state.update(h, status="skipped", why=why)
            self._move(it["file"], DONE)
            return {"result": f"skipped - {why}"}

        # what costs no search is checked first: a release ruled out by anything free is
        # skipped without spending one. A rule that needs the tracker's numbers asks only
        # once its own free conditions hold - and at most once for the torrent.
        asked = []

        def fetch():
            nonlocal known
            if not asked:
                asked.append(True)
                known = look()
        call = rules_mod.decide(self.app.cfg, rel, fetch=fetch)
        if not call.build:
            return skip(call.why)
        if limit > 0:
            fetch()                       # the age limit: only now, if no rule asked already
            if known and known.published and known.age_min > limit * 1440:
                # not new to the tracker: autobrr sent an old release (a re-announce, a freeleech)
                return skip(f"the tracker posted it {age_text(known.age_min)} ago - older than your "
                            f"{limit:g}-day limit for automatic builds")
            if not (known and known.published):
                info("the tracker's posting time could not be found - built anyway, as there is no age to go by")
        if call.why:
            info(call.why)
        while True:
            cfg = self.app.cfg
            deadline = it.get("first_seen", time.time()) + cfg.auto_wait_hours * 3600
            wait = it.get("next_try", 0) - time.time()
            if wait > 0:
                info(f"next try at {time.strftime('%H:%M', time.localtime(it['next_try']))}")
                if not self.state.move_on(h, status="waiting"):
                    raise Cancelled("stopped")
                _sleep(wait)
            info("waiting for a free build slot" if self.slot_count > 1 else "waiting for the build slot")
            while not self.slots.acquire(timeout=5):
                check_cancel()
            attempts = it.get("attempts", 0) + 1
            if not self.state.move_on(h, status="building", attempts=attempts):
                self.slots.release()        # stopped while it waited: never starts building
                raise Cancelled("stopped")
            try:
                opts = Options(unattended=True, start=cfg.auto_start,
                               whole_posts=bool(it.get("whole_posts")))
                tor = local_release(t, it["file"])
                if rel.seeders is not None and rel.seeders >= 0:
                    tor = dataclasses.replace(tor, seeders=rel.seeders)
                out = execute_run(cfg, opts, tor, [], torrent_data=data)
            except Cancelled:
                self.state.update(h, status="failed", why="cancelled")
                self._move(it["file"], FAILED)
                raise
            except Incomplete as e:
                self.state.update(h, status="failed", why=str(e))
                self._move(it["file"], FAILED)
                raise
            except (Abort, ApiError, OSError, ValueError) as e:
                why = str(e)
                gap = retry_gap(cfg, attempts)
                # try again while still inside the window, so the last try lands on the
                # deadline rather than a gap short of it
                # an old release that is not on Usenet by now will not be in a few
                # minutes either: the quick retries are for fresh ones on their way
                age = time.time() - rel.published if rel.published else None
                old = age is not None and age > max(3600, cfg.auto_wait_hours * 3600)
                if _NOT_YET.search(why) and old:
                    warn(f"the tracker posted it {_ago(age)} ago - if it is not on Usenet by "
                         "now, a few more minutes will not change that; not trying again")
                elif _NOT_YET.search(why) and time.time() < deadline:
                    nxt = time.time() + gap
                    warn(f"not complete from Usenet yet ({why}) - trying again at "
                         f"{time.strftime('%H:%M', time.localtime(nxt))}")
                    if not self.state.move_on(h, status="waiting", next_try=nxt, why=why):
                        raise Cancelled("stopped") from None
                    continue
                self.state.update(h, status="failed", why=why)
                self._move(it["file"], FAILED)
                raise Abort(f"gave up: {why}") from None
            finally:
                self.slots.release()
            self.state.update(h, status="done", why=out.get("result", "done"))
            self._move(it["file"], DONE)
            return out

    # -------------------------------------------------------------- stopping
    def pending(self) -> list[str]:
        """Torrents that have not started building: queued, or waiting for their post."""
        return [h for h, it in self.state.items.items() if it.get("status") in ("queued", "waiting")]

    def stop_one(self, h: str, why: str) -> bool:
        """Stop a torrent that is not building yet. It stays in the list, stopped, and can
        be tried again - nothing is deleted. A build already under way is left alone."""
        it = self.state.items.get(h)
        if not it or it.get("status") not in ("queued", "waiting"):
            return False
        with self.state.lock:
            it = self.state.items.get(h)
            if not it or it.get("status") not in ("queued", "waiting"):
                return False                # it started building a moment ago: left alone
            it.update(status="failed", why=why, next_try=0, stopped=True)
        self.state.save()
        job = self.app.jobs.get(self.running.get(h) or it.get("job") or -1)
        if job is not None and h in self.running:
            job.cancel.set()                # it is only sleeping or waiting for a slot
        if it.get("file") and os.path.exists(it["file"]):
            self._move(it["file"], FAILED)  # where Try again looks for it
        return True

    def stop_pending(self) -> int:
        """The Clear queue button: stop everything that has not started building."""
        return sum(self.stop_one(h, "stopped: the queue was cleared") for h in self.pending())

    def stop_stale(self) -> int:
        """Stop what has waited STALE_HOURS without building - probably too old to be worth
        it by now, but kept so it can still be tried again."""
        end = time.time() - STALE_HOURS * 3600
        return sum(self.stop_one(h, f"stopped: not built within {STALE_HOURS} hours of arriving")
                   for h in self.pending()
                   if (self.state.items[h].get("first_seen") or time.time()) < end)

    # -------------------------------------------------------------- for the GUI
    def items(self) -> list[dict]:
        out = [dict(v, infohash=h, running=h in self.running) for h, v in self.state.items.items()]
        return sorted(out, key=lambda x: -(x.get("first_seen") or 0))

    def retryable_job(self, job_id: int) -> str | None:
        """The torrent an automatic job was building, if that job was its last and it ended
        without a result - so Try again on the Jobs tab can do what it does here."""
        for h, it in self.state.items.items():
            if it.get("job") == job_id and it.get("status") == "failed" and h not in self.running:
                return h
        return None

    def retry(self, h: str, whole_posts: bool = False):
        """Try a torrent again - ``whole_posts``: trying whole posts where the last build
        settled for small files missing (a plain Try again goes back to settling)."""
        it = self.state.items.get(h)
        if not it or h in self.running:
            raise ValueError("nothing to retry")
        failed = os.path.join(self.app.cfg.auto_folder, FAILED, os.path.basename(it["file"]))
        if not os.path.exists(it["file"]) and os.path.exists(failed):
            os.makedirs(os.path.dirname(it["file"]), exist_ok=True)
            shutil.move(failed, it["file"])
        old = self.app.jobs.get(it.get("job") or -1)
        self.state.update(h, status="queued", first_seen=time.time(), next_try=0, why="", stopped=False,
                          whole_posts=whole_posts)
        self.start(h)
        if old is not None and old.status != "running" and not old.retried_as:
            old.retried_as = self.state.items[h].get("job")     # "tried again as job N"
            self.app.store.changed(urgent=True)

    def forget(self, h: str):
        if h in self.running:
            raise ValueError("stop its job first")
        # removing it from the list never changes what automatic removal may delete later
        from . import retention
        retention.mark_automatic(self.app.cfg, h)
        with self.state.lock:
            self.state.items.pop(h, None)
        self.state.save()
