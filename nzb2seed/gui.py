"""Local web GUI: python -m nzb2seed gui

A small JSON API over the same pipeline the CLI uses, plus one static page.
Each build runs in its own thread and reports into its own job log.
"""
from __future__ import annotations

import base64
import dataclasses
import glob
import hmac
import ipaddress
import json
import math
import os
import re
import socket
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import urlsplit

from . import config as config_mod
from . import report
from .clients import ApiError, Autobrr, Prowlarr, QBittorrent, Release, SABnzbd
from . import inbox as inbox_mod
from . import metadata
from . import season as season_mod
from . import series as series_mod
from . import episodes as episodes_mod
from . import retention as retention_mod
from . import space as space_mod
from . import worth as worth_mod
from . import rules as rules_mod
from . import events as events_mod

# the build slot this thread's build holds, if any: handed back while it waits for you
_held = threading.local()


def take_slot(slots: threading.Semaphore, why: str = "Waiting for a build slot"):
    """Queue for a build slot (a build waiting for one can still be cancelled)."""
    if not slots.acquire(blocking=False):
        report.step(why)
        while not slots.acquire(timeout=5):
            report.check_cancel()
    _held.slots = slots


def give_slot():
    """Hand back the slot this thread holds (nothing if it holds none)."""
    slots, _held.slots = getattr(_held, "slots", None), None
    if slots is not None:
        slots.release()
    return slots
from . import torrent as torrent_mod
from . import matching
from . import usenet_odds as odds_mod
from . import pipeline as pipeline_mod
from . import lookup as lookup_mod
from .torrent import TorrentError, parse as parse_torrent
from .pathmap import map_path
from .pipeline import (Abort, Incomplete, Options, execute_assemble, execute_run, group_selection, pair,
                       previous_downloads, search)

MAX_BODY = 64 << 20
MAX_LINES = 20000


# ---------------------------------------------------------------- jobs

RESUMING = "the GUI stopped while this build was running; resuming"
INTERRUPTED = ("the GUI stopped while this build was running; build it again - files it "
               "already placed are reused and a stopped torrent stays stopped")


class Job:
    def __init__(self, id: int, title: str, kind: str, on_change=None):
        self.id = id
        self.title = title
        self.kind = kind
        self.status = "running"      # running | done | failed | cancelled | interrupted
        self.result = ""
        self.lines: list[dict] = []
        self.progress = ""
        self.pieces = ""
        self.started = time.time()
        self.ended: float | None = None
        self.cancel = threading.Event()
        self.lock = threading.Lock()
        self.on_change = on_change or (lambda urgent=False: None)
        self.question: dict | None = None     # {"prompt", "choices"} while waiting for the person
        self.infohash: str = ""               # the torrent it builds, once known
        self.repeat: dict | None = None       # the request that started it, so it can be run again
        self.extra: dict | None = None        # how far a failed build got (see pipeline.Incomplete)
        self.retried_as: int | None = None    # the job that tried this one again, if any
        self.cut_off = False                  # running when the last restart stopped it
        self._answer: int | None = None
        self._answered = threading.Event()

    # report sink
    def emit(self, kind: str, text: str):
        with self.lock:
            self.progress = ""
            if len(self.lines) < MAX_LINES:
                self.lines.append({"k": kind, "t": text})
        self.on_change()

    def progress_fn(self, text: str):
        self.progress = text
        self.on_change()

    def end_progress(self):
        if self.progress:
            self.emit("info", self.progress)

    def pieces_fn(self, states: str):
        self.pieces = states
        self.on_change()

    def cancelled(self) -> bool:
        return self.cancel.is_set()

    def ask(self, prompt: str, choices: list[dict]) -> int | None:
        """Pause the build until the person picks a choice (or stops the build)."""
        self._answer = None
        self._answered.clear()
        self.question = {"prompt": prompt, "choices": choices}
        self.status = "waiting"
        self.emit("warn", "waiting for you: " + prompt)
        slots = give_slot()             # waiting on you, not building: another build can run
        self.on_change(True)
        while not self._answered.wait(1.0):
            if self.cancel.is_set():
                break
        self.question = None
        self.status = "running"
        self.on_change(True)
        if self.cancel.is_set():
            raise report.Cancelled("cancelled")
        if slots is not None and self._answer is not None:
            # answered: back in the queue for a slot, like any build that has not started
            take_slot(slots, "Waiting for a build slot to carry on with your pick")
        return self._answer

    def answer(self, choice: int | None):
        if self.question is None:
            raise ValueError("this build is not waiting for an answer")
        if choice is not None and not (0 <= choice < len(self.question["choices"])):
            raise ValueError("no such choice")
        self._answer = choice
        self._answered.set()

    def summary(self) -> dict:
        return {"id": self.id, "title": self.title, "kind": self.kind, "status": self.status,
                "result": self.result, "started": self.started, "ended": self.ended,
                "progress": self.progress, "question": self.question,
                "can_retry": bool(self.repeat) and not self.retried_as,
                "retried_as": self.retried_as, "extra": self.extra,
                "steps": [x["t"] for x in self.lines if x["k"] == "step"]}

    def to_dict(self) -> dict:
        with self.lock:
            return {**self.summary(), "lines": list(self.lines), "pieces": self.pieces,
                    "repeat": self.repeat, "extra": self.extra, "retried_as": self.retried_as,
                    "infohash": self.infohash}

    @classmethod
    def from_dict(cls, d: dict, on_change) -> "Job":
        job = cls(d["id"], d.get("title", ""), d.get("kind", "build"), on_change)
        job.status, job.result = d.get("status", "done"), d.get("result", "")
        job.lines, job.pieces = d.get("lines", []), d.get("pieces", "")
        job.progress, job.started, job.ended = d.get("progress", ""), d.get("started", 0), d.get("ended")
        job.question = None
        job.repeat = d.get("repeat")
        job.extra = d.get("extra")
        job.retried_as = d.get("retried_as")
        job.infohash = d.get("infohash") or ""
        job.cut_off = job.status in ("running", "waiting")
        if job.cut_off:                              # its thread died with the previous GUI process
            job.status, job.result, job.progress = "interrupted", INTERRUPTED, ""
            job.ended = job.ended or time.time()
            job.lines.append({"k": "warn", "t": INTERRUPTED})
        return job


class JobStore:
    """Keeps the job list in a JSON file: written ~once a second while anything changes,
    immediately when a job starts or ends, and on shutdown."""

    def __init__(self, path: str, jobs_fn):
        self.path = path
        self.jobs_fn = jobs_fn
        self.on_change = None                 # told of every change (the pages' live feed)
        self.dirty = threading.Event()
        self.write_lock = threading.Lock()
        threading.Thread(target=self._loop, name="job-store", daemon=True).start()

    def load(self) -> list[dict]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                return json.load(fh).get("jobs", [])
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            print(f"could not read {self.path} ({e}); starting with an empty job list", flush=True)
            return []

    def changed(self, urgent: bool = False):
        if self.on_change:
            self.on_change()
        if urgent:
            try:
                self.flush()
            except OSError as e:     # never let bookkeeping break a build
                print(f"could not save jobs to {self.path}: {e}", flush=True)
                self.dirty.set()
        else:
            self.dirty.set()

    def flush(self):
        with self.write_lock:
            self.dirty.clear()
            data = {"jobs": [j.to_dict() for j in sorted(self.jobs_fn(), key=lambda j: j.id)]}
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = f"{self.path}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, self.path)

    def _loop(self):
        while True:
            self.dirty.wait()
            time.sleep(1.0)              # coalesce a burst of log lines into one write
            try:
                self.flush()
            except OSError as e:
                print(f"could not save jobs to {self.path}: {e}", flush=True)


class _Sink:
    """Adapter so a Job can be the thread's report sink."""

    def __init__(self, job: Job):
        self.job = job

    def emit(self, kind, text):
        self.job.emit(kind, text)

    def progress(self, text):
        self.job.progress_fn(text)

    def end_progress(self):
        self.job.end_progress()

    def pieces(self, states):
        self.job.pieces_fn(states)

    def ask(self, prompt, choices):
        return self.job.ask(prompt, choices)

    def cancelled(self):
        return self.job.cancelled()

    def torrent(self, infohash):
        self.job.infohash = infohash
        self.job.on_change()


class App:
    def __init__(self, config_path: str | None):
        self.config_path = config_path
        self.cfg = config_mod.load_or_default(config_path)
        metadata.configure(self.cfg.flaresolverr_url, self.cfg.outbound_proxy)
        episodes_mod.configure_cache(self.cfg.path)
        self.jobs: dict[int, Job] = {}
        self.lock = threading.Lock()
        self._cleaning = threading.Lock()        # one download clean-up at a time
        self.events = events_mod.Events()
        self._clean_now = threading.Event()      # set when a build ends
        self.store = JobStore(os.path.join(os.path.dirname(os.path.abspath(self.cfg.path)), "jobs.json"),
                              lambda: list(self.jobs.values()))
        self.store.on_change = lambda: self.events.publish("jobs")
        for d in self.store.load():
            job = Job.from_dict(d, self.store.changed)
            self.jobs[job.id] = job
        if self.jobs:
            self.store.flush()           # persist any "interrupted" marks right away
        self._next = max(self.jobs, default=0) + 1

    # Builds queue behind this. Ticking twenty seasons starts twenty builds otherwise, and
    # each one holds a finished download on the staging disk while it waits its turn to
    # place files - which fills that disk with work nothing can finish.
    build_slots = threading.Semaphore(2)
    build_slots_size = 2

    def build_slot(self):
        """Resize the queue if the setting changed, and hand out a slot."""
        want = max(1, int(getattr(self.cfg, "build_parallel", 2) or 2))
        with self.lock:
            while self.build_slots_size < want:
                self.build_slots.release()
                self.build_slots_size += 1
            while self.build_slots_size > want:
                if not self.build_slots.acquire(blocking=False):
                    break                      # all in use; it shrinks as they finish
                self.build_slots_size -= 1
        return self.build_slots

    def nzb2seed_torrent_rows(self, rows: list[dict]) -> dict[str, dict]:
        """nzb2seed's own torrents in qBittorrent, as {"auto": {...}, "manual": {...}}, each
        {"rows": its torrents, "cross_seeds": how many cross-seeds were folded into them}.

        Automatic - the Automatic tab built it (its record or its list says so), or built
        the release first and it was finished by hand (Build tab, Look on other trackers).
        Manual - any other build of nzb2seed's: a record that says so or predates saying
        which, or a torrent with nzb2seed's category or tag.

        A cross-seed of a build's files takes no disk of its own: its upload is added to
        the build it came from - found by the same name and size, or, for one tagged or
        filed as a cross-seed, by name (a cross-seed is often named after the video file)."""
        norm = worth_mod._norm
        auto, manual = set(), set()
        for p in glob.glob(os.path.join(glob.escape(self.cfg.torrent_dir), "*.owned.json")):
            try:
                with open(p, encoding="utf-8") as fh:
                    src = json.load(fh).get("source")
            except (OSError, ValueError):
                continue
            (auto if src == "auto" else manual).add(os.path.basename(p)[:-len(".owned.json")].lower())
        box = getattr(self, "inbox", None)
        tried = set()                          # releases the Automatic tab took on, however it went
        for h, it in (box.state.items.items() if box else []):
            if it.get("status") == "done":
                auto.add(h.lower())
            if it.get("name"):
                tried.add(norm(it["name"]))
        cat = (self.cfg.qbit_category or "nzb2seed").lower()
        tags = {t.lower() for t in (self.cfg.qbit_tags or [])} or {"nzb2seed"}
        marks = lambda r: {t.strip().lower() for t in (r.get("tags") or "").split(",") if t.strip()}  # noqa: E731
        for r in rows:
            if (r.get("category") or "").lower() == cat or marks(r) & tags:
                manual.add((r.get("hash") or "").lower())
        manual -= auto
        by_hash = {(r.get("hash") or "").lower(): r for r in rows}
        for h in list(manual):
            if h in by_hash and norm(by_hash[h].get("name") or "") in tried:
                manual.discard(h)
                auto.add(h)                    # the Automatic tab's release, finished by hand
        ours = {h: dict(by_hash[h], copies=1) for h in auto | manual if h in by_hash}
        by_name: dict[tuple, str] = {}
        for h, r in ours.items():
            by_name.setdefault((norm(r.get("name") or ""), r.get("size") or 0), h)

        def origin(r) -> str | None:
            same = by_name.get((norm(r.get("name") or ""), r.get("size") or 0))
            if same:
                return same
            flagged = (r.get("category") or "").lower().startswith("cross-seed") or \
                any(t.endswith(".cross-seed") or t == "cross-seed" for t in marks(r))
            if not flagged:
                return None
            base = norm(re.sub(r"\.[A-Za-z0-9]{2,4}$", "", r.get("name") or ""))
            if len(base) < 8:
                return None
            hits = [h for h, o in ours.items()
                    if (n := norm(o.get("name") or "")) and (base == n or base.startswith(n) or n.startswith(base))]
            return max(hits, key=lambda h: len(ours[h].get("name") or "")) if hits else None

        folded = {"auto": 0, "manual": 0}
        for h, r in by_hash.items():
            if h in ours:
                continue
            o = origin(r)
            if o is None:
                continue
            ours[o]["uploaded"] = (ours[o].get("uploaded") or 0) + (r.get("uploaded") or 0)
            ours[o]["copies"] += 1
            folded["auto" if o in auto else "manual"] += 1
        return {kind: {"rows": [ours[h] for h in hashes if h in ours], "cross_seeds": folded[kind]}
                for kind, hashes in (("auto", auto), ("manual", manual))}

    def nearly_built(self, title: str) -> Job | None:
        """An earlier build of this release that stopped nearly complete and still has its
        Usenet downloads (not abandoned) - whichever tracker it came from."""
        want = matching.norm(title)
        with self.lock:
            return next((j for j in self.jobs.values()
                         if j.kind in ("build", "auto") and (j.extra or {}).get("have") is not None
                         and not (j.extra or {}).get("abandoned")
                         and matching.norm(j.title[len("auto: "):] if j.title.startswith("auto: ") else j.title) == want),
                        None)

    def active_build(self, title: str) -> Job | None:
        """A build of this torrent that has not ended yet (a second one would fight it)."""
        with self.lock:
            return next((j for j in self.jobs.values() if j.kind == "build" and j.title == title
                         and j.status in ("running", "waiting")), None)

    def autobrr_memo(self, memo=None) -> dict:
        """What nzb2seed changed in autobrr, so unticking a filter puts it back:
        {"switched_off": [action ids], "filters_were": {filter id: enabled before}}."""
        path = os.path.join(os.path.dirname(os.path.abspath(self.cfg.path)), "autobrr.json")
        if memo is None:
            try:
                with open(path, encoding="utf-8") as fh:
                    d = json.load(fh)
            except (OSError, ValueError):
                d = {}
            return {"switched_off": list(d.get("switched_off", [])),
                    "filters_were": {str(k): bool(v) for k, v in (d.get("filters_were") or {}).items()}}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"switched_off": sorted(set(memo["switched_off"])),
                       "filters_were": memo["filters_were"]}, fh)
        return memo

    def _learn(self, job: "Job"):
        """Was its release on Usenet? Built, or nearly built: yes. "No Usenet post": no."""
        if job.kind not in ("build", "auto"):
            return
        found = job.status == "done" or (job.status == "failed" and (job.extra or {}).get("have") is not None)
        missing = job.status == "failed" and odds_mod.NOT_ON_USENET.search(job.result or "")
        if found or missing:
            try:
                odds_mod.odds_for(self.cfg).record(job.title, bool(found))
            except OSError:
                pass                             # never let the tally break a build

    def seed_odds(self):
        """The first time: what the jobs and the Automatic tab already show."""
        outcomes = []
        for j in list(self.jobs.values()):
            if j.kind in ("build", "auto"):
                if j.status == "done" or (j.extra or {}).get("have") is not None:
                    outcomes.append((j.title, True))
                elif odds_mod.NOT_ON_USENET.search(j.result or ""):
                    outcomes.append((j.title, False))
        box = getattr(self, "inbox", None)
        for it in (box.state.items.values() if box else []):
            if it.get("status") == "done":
                outcomes.append((it.get("name") or "", True))
            elif odds_mod.NOT_ON_USENET.search(it.get("why") or ""):
                outcomes.append((it.get("name") or "", False))
        odds_mod.odds_for(self.cfg).seed(outcomes)

    def start_inbox(self):
        """The automatic-build watcher (idle while switched off on the Automatic tab)."""
        self.inbox = inbox_mod.Inbox(self, os.path.join(os.path.dirname(os.path.abspath(self.cfg.path)),
                                                        "inbox.json"))
        self.inbox.state.on_save = lambda: self.events.publish("auto")
        try:
            self.seed_odds()
        except OSError as e:
            print(f"could not learn from the history: {e}", flush=True)

    def demand_stats(self) -> "worth_mod.Stats":
        return worth_mod.Stats(os.path.join(os.path.dirname(os.path.abspath(self.cfg.path)),
                                            "demand.json"))

    def prowlarr(self):
        return Prowlarr(self.cfg.prowlarr_url, self.cfg.prowlarr_key) if self.cfg.prowlarr_url else None

    def tracker_cache(self) -> "lookup_mod.Cache":
        return lookup_mod.Cache(os.path.join(os.path.dirname(os.path.abspath(self.cfg.path)),
                                             "tracker-info.json"))

    def guard(self) -> "space_mod.Guard":
        return space_mod.Guard(os.path.join(os.path.dirname(os.path.abspath(self.cfg.path)),
                                            "space.json"))

    def _sab(self) -> SABnzbd | None:
        return SABnzbd(self.cfg.sab_url, self.cfg.sab_key) if self.cfg.sab_url else None

    def start_space_guard(self):
        """Watches free space every few minutes (idle while no limit is set).

        The gate a running build waits on is set here too: while the guard is holding
        downloads back, a build stops before its next big write instead of filling the
        disk it is already short of."""
        def gate(role: str = "") -> str:
            """Why a build waiting to write to ``role``'s disk cannot go on yet ("" = it can).
            With no role, any full disk holds it."""
            cfg = self.cfg
            held = self.guard()
            if not space_mod.limits_set(cfg) or not held.holding():
                return ""
            sab = self._sab()
            now = space_mod.check(cfg, sab, resuming=True)
            if role:
                mine = [d for d in now["disks"] if d["role"] == role and d["why"]]
                if not mine:
                    return ""                       # a different disk is full, not this one
                return "; ".join(f"{d['path']}: {d['why']}" for d in mine)
            if not now["low"]:
                # there is room again: let the queue go now rather than at the next sweep,
                # which could be five minutes away, and never claim "the disk is full"
                # about a disk that has room
                held.run(cfg, sab, self._qbit(), log=lambda t: print(f"[space] {t}", flush=True))
                return ""
            return now["why"]

        space_mod.GATE = gate

        def loop():
            while True:
                time.sleep(300)
                if not space_mod.limits_set(self.cfg):
                    continue
                try:
                    self.guard().run(self.cfg, self._sab(), self._qbit(),
                                     log=lambda t: print(f"[space] {t}", flush=True))
                except Exception as e:                       # never let it kill its thread
                    print(f"space check failed: {e}", flush=True)
        threading.Thread(target=loop, name="space", daemon=True).start()

    @staticmethod
    def tracker_key(name: str) -> str:
        """"sometracker (API)" and "Sometracker" are the same tracker."""
        n = (name or "").strip().lower()
        return n[:-6].strip() if n.endswith("(api)") else n

    def always_adds(self, tracker: str) -> bool:
        want = self.tracker_key(tracker)
        return bool(want) and any(self.tracker_key(t) == want
                                  for t in (self.cfg.nearly_auto_trackers or []))

    def nearly_limit_text(self) -> str:
        return f"{float(self.cfg.nearly_complete_percent or 5):g}% or {float(self.cfg.nearly_complete_mb or 200):g} MB"

    def short_enough(self, fraction_missing: float, bytes_missing: int | None) -> bool:
        """Is this little enough to fetch over BitTorrent: less than the percentage AND less
        than the megabytes - whichever limit comes first. Bytes unknown: the percentage only."""
        if fraction_missing * 100 >= float(self.cfg.nearly_complete_percent or 5):
            return False
        return bytes_missing is None or bytes_missing < float(self.cfg.nearly_complete_mb or 200) * 1024 ** 2

    def nearly_enough(self, extra: dict | None) -> bool:
        if not extra or extra.get("have") is None:
            return False
        if extra.get("seeders") == 0:
            return False            # nobody to download the rest from: it could never finish
        return self.short_enough(1 - float(extra["have"]), extra.get("short"))

    def finish_in_client(self, job: "Job", why: str = "", override: bool = False) -> "Job":
        """Hand a nearly-built torrent to qBittorrent to download the rest - the one place
        nzb2seed starts a torrent that is not 100% complete, and only because the person
        chose it, for this build or for its tracker. ``override``: they chose it knowing it
        is outside the limit (or that no seeders were reported), so the limit is not checked."""
        x = dict(job.extra or {})

        def run(cfg):
            qb = self._qbit()
            if qb is None:
                raise Abort("no qBittorrent is set up")
            if why:
                report.info(why)
            h = x["infohash"]
            if not qb.info(h):
                report.step("Adding it to qBittorrent")
                with open(x["torrent_path"], "rb") as fh:
                    qb.add_stopped(fh.read(), os.path.basename(x["torrent_path"]), x["save_path"],
                                   cfg.qbit_category, ",".join(cfg.qbit_tags))
                for _ in range(40):
                    if qb.info(h):
                        break
                    time.sleep(0.5)
                qb.wait_idle(h)
                report.step("Checking what is already there")
                qb.recheck(h)
                qb.wait_idle(h, min_wait=5)
            frac = (qb.info(h) or {}).get("progress", 0)
            have = pipeline_mod.exact_pct(frac)
            rest = pipeline_mod.exact_pct(1 - frac) if frac >= 1 else \
                f"{math.ceil((1 - frac) * 1000000) / 10000:.4f}%"
            # the recheck has the last word: data that went bad since the build (or was never
            # as good as it looked) must not turn "a few KB over BitTorrent" into a download
            # the tracker counts towards a hit-and-run - so it stays stopped
            info = qb.info(h) or {}
            total = info.get("total_size") or info.get("size")
            short = round(total * (1 - frac)) if total else None
            if not override and not self.short_enough(1 - frac, short):
                job.extra = {**(job.extra or {}), "have": frac, "in_client": True, "short": short}
                self.store.changed(urgent=True)
                raise Abort(f"qBittorrent's check found only {have} here, so {rest} would have to "
                            f"be downloaded - at or over your limit of {self.nearly_limit_text()}. Not "
                            "started: the torrent stays stopped in qBittorrent (nothing deleted)")
            report.info(f"{have} is already here from Usenet; downloading the other "
                        f"{rest} over BitTorrent")
            qb.start(h)
            job.extra = {**(job.extra or {}), "added": True}
            self.store.changed(urgent=True)
            report.info("its Usenet downloads are cleared once the torrent is complete")
            return {"result": f"in qBittorrent, downloading the last {rest}"}

        return self.start_job(f"Finish in qBittorrent: {job.title}", "client", run)

    # ------------------------------------------------ clearing what builds leave behind
    def clean_downloads(self, log=print) -> tuple[int, int]:
        """Clear the Usenet downloads of torrents already complete in qBittorrent, and the
        check/peek folders killed builds left (see pipeline.clean_seeded_downloads)."""
        qb, sab = self._qbit(), self._sab()
        if qb is None or sab is None:
            return 0, 0
        with self._cleaning:                     # one clean-up at a time
            n, freed = pipeline_mod.clean_seeded_downloads(self.cfg, sab, qb, log=log)
            keep, live, names = self.downloads_in_use()
            m, also = pipeline_mod.clean_unused_downloads(self.cfg, sab, qb, keep, live, names, log=log)
            _, more = pipeline_mod.remove_stray_peeks(self.cfg, sab)
            return n + m, freed + also + more

    def downloads_in_use(self) -> tuple[set[str], set[str], set[str]]:
        """What still needs Usenet downloads: the torrents being built or queued to be (live),
        those a job on the list can still be retried for (keep: failed, cancelled or
        interrupted - not abandoned, and not already tried again), and the releases of both
        (names, see pipeline.release_key: for old records, which may name another torrent).
        A job taken off the list needs nothing - even if the Automatic tab could try it again."""
        keep, live, names = set(), set(), set()
        name = lambda t: t[len("auto: "):] if t.startswith("auto: ") else t
        for j in list(self.jobs.values()):
            h = self.torrent_of(j)
            if j.status in ("running", "waiting"):
                if h:
                    live.add(h)
                names.add(pipeline_mod.release_key(name(j.title)))
            elif j.status in ("failed", "cancelled", "interrupted") and not (j.extra or {}).get("abandoned") \
                    and not j.retried_as:
                if h:
                    keep.add(h)
                names.add(pipeline_mod.release_key(name(j.title)))
        box = getattr(self, "inbox", None)
        if box is not None:
            for h, it in box.state.items.items():
                if it.get("status") in ("queued", "waiting", "building"):
                    live.add(h)                  # automatic torrents waiting their turn
                    names.add(pipeline_mod.release_key(it.get("name") or ""))
        return keep, live, names

    def start_cleaner(self):
        """After every build, and hourly: downloads of torrents that are done are cleared."""
        def loop():
            while True:
                try:
                    self.clean_downloads()
                except Exception as e:           # never let the cleaner die
                    print(f"download clean-up: {type(e).__name__}: {e}", flush=True)
                self._clean_now.wait(3600)
                self._clean_now.clear()
        threading.Thread(target=loop, name="cleaner", daemon=True).start()

    # what the request being served asked for, so a job can be started again later
    request = threading.local()

    def start_sab_category(self):
        """Give nzb2seed its own SABnzbd category - its own download folder, +Repair, never
        +Delete - so its downloads are not mixed in with other apps'. Checked at start-up
        (in the background, retrying while SABnzbd is not answering); an existing category
        of that name is left as it is."""
        def loop():
            while True:
                cfg = self.cfg
                name = (cfg.sab_category or "").strip()
                if not name or name == "*" or not cfg.sab_url:
                    return
                try:
                    if SABnzbd(cfg.sab_url, cfg.sab_key).ensure_category(name, name):
                        print(f"created SABnzbd category {name!r}: new downloads go to its own "
                              f"folder ({name}), +Repair", flush=True)
                    return
                except (ApiError, OSError) as e:
                    print(f"SABnzbd category check: {e} - trying again in a minute", flush=True)
                    time.sleep(60)
        threading.Thread(target=loop, name="sab-category", daemon=True).start()

    def start_retention(self):
        """Hourly sweep that removes builds past their retention age (idle while off)."""
        def loop():
            while True:
                time.sleep(3600)
                cfg = self.cfg
                if not cfg.retention_enabled:
                    continue
                try:
                    out = retention_mod.sweep(cfg, self._qbit(), log=self._retention_log)
                except Exception as e:                       # never let the sweep kill its thread
                    print(f"retention sweep failed: {e}", flush=True)
                    continue
                if out["removed"]:
                    self._retention_log(
                        f"removed {len(out['removed'])} build(s), freeing "
                        f"{retention_mod.gb(out['bytes'])}")
        threading.Thread(target=loop, name="retention", daemon=True).start()

    def _retention_log(self, text: str):
        print(f"[retention] {text}", flush=True)

    def _qbit(self) -> QBittorrent | None:
        cfg = self.cfg
        if not cfg.qbit_url:
            return None
        qb = QBittorrent(cfg.qbit_url, cfg.qbit_user, cfg.qbit_pass)
        qb.login()
        return qb

    def start_job(self, title: str, kind: str, fn) -> Job:
        with self.lock:
            job = Job(self._next, title, kind, self.store.changed)
            job.repeat = getattr(self.request, "asked", None)     # what to replay on a retry
            again = getattr(self.request, "retrying", None)
            if again is not None:
                again.retried_as = job.id                          # that one is tried once
                self.request.retrying = None
            self._next += 1
            self.jobs[job.id] = job
        self.store.changed(urgent=True)
        self._launch(job, fn)
        return job

    def torrent_of(self, job: "Job") -> str:
        """The infohash of the torrent a job built: recorded by the build, or by a build
        that stopped nearly complete, or read from the .torrent saved under its name."""
        h = job.infohash or (job.extra or {}).get("infohash") or ""
        if h or job.kind not in ("build", "auto"):
            return h
        name = job.title[len("auto: "):] if job.title.startswith("auto: ") else job.title
        safe = re.sub(r'[<>:"/\\|?*]', "_", name)
        try:
            with open(os.path.join(self.cfg.torrent_dir, f"{safe}.torrent"), "rb") as fh:
                return torrent_mod.parse(fh.read()).infohash
        except (OSError, ValueError, torrent_mod.TorrentError):
            return ""

    def summary(self, job: "Job") -> dict:
        """job.summary(), plus what only the app knows: an automatic job is tried again
        through the Automatic tab's queue, not by replaying a request."""
        s = job.summary()
        box = getattr(self, "inbox", None)
        if job.kind == "auto":
            s["can_retry"] = bool(box and not job.retried_as and box.retryable_job(job.id))
        return s

    def resume_builds(self) -> list[int]:
        """Carry on the Build-tab builds the last restart cut off, each as the same job - as
        the Automatic tab does with its own. They start again from their request: downloads
        SABnzbd finished (or is still making) meanwhile, NZBs already fetched and files
        already placed are all reused. Builds interrupted by an earlier restart are left be."""
        resumed = []
        for job in sorted(self.jobs.values(), key=lambda j: j.id):
            rq = job.repeat or {}
            if not (getattr(job, "cut_off", False) and job.kind == "build" and job.status == "interrupted"
                    and rq.get("path") == "/api/build" and not job.retried_as):
                continue
            job.cut_off = False
            try:
                title, run = build_job(self, dict(rq.get("body") or {}))
            except (KeyError, TypeError, ValueError, OSError) as e:
                job.lines.append({"k": "warn", "t": f"could not carry on by itself ({e}) - build it again"})
                continue
            if self.active_build(title):
                continue
            self.resume_job(job, run)
            resumed.append(job.id)
        if resumed:
            print(f"resumed {len(resumed)} build(s) the restart interrupted", flush=True)
        return resumed

    def resume_job(self, job: "Job", fn) -> "Job":
        """Carry on a job the last restart interrupted, as the same job: its list entry and
        log continue, instead of an "interrupted" one left behind next to a new copy."""
        with self.lock:
            if job.lines and job.lines[-1].get("t") == INTERRUPTED:
                job.lines[-1] = {"k": "warn", "t": RESUMING}
            else:
                job.lines.append({"k": "warn", "t": RESUMING})
            job.status, job.result, job.ended, job.progress = "running", "", None, ""
            job.cancel = threading.Event()
        self.store.changed(urgent=True)
        self._launch(job, fn)
        return job

    def _launch(self, job: "Job", fn):
        cfg = self.cfg

        def run():
            report.use(_Sink(job))
            try:
                out = fn(cfg) or {}
                job.result = out.get("result", "done")
                if out.get("extra"):
                    job.extra = {**(job.extra or {}), **out["extra"]}
                job.status = "done"
            except report.Cancelled:
                job.status, job.result = "cancelled", "cancelled"
                report.warn("cancelled - nothing further was changed; a stopped torrent stays stopped")
            except Incomplete as e:
                # nearly there: keep everything, and say how near
                job.status, job.result, job.extra = "failed", str(e), e.details()
                report.warn(str(e))
                if self.nearly_enough(job.extra) and self.always_adds(e.tracker):
                    miss = math.ceil((1 - e.have) * 1000000) / 10000     # never rounded to 0
                    report.info(f"{e.tracker} is set to take builds this close, so it goes to "
                                f"qBittorrent to download the missing {miss:.4f}%")
                    job.extra = {**job.extra, "auto": True}
                    self.finish_in_client(job, why=f"{e.tracker} is set to always add builds "
                                                   f"missing less than {self.nearly_limit_text()}")
            except (Abort, ApiError, ValueError, OSError) as e:
                job.status, job.result = "failed", str(e)
                report.warn(str(e))
            except Exception as e:  # keep the server alive and show what broke
                job.status, job.result = "failed", f"{type(e).__name__}: {e}"
                report.line(traceback.format_exc())
            finally:
                job.progress = ""
                job.ended = time.time()
                self.store.changed(urgent=True)
                self._clean_now.set()            # a build ended: clear what is done
                self._learn(job)
        threading.Thread(target=run, name=f"job-{job.id}", daemon=True).start()


# ---------------------------------------------------------------- HTTP

def _rel(d: dict) -> Release:
    fields = {f.name for f in dataclasses.fields(Release)}
    return Release(**{k: v for k, v in d.items() if k in fields})


UPLOAD_PREFIX = "upload:"


def _upload_path(cfg, infohash: str) -> str:
    return os.path.join(cfg.torrent_dir, "uploads", f"{infohash}.torrent")


def upload_torrent(cfg, name: str, data: bytes) -> Release:
    """Keep an uploaded .torrent and describe it like a search result, so the Build tab
    can pair and build it the same way. Its guid points back at the saved file."""
    t = parse_torrent(data)                     # raises TorrentError for anything else
    path = _upload_path(cfg, t.infohash)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return Release(title=t.name, protocol="torrent", indexer=os.path.basename(name or "uploaded.torrent"),
                   indexer_id=0, size=t.total_size, guid=UPLOAD_PREFIX + t.infohash, download_url="",
                   info_url="", publish_date="", grabs=None, seeders=None, files=len(t.real_files))


def uploaded_data(cfg, rel: Release) -> bytes | None:
    """The saved .torrent behind an uploaded result (None: it came from a search)."""
    if not rel.guid.startswith(UPLOAD_PREFIX):
        return None
    infohash = rel.guid[len(UPLOAD_PREFIX):]
    if not re.fullmatch(r"[0-9a-f]{40}", infohash):
        raise ValueError("not an uploaded torrent")
    try:
        with open(_upload_path(cfg, infohash), "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        raise ValueError("the uploaded .torrent is gone; upload it again") from None


def data_roots(cfg) -> list[str]:
    """The folders the GUI's folder chooser may show: the configured data folders (where
    finished torrents and downloads live), never the rest of the disk."""
    roots = [p[0] for p in (cfg.local_to_qbit or [])] + [p[1] for p in (cfg.sab_to_local or [])]
    roots += [cfg.output_dir] if cfg.output_dir else []
    out = []
    for r in roots:
        r = os.path.realpath(r)
        if os.path.isdir(r) and r not in out:
            out.append(r)
    return out


def local_folder(cfg, path: str) -> str:
    """A path as this machine sees it. A network path from another computer -
    '\\\\nas\\share\\rest', '//nas/share/rest' or 'smb://nas/share/rest' - is found by its
    share name among the configured data folders (a share is usually a folder on one of
    the data disks). Relative paths are refused: nzb2seed runs here, not on your PC."""
    p = path.strip().strip('"').strip()
    m = re.match(r"^(?:smb:)?(?:\\\\|//)([^\\/]+)[\\/]+([^\\/]+)[\\/]*(.*)$", p, re.I)
    if m:
        share, rest = m.group(2), [x for x in re.split(r"[\\/]+", m.group(3)) if x]
        for root in data_roots(cfg):
            for base in (root, os.path.dirname(root)):
                cand = base if os.path.basename(base).casefold() == share.casefold() else None
                if cand is None and os.path.isdir(base):
                    cand = next((os.path.join(base, n) for n in os.listdir(base)
                                 if n.casefold() == share.casefold() and os.path.isdir(os.path.join(base, n))), None)
                if cand:
                    return os.path.join(cand, *rest)
        raise ValueError(f"{path} is a network path, and no shared folder called {share!r} was found in "
                         "the configured data folders - give the folder as the NAS sees it")
    if not os.path.isabs(p):
        raise ValueError(f"{path} is not a full path on the NAS (nzb2seed runs there, not on your PC)")
    return p


def within_roots(cfg, path: str) -> bool:
    real = os.path.realpath(path)
    return any(real == r or real.startswith(r.rstrip(os.sep) + os.sep) for r in data_roots(cfg))


def build_job(app: "App", body: dict):
    """A Build-tab build from its request: (title, the function the job runs). Used for
    the request itself, and to carry on the build after a restart."""
    torrent = _rel(body["torrent"])
    groups = group_selection([_rel(n) for n in body.get("nzbs", [])])   # empty: the build finds them
    opts = _opts(body.get("options", {}))
    data = uploaded_data(app.cfg, torrent)   # None: download it through Prowlarr
    # a release that already stopped nearly complete - built again from another tracker,
    # or tried again: its Usenet downloads are already made, so it is only placing files
    # and handing the torrent over - not waiting behind a queue of builds that download
    finishing = bool(body.get("other_tracker")) or app.nearly_built(torrent.title)

    def run(cfg):
        if finishing:
            report.info("built from another tracker with the downloads already made: "
                        "not waiting for a build slot")
            return execute_run(cfg, opts, torrent, groups, torrent_data=data)
        take_slot(app.build_slot(), "Waiting for a build slot "
                  f"({max(1, int(getattr(cfg, 'build_parallel', 2) or 2))} build(s) run at once)")
        try:
            return execute_run(cfg, opts, torrent, groups, torrent_data=data)
        finally:
            give_slot()          # not held if it was waiting on you when it ended
    return torrent.title, run


def _opts(d: dict) -> Options:
    return Options(pp=d.get("pp") or None, output_dir=d.get("output_dir") or None,
                   no_cleanup=bool(d.get("no_cleanup")), local_verify=bool(d.get("local_verify")),
                   no_qbit=bool(d.get("no_qbit")), start=bool(d.get("start")),
                   dry_run=bool(d.get("dry_run")),
                   retry_bad=None if d.get("retry_bad") is None else bool(d.get("retry_bad")),
                   fetch_missing=bool(d.get("fetch_missing")), whole_posts=bool(d.get("whole_posts")))


def make_handler(app: App, login_override: tuple[str, str] | None, allowed_hosts: set[str]):
    """``login_override`` (from --username/--password) beats the login in the config."""

    def login() -> tuple[str, str]:
        if login_override:
            return login_override
        return app.cfg.gui_username or "", app.cfg.gui_password or ""

    web = resources.files("nzb2seed").joinpath("web")
    page = web.joinpath("index.html").read_bytes()
    # the page's own files: only these kinds, only from these folders, never a path that
    # climbs out of them
    kinds = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
             ".svg": "image/svg+xml"}

    def static(path: str) -> tuple[bytes, str] | None:
        parts = path.strip("/").split("/")
        if parts[0] not in ("css", "js", "vendor") and path != "/favicon.svg":
            return None
        if any(p in ("", ".", "..") or "\\" in p or ":" in p for p in parts):
            return None
        ext = os.path.splitext(parts[-1])[1].lower()
        if ext not in kinds:
            return None
        f = web.joinpath(*parts)
        return (f.read_bytes(), kinds[ext]) if f.is_file() else None

    class H(BaseHTTPRequestHandler):
        server_version = "nzb2seed"

        def log_message(self, fmt, *args):
            pass

        # -- plumbing
        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def events(self):
            """The live feed: an event each time something the page shows changed, named
            by topic. A comment every 20 seconds keeps the connection open through proxies."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            seen = app.events.version
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while True:
                    version, topics = app.events.wait(seen, 20)
                    if version == seen:
                        self.wfile.write(b": still here\n\n")
                    else:
                        time.sleep(0.3)                  # a burst of changes goes as one
                        version, topics = app.events.wait(seen, 0)
                        seen = version
                        data = json.dumps({"topics": topics})
                        self.wfile.write(f"event: change\ndata: {data}\n\n".encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                return                                   # the page went away

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _err(self, msg, code=400):
            self._json({"error": msg}, code)

        def _guard(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            if allowed_hosts and host not in allowed_hosts and not _is_ip(host):
                # also stops DNS-rebinding pages from reaching the API through a browser
                self._err(f"unexpected Host header {host!r}; start the GUI with --allowed-host {host} "
                          "if that is how you reach this machine", 403)
                return False
            user, password = login()
            if not password and not _is_local_client(self.client_address[0]):
                self._err("the login is switched off, so only this machine and private-network "
                          "addresses may use the GUI", 403)
                return False
            if password:
                auth = self.headers.get("Authorization", "")
                ok = False
                if auth.startswith("Basic "):
                    try:
                        u, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
                        # compare both, always, so timing does not reveal which one was wrong
                        ok = hmac.compare_digest(u.encode(), user.encode()) & \
                            hmac.compare_digest(pw.encode(), password.encode())
                    except ValueError:
                        pass
                if not ok:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="nzb2seed"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return False
            return True

        def _body(self) -> dict | None:
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                self._err("expected application/json", 415)  # blocks cross-site form posts
                return None
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                self._err("request too large", 413)
                return None
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                self._err("invalid JSON")
                return None

        # -- routes
        def do_GET(self):
            if not self._guard():
                return
            path = urlsplit(self.path).path
            if path in ("/", "/index.html"):
                return self._send(200, page, "text/html; charset=utf-8")
            if path == "/api/events":
                return self.events()
            found = static(path)
            if found:
                return self._send(200, *found)
            if path == "/api/settings":
                return self._json(self.settings_payload())
            if path == "/api/trackers":
                # the torrent indexers Prowlarr has - to tick the ones nearly complete builds
                # are always added for. Prowlarr's own list: nothing reaches a tracker
                try:
                    names = [n for n, _ in Prowlarr(app.cfg.prowlarr_url, app.cfg.prowlarr_key).indexer_sites() if n]
                except (ApiError, OSError) as e:
                    return self._json({"trackers": [], "error": str(e)})
                return self._json({"trackers": sorted(set(names), key=str.lower)})
            if path == "/api/demand":
                try:
                    rows = app._qbit().torrents() if app.cfg.qbit_url else []
                except (ApiError, OSError) as e:
                    return self._json({"error": str(e), "torrents": 0, "by": [], "stats": {}})
                out = worth_mod.overview(rows, app.cfg.demand_age_days, app.cfg.demand_min_sample)
                out.pop("stats", None)
                # alongside: the same measures for nzb2seed's own builds - manual and automatic
                # apart (with the cross-seeds of their files) - every group shown, however small
                ours = {}
                for kind, got in app.nzb2seed_torrent_rows(rows).items():
                    mine = got["rows"]
                    if not mine:
                        continue
                    a = worth_mod.overview(mine, app.cfg.demand_age_days, 1)
                    ours[kind] = {k: a[k] for k in ("torrents", "stored_gb", "uploaded_gb", "overall_ratio",
                                                    "median_ratio", "dead", "dead_gb", "by")}
                    ours[kind]["built"] = len(mine)                        # however new
                    ours[kind]["cross_seeds"] = got["cross_seeds"]
                flat = [r for g in out["by"] for r in
                        ({**x, "what": g["what"], "value": x["where"]} for x in g["rows"])]
                return self._json({**out,
                                   "chase": rules_mod.propose(flat, app.cfg.demand_min_sample),
                                   "rules": list(app.cfg.demand_rules or []),
                                   "block": list(app.cfg.demand_block or []),
                                   "only_rules": bool(app.cfg.demand_only_rules),
                                   "usenet": odds_mod.odds_for(app.cfg).rows(),
                                   "never_after": odds_mod.MIN_TRIES,
                                   "avoid": out.pop("rules", []),
                                   "min_sample": app.cfg.demand_min_sample,
                                   "age_days": app.cfg.demand_age_days,
                                   "nzb2seed": ours})
            if path == "/api/space":
                try:
                    out = space_mod.check(app.cfg, app._sab())
                except (ApiError, OSError) as e:
                    return self._json({"limits_set": space_mod.limits_set(app.cfg),
                                       "disks": [], "low": False, "why": "", "warning": str(e)})
                return self._json({**out, "holding": app.guard().holding()})
            if path == "/api/retention":
                try:
                    return self._json(retention_mod.state(app.cfg, app._qbit()))
                except (ApiError, OSError) as e:
                    return self._json({**retention_mod.state(app.cfg, None), "warning": str(e)})
            if path == "/api/season/reports":
                return self._json(season_mod.reports(app.cfg, SABnzbd(app.cfg.sab_url, app.cfg.sab_key)))
            if path in ("/api/season/report", "/api/season/file"):
                return self.season_file(path)
            if path == "/api/jobs":
                with app.lock:
                    jobs = [app.summary(j) for j in sorted(app.jobs.values(), key=lambda j: -j.id)]
                return self._json(jobs)
            if path.startswith("/api/jobs/"):
                job = app.jobs.get(int(path.rsplit("/", 1)[1]) if path.rsplit("/", 1)[1].isdigit() else -1)
                if not job:
                    return self._err("no such job", 404)
                q = urlsplit(self.path).query
                since = int(q.split("since=")[1].split("&")[0]) if "since=" in q else 0
                with job.lock:
                    lines = job.lines[since:]
                return self._json({**app.summary(job), "lines": lines, "next": since + len(lines),
                                   "pieces": job.pieces})
            self._err("not found", 404)

        def do_POST(self):
            if not self._guard():
                return
            body = self._body()
            if body is None:
                return
            path = urlsplit(self.path).path
            if path == "/api/jobs/other_trackers":
                return self.other_trackers(body)
            if path == "/api/jobs/add_to_client":
                return self.add_to_client(body)
            if path == "/api/jobs/whole_posts":
                return self.whole_posts(body)
            if path == "/api/jobs/remove":
                # take finished jobs off the list; one still running (or waiting for an
                # answer) is never removed. Only the list entry goes - unless its Usenet
                # downloads are to be deleted too, which only happens where nothing in
                # qBittorrent depends on them. An automatic torrent keeps its Automatic-tab entry
                ids = {int(i) for i in body.get("ids") or [] if str(i).lstrip("-").isdigit()}
                gone = [i for i in ids if i in app.jobs
                        and app.jobs[i].status in ("done", "failed", "cancelled", "interrupted")]
                out = {}
                if body.get("delete_downloads"):
                    hashes = {h for h in (app.torrent_of(app.jobs[i]) for i in gone) if h}
                    busy = {app.torrent_of(j) for j in app.jobs.values() if j.status in ("running", "waiting")}
                    try:
                        out = pipeline_mod.delete_downloads_of(app.cfg, app._sab(), app._qbit(), hashes - busy)
                    except (ApiError, Abort, OSError) as e:
                        return self._err(f"nothing was removed: {e}")
                with app.lock:
                    for i in gone:
                        app.jobs.pop(i, None)
                app.store.changed(urgent=True)
                return self._json({"removed": len(gone), "kept": len(ids) - len(gone), **out})
            if path == "/api/jobs/abandon":
                return self.abandon(body)
            if path == "/api/jobs/retry":
                # replay the request that made the job: the same call, made again
                job = app.jobs.get(int(body.get("id", -1)) if str(body.get("id", "")).lstrip("-").isdigit() else -1)
                if not job:
                    return self._err("no such job", 404)
                if job.kind == "auto":
                    # the Automatic tab's own Try again: back into its queue, as a new job
                    box = getattr(app, "inbox", None)
                    h = box.retryable_job(job.id) if box else None
                    if job.retried_as or not h:
                        return self._err("it was already tried again, or is still under way", 409)
                    box.retry(h)
                    job.retried_as = box.running.get(h) or box.state.items[h].get("job")
                    app.store.changed(urgent=True)
                    return self._json({"id": job.retried_as})
                if not job.repeat:
                    return self._err("this job was not started from a request that can be repeated")
                if job.retried_as:
                    # a second press, or a tap that landed before the list caught up
                    return self._err(f"it was already tried again, as job {job.retried_as}", 409)
                path, body = job.repeat.get("path") or "", dict(job.repeat.get("body") or {})
                app.request.retrying = job
                job.retried_as = -1          # taken at once, so a second press is refused
            app.request.asked = {"path": path, "body": body}
            if urlsplit(self.path).path != "/api/jobs/retry":
                app.request.retrying = None
            try:
                if path == "/api/settings":
                    return self.save_settings(body)
                if path == "/api/demand/forget":
                    return self._json({"removed": app.tracker_cache().clear()})
                if path == "/api/retention/sweep":
                    # the Settings button: preview by default, remove only when asked to.
                    # Both work while retention is still off - seeing what would go is how
                    # you decide whether to switch it on, and the age can be tried without
                    # saving it.
                    dry = bool(body.get("dry_run", True))
                    # 0 is a real age ("everything"), so only a missing value falls back
                    days = None if body.get("days") is None else float(body["days"])
                    qb = app._qbit()
                    out = retention_mod.sweep(app.cfg, qb, log=app._retention_log,
                                              dry_run=dry, force=True, days=days)
                    return self._json({**retention_mod.state(app.cfg, qb, days), **out})
                if path == "/api/test":
                    return self._json(self.test_connections())
                if path == "/api/search":
                    q = (body.get("query") or "").strip()
                    if not q:
                        return self._err("type a release name or search text")
                    torrents, nzbs = search(app.cfg, q)
                    if body.get("protocol") == "usenet":
                        torrents = []
                    return self._json({"torrents": [dataclasses.asdict(r) for r in torrents],
                                       "usenet": [dataclasses.asdict(r) for r in nzbs]})
                if path == "/api/pair":
                    return self.pair(body)
                if path == "/api/season/shows":
                    return self._json(metadata.tvmaze_search((body.get("query") or "").strip()))
                if path == "/api/season/seasons":
                    show = metadata.tvmaze_show(int(body["show_id"]))
                    return self._json(episodes_mod.seasons_summary(show, self._source(body)))
                if path == "/api/metadata/clear_cache":
                    return self._json({"removed": episodes_mod.clear_cache()})
                if path == "/api/season/options":
                    return self.season_options(body)
                if path == "/api/season/grab":
                    return self.season_grab(body)
                if path == "/api/series/grab":
                    return self.series_grab(body)
                if path.startswith("/api/auto/"):
                    return self._json(self.auto(path.rsplit("/", 1)[1], body))
                if path == "/api/series/options":
                    return self.series_options(body)
                if path == "/api/folders":
                    return self.list_folders(body)
                if path == "/api/previous":
                    sab = SABnzbd(app.cfg.sab_url, app.cfg.sab_key)
                    return self._json({"previous": previous_downloads(app.cfg, sab, body.get("title") or "")})
                if path == "/api/build":
                    return self.build(body)
                if path == "/api/upload_torrent":
                    try:
                        data = base64.b64decode(body.get("torrent_b64") or "", validate=True)
                    except ValueError:
                        return self._err("that upload was not readable")
                    if not data:
                        return self._err("choose a .torrent file")
                    try:
                        rel = upload_torrent(app.cfg, body.get("torrent_name") or "", data)
                    except TorrentError as e:
                        return self._err(f"not a usable .torrent file: {e}")
                    except Exception as e:          # bencode errors on random files
                        return self._err(f"not a .torrent file ({type(e).__name__})")
                    return self._json({"torrent": dataclasses.asdict(rel)})
                if path == "/api/assemble":
                    return self.assemble(body)
                if path.startswith("/api/jobs/") and path.endswith("/answer"):
                    job = app.jobs.get(int(path.split("/")[3]))
                    if not job:
                        return self._err("no such job", 404)
                    choice = body.get("choice")
                    job.answer(None if choice is None else int(choice))
                    return self._json({"ok": True})
                if path.startswith("/api/jobs/") and path.endswith("/cancel"):
                    job = app.jobs.get(int(path.split("/")[3]))
                    if not job:
                        return self._err("no such job", 404)
                    job.cancel.set()
                    return self._json({"ok": True})
            except (ApiError, Abort, ValueError) as e:
                return self._err(str(e), 502 if isinstance(e, ApiError) else 400)
            except Exception as e:
                return self._err(f"{type(e).__name__}: {e}", 500)
            finally:
                # a retry that did not start a job - refused, or nothing to replay into -
                # leaves the job free to be tried again rather than locking it
                again = getattr(app.request, "retrying", None)
                if again is not None and again.retried_as == -1:
                    again.retried_as = None
                app.request.retrying = None
            self._err("not found", 404)

        # -- handlers
        def settings_payload(self):
            cfg = app.cfg
            user, password = login()
            return {"path": str(cfg.path), "exists": os.path.isfile(cfg.path),
                    "settings": config_mod.to_sections(cfg),
                    "default_login": (not login_override and password == config_mod.DEFAULT_GUI_PASSWORD),
                    "login_from_command_line": bool(login_override)}

        def save_settings(self, body):
            sections = body.get("settings") or {}
            try:
                cfg = config_mod.from_sections(sections, app.cfg.path)
            except (ValueError, TypeError) as e:
                return self._err(str(e))
            config_mod.save(cfg)
            app.cfg = cfg
            metadata.configure(cfg.flaresolverr_url, cfg.outbound_proxy)
            return self._json(self.settings_payload())

        def _source(self, body) -> str:
            src = (body.get("source") or app.cfg.episode_source or "all").lower()
            if src not in episodes_mod.SOURCES:
                raise ValueError(f"unknown episode source {src!r}")
            return src

        def _with_names(self, body, cfg=None):
            """The config with episode-name matching as the Seasons tab's checkbox says."""
            return dataclasses.replace(cfg or app.cfg, match_episode_names=bool(body.get("match_names")))

        def season_options(self, body):
            show = metadata.tvmaze_show(int(body["show_id"]))
            sn = int(body["season"])
            eps, used = episodes_mod.season_episodes(show, sn, self._source(body))
            wanted = [e["number"] for e in eps]
            pr = Prowlarr(app.cfg.prowlarr_url, app.cfg.prowlarr_key)
            cfg = self._with_names(body)
            opts = season_mod.find_options(cfg, pr, show["name"], sn, wanted, season_mod.names_for(cfg, show))
            return self._json({"show": show, "episodes": eps, "links": metadata.links(show),
                               "source": episodes_mod.SOURCES.get(used or "", ""),
                               "options": [o.summary(wanted) for o in opts]})

        def _library(self, body) -> tuple[bool, str | None]:
            """(skip owned episodes?, the show's folder) - the folder must lie inside one of the
            configured data folders."""
            if not body.get("skip_owned"):
                return False, None
            lib = (body.get("library") or "").strip() or None
            lib = local_folder(app.cfg, lib) if lib else None
            if lib and not within_roots(app.cfg, lib):
                raise ValueError("choose a folder inside one of the configured data folders")
            return True, lib

        def season_grab(self, body):
            sid, sn = int(body["show_id"]), int(body["season"])
            key = [str(k) for k in body.get("keys") or []] or body["key"]     # a priority list, or one
            packing = body.get("packing") or None
            label = body.get("label") or f"season {sn}"
            skip_owned, lib = self._library(body)
            source = self._source(body)

            def run(cfg):
                cfg = self._with_names(body, cfg)
                skip = None
                if skip_owned:
                    report.step("Looking for episodes you already have")
                    skip = series_mod.owned(cfg, metadata.tvmaze_show(sid), lib).get(sn, set())
                return season_mod.grab_season(cfg, sid, sn, key, packing, skip=skip, source=source,
                                              whole_posts=bool(body.get("whole_posts")))
            job = app.start_job(label, "season", run)
            return self._json({"id": job.id})

        def series_options(self, body):
            """Every release group/resolution over all seasons, for the priority list."""
            show = metadata.tvmaze_show(int(body["show_id"]))
            skip_owned, lib = self._library(body)
            source = self._source(body)
            lists, used = episodes_mod.episode_lists(show, source)
            cfg = self._with_names(body)
            have = series_mod.owned(cfg, show, lib) if skip_owned else {}
            pr = Prowlarr(app.cfg.prowlarr_url, app.cfg.prowlarr_key)      # searching only
            plans = series_mod.options_for(cfg, pr, show, lists, have)
            out = series_mod.summary(plans)
            for srow in out["seasons"]:
                srow["source"] = episodes_mod.SOURCES.get(used.get(srow["season"], ""), "")
            return self._json(out)

        def series_grab(self, body):
            sid = int(body["show_id"])
            packing = body.get("packing") or None
            plan_only = bool(body.get("plan_only"))
            skip_owned, lib = self._library(body)
            label = (body.get("label") or f"show {sid}") + (" - plan" if plan_only else " - all seasons")
            res = (body.get("res") or "").strip().lower() or None
            source = self._source(body)
            priority = [str(k) for k in body.get("priority") or []]
            job = app.start_job(label, "season", lambda cfg: series_mod.grab_series(
                self._with_names(body, cfg), sid, packing, library=lib, skip_owned=skip_owned, plan_only=plan_only, prefer_res=res,
                source=source, priority=priority, whole_posts=bool(body.get("whole_posts"))))
            return self._json({"id": job.id})

        # ------------------------------------------------ the Automatic tab
        AUTO_FIELDS = {"enabled": ("auto_enabled", bool), "folder": ("auto_folder", str),
                       "autobrr_folder": ("auto_autobrr_folder", str), "wait_hours": ("auto_wait_hours", float),
                       "retry_minutes": ("auto_retry_minutes", float), "start": ("auto_start", bool),
                       "retry_first_minutes": ("auto_retry_first_minutes", float),
                       "parallel": ("auto_parallel", int), "searches_per_hour": ("auto_searches_per_hour", int),
                       "queue_max": ("auto_queue_max", int), "queue_keep_older": ("auto_queue_keep_older", bool),
                       "skip_unposted": ("auto_skip_unposted", bool),
                       "autobrr_url": ("autobrr_url", str), "autobrr_key": ("autobrr_key", str)}

        # ------------------------------------------------ a build that nearly made it
        def _nearly(self, body):
            try:
                job = app.jobs.get(int(body.get("id")))
            except (TypeError, ValueError):
                job = None
            if not job or not job.extra:
                self._err("that build did not stop part-way, so there is nothing to finish", 404)
                return None
            return job

        def whole_posts(self, body):
            """A build that stopped short where only small files were missing - and settled
            rather than download another whole post, since a whole post is usually hundreds of
            MB for a file of a few dozen: build it again, trying whole posts for those parts
            this time - from the Build tab's request, or back through the Automatic tab. Everything already downloaded is reused; what is still missing after
            that is offered again, alongside completing it from the tracker."""
            job = self._nearly(body)
            if job is None:
                return
            x = job.extra
            if not x.get("settled"):
                return self._err("no part of it was left without trying every whole post")
            if x.get("added") or x.get("abandoned"):
                return self._err("it was already added to qBittorrent or abandoned")
            if job.retried_as:
                return self._err(f"it was already tried again, as job {job.retried_as}", 409)
            if job.kind == "auto":
                # an automatic build goes back into the Automatic tab's queue, as Try again does
                box = getattr(app, "inbox", None)
                h = box.retryable_job(job.id) if box else None
                if not h:
                    return self._err("it is not waiting on the Automatic tab to be tried again", 409)
                box.retry(h, whole_posts=True)
                job.retried_as = box.running.get(h) or box.state.items[h].get("job")
                app.store.changed(urgent=True)
                return self._json({"id": job.retried_as})
            rq = job.repeat or {}
            again = json.loads(json.dumps(rq.get("body") or {}))
            again.setdefault("options", {})["whole_posts"] = True
            if rq.get("path") in ("/api/season/grab", "/api/series/grab"):
                # a season grab: again, with whole posts looked into for the missing files -
                # episodes already in the season folder stay
                again = json.loads(json.dumps(rq.get("body") or {}))
                again["whole_posts"] = True
                app.request.asked = {"path": rq["path"], "body": again}
                app.request.retrying = job
                return (self.season_grab if rq["path"] == "/api/season/grab" else self.series_grab)(again)
            if rq.get("path") == "/api/assemble":
                # built from folders, with what they lack fetched from Usenet: the same again
                app.request.asked = {"path": "/api/assemble", "body": again}
                app.request.retrying = job
                return self.assemble(again)
            if rq.get("path") != "/api/build":
                return self._err("this job was not started by a request that can be repeated")
            title, run = build_job(app, again)
            busy = app.active_build(title)
            if busy:
                return self._err(f"{title} is already being built (job {busy.id})", 409)
            app.request.asked = {"path": "/api/build", "body": again}
            app.request.retrying = job                  # this job is the one tried again
            nj = app.start_job(title, "build", run)
            return self._json({"id": nj.id})

        def other_trackers(self, body):
            """The same release on your other trackers, for a build that stopped nearly
            complete on one you have not pre-approved: building it from one that is lets the
            missing bit come over BitTorrent without asking - and the Usenet downloads already
            made are reused. Prowlarr is asked; no tracker is contacted by nzb2seed."""
            job = self._nearly(body)
            if job is None:
                return
            x = job.extra
            name = job.title[len("auto: "):] if job.title.startswith("auto: ") else job.title
            size = None
            try:
                with open(x.get("torrent_path") or "", "rb") as fh:
                    size = torrent_mod.parse(fh.read()).total_size
            except (OSError, ValueError, torrent_mod.TorrentError):
                pass
            try:
                torrents, _ = search(app.cfg, name)
            except (ApiError, OSError) as e:
                return self._err(f"Prowlarr could not be searched: {e}", 502)
            own, want = app.tracker_key(x.get("tracker", "")), matching.norm(name)
            out = [{**dataclasses.asdict(r), "approved": app.always_adds(r.indexer),
                    "same_size": size is not None and r.size == size}
                   for r in torrents
                   if matching.norm(r.title) == want and app.tracker_key(r.indexer) != own]
            out.sort(key=lambda r: (not r["approved"], not r["same_size"], -(r["seeders"] or 0)))
            return self._json({"name": name, "size": size, "releases": out})

        def add_to_client(self, body):
            """Hand a nearly-built torrent to qBittorrent to download the rest. Only ever on
            the person's say-so: this is the one place nzb2seed starts a torrent that is
            not 100% complete, because they have chosen to download the missing part."""
            job = self._nearly(body)
            if job is None:
                return
            x = dict(job.extra)
            if x.get("abandoned"):
                return self._err("that build was abandoned and its files deleted")
            if x.get("added"):
                return self._err("it is already in qBittorrent")
            override = bool(body.get("override"))
            if not override and x.get("seeders") == 0:
                return self._err("Prowlarr reported no seeders for it, so the missing part could "
                                 "never come over BitTorrent")
            if not override and not app.nearly_enough(x):
                return self._err(f"it is missing more than your limit of {app.nearly_limit_text()} - "
                                 "use Override to add it anyway")
            if override and body.get("always"):
                return self._err("an override is for this build only")
            if body.get("always") and x.get("tracker"):
                # from now on, every build from this tracker that stops this close is
                # handed over without asking
                cfg = app.cfg
                if not app.always_adds(x["tracker"]):
                    trackers = list(cfg.nearly_auto_trackers or []) + [x["tracker"]]
                    app.cfg = dataclasses.replace(cfg, nearly_auto_trackers=trackers)
                    config_mod.save(app.cfg)
            nj = app.finish_in_client(job, why="added by override, outside the limit" if override else "",
                                      override=override)
            also = []
            if body.get("always") and x.get("tracker"):
                # "always" covers the ones already waiting from that tracker too, not only
                # builds that stop short from now on
                for other in list(app.jobs.values()):
                    ox = other.extra or {}
                    if (other is not job and other.status in ("failed", "cancelled", "interrupted")
                            and app.tracker_key(ox.get("tracker", "")) == app.tracker_key(x["tracker"])
                            and not ox.get("added") and not ox.get("abandoned")
                            and app.nearly_enough(ox)):
                        why = f"{x['tracker']} is set to always add builds this close"
                        also.append(app.finish_in_client(other, why).id)
            return self._json({"id": nj.id, "also": also})

        def abandon(self, body):
            """Clear what a failed build left in nzb2seed and SABnzbd: its Usenet downloads, and
            the files it placed - unless its torrent is in qBittorrent. qBittorrent is never
            touched: a torrent there stays, with the files it uses; remove it there yourself."""
            job = self._nearly(body)
            if job is None:
                return
            x = job.extra
            h = x["infohash"]
            done, kept = [], []
            later = app.jobs.get(job.retried_as or -1)
            while later is not None and later.retried_as:
                later = app.jobs.get(later.retried_as)
            if later is not None and later.status == "done":
                return self._err(f"it was tried again and job {later.id} built it - its files "
                                 "are that build's now")
            qb = app._qbit()
            there = qb.info(h) if qb else None
            if there or x.get("added"):
                kept.append("the torrent in qBittorrent and the files it uses (remove it there if you "
                            "no longer want it)")
            else:
                owned = pipeline_mod.owned_record(app.cfg, type("T", (), {"infohash": h})())
                gone = 0
                for f in sorted(owned.files):
                    try:
                        if os.path.isfile(f):
                            os.remove(f)
                            gone += 1
                    except OSError:
                        kept.append(f)
                for d in sorted(owned.dirs, key=len, reverse=True):
                    try:
                        if os.path.isdir(d) and not os.listdir(d):
                            os.rmdir(d)
                    except OSError:
                        pass
                try:
                    os.remove(owned.path)
                except OSError:
                    pass
                done.append(f"deleted the {gone} file(s) it placed")
            # its Usenet downloads - unless another build is running, which may be using one
            busy = [j for j in app.jobs.values() if j.status in ("running", "waiting") and j is not job]
            if busy:
                kept.append("its Usenet downloads (other builds are running and may be using them)")
            else:
                sab = app._sab()
                n = 0
                for j in pipeline_mod.ledger_for(app.cfg).all():
                    if j.get("torrent") == h and sab is not None:
                        try:
                            if pipeline_mod.remove_download(app.cfg, sab, j["nzo"]):
                                n += 1
                        except (ApiError, OSError) as e:
                            kept.append(f"{j.get('title') or j['nzo']} ({e})")
                done.append(f"deleted {n} Usenet download(s)")
            job.extra = {**x, "abandoned": True}
            job.result = "abandoned: " + "; ".join(done) + (f" (kept {', '.join(kept)})" if kept else "")
            app.store.changed(urgent=True)
            return self._json({"result": job.result})

        def _auto_state(self):
            cfg = app.cfg
            box = getattr(app, "inbox", None)
            return {"settings": {k: getattr(cfg, f) for k, (f, _) in self.AUTO_FIELDS.items() if k != "autobrr_key"},
                    "has_key": bool(cfg.autobrr_key), "items": box.items() if box else []}

        def _autobrr(self, body=None):
            url = ((body or {}).get("autobrr_url") or app.cfg.autobrr_url or "").strip()
            key = ((body or {}).get("autobrr_key") or app.cfg.autobrr_key or "").strip()
            if not url or not key:
                raise ValueError("enter autobrr's URL and API key (autobrr: Settings, API keys)")
            return Autobrr(url, key)

        def auto(self, what, body):
            cfg = app.cfg
            if what == "state":
                return self._auto_state()
            if what == "exclude_group":
                # the Demand tab's suggestion: stop a group that never reaches Usenet at the
                # source, in every autobrr filter that feeds nzb2seed - asked for by pressing it
                group = (body.get("group") or "").strip()
                if not group or not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", group):
                    raise ValueError("no release group given")
                ab = self._autobrr()
                changed = 0
                for f in ab.filters():
                    full = ab.filter(f["id"])
                    if ab.ours(full, cfg.auto_autobrr_folder) and ab.exclude_group(f["id"], group):
                        changed += 1
                return {"group": group, "filters": changed}
            if what == "save":
                changes = {}
                for k, (field, typ) in self.AUTO_FIELDS.items():
                    if k in body and not (k == "autobrr_key" and not body[k]):
                        changes[field] = typ(body[k]) if typ is not bool else bool(body[k])
                if changes.get("auto_folder"):
                    changes["auto_folder"] = local_folder(cfg, changes["auto_folder"])
                if changes.get("auto_enabled") and not (changes.get("auto_folder") or cfg.auto_folder):
                    raise ValueError("set the inbox folder first")
                new = dataclasses.replace(cfg, **changes)
                if new.auto_folder:
                    os.makedirs(new.auto_folder, exist_ok=True)
                config_mod.save(new)
                app.cfg = new
                return self._auto_state()
            if what == "detect":
                found = inbox_mod.detect_autobrr_folder()
                if not found:
                    raise ValueError("no autobrr container found on this machine - enter both paths yourself")
                return {"folder": found[0], "autobrr_folder": found[1]}
            if what == "filters":
                ab = self._autobrr(body)
                folder = body.get("autobrr_folder") or cfg.auto_autobrr_folder
                out = []
                for f in ab.filters():
                    full = ab.filter(f["id"])
                    ours = ab.ours(full, folder)
                    out.append({"id": f["id"], "name": f.get("name", ""), "enabled": f.get("enabled", True),
                                "indexers": [i.get("name") or i.get("identifier") for i in full.get("indexers") or []],
                                # actions that neither nzb2seed nor a download client: shown as "other"
                                "actions": [a.get("name") or a.get("type") for a in full.get("actions") or []
                                            if a not in ours and a not in ab.grabbers(full)],
                                "ours": bool(ours), "folder_ok": all(a.get("watch_folder") == folder for a in ours),
                                "grabbers": [{"id": a["id"], "name": a.get("name") or a.get("type"),
                                              "enabled": bool(a.get("enabled"))} for a in ab.grabbers(full)]})
                return {"version": ab.version(), "filters": out}
            if what == "apply":
                folder = cfg.auto_autobrr_folder
                if not folder:
                    raise ValueError("set the folder as autobrr sees it first")
                ab = self._autobrr(body)
                want = {int(x) for x in body.get("filters") or []}
                replace = {int(k): bool(v) for k, v in (body.get("replace") or {}).items()}
                memo = app.autobrr_memo()
                switched, were = memo["switched_off"], memo["filters_were"]
                added = removed = fixed = off = back = on = stopped = 0
                for f in ab.filters():
                    fid, was = f["id"], bool(f.get("enabled"))
                    full = ab.filter(f["id"])
                    ours = ab.ours(full, folder)
                    if f["id"] in want and not ours:
                        ab.add_action(f["id"], folder)
                        added += 1
                    elif f["id"] in want:
                        for a in ours:
                            if a.get("watch_folder") != folder:
                                ab.set_folder(a, folder)
                                fixed += 1
                    else:
                        for a in ours:
                            ab.delete_action(a["id"])
                            removed += 1
                    # the filter's own download actions: off while nzb2seed builds it from
                    # Usenet, back on (the ones nzb2seed switched off) when it stops
                    for a in ab.grabbers(full):
                        if fid in want and replace.get(fid, True) and a.get("enabled"):
                            ab.toggle(a["id"])
                            switched.append(a["id"])
                            off += 1
                        elif (fid not in want or not replace.get(fid, True)) and a["id"] in switched \
                                and not a.get("enabled"):
                            ab.toggle(a["id"])
                            switched.remove(a["id"])
                            back += 1
                    # the filter itself: enabled in autobrr while nzb2seed builds its releases,
                    # and disabled there when you untick it - so unticking always stops the
                    # filter, never leaves autobrr acting on it on its own
                    if fid in want:
                        were.setdefault(str(fid), was)
                        if not was:
                            ab.set_enabled(fid, True)
                            on += 1
                    elif str(fid) in were:
                        if was:
                            ab.set_enabled(fid, False)
                            stopped += 1
                        del were[str(fid)]
                app.autobrr_memo({"switched_off": switched, "filters_were": were})
                return {"added": added, "removed": removed, "fixed": fixed, "off": off, "back": back,
                        "on": on, "stopped": stopped}
            if what == "retry":
                app.inbox.retry(str(body.get("infohash")))
                return self._auto_state()
            if what == "forget":
                app.inbox.forget(str(body.get("infohash")))
                return self._auto_state()
            if what == "clear":
                return dict(self._auto_state(), stopped=app.inbox.stop_pending())
            raise ValueError(f"unknown request {what!r}")

        def list_folders(self, body):
            roots = data_roots(app.cfg)
            path = (body.get("path") or "").strip()
            if not path:
                return self._json({"path": "", "parent": None, "roots": roots,
                                   "dirs": [{"name": r, "path": r} for r in roots]})
            if not within_roots(app.cfg, path) or not os.path.isdir(path):
                return self._err("not a folder inside the configured data folders")
            real = os.path.realpath(path)
            try:
                names = sorted((n for n in os.listdir(real)
                                if not n.startswith(".") and os.path.isdir(os.path.join(real, n))), key=str.casefold)
            except OSError as e:
                return self._err(f"cannot list {path}: {e.strerror}")
            parent = os.path.dirname(real)
            return self._json({"path": real, "roots": roots,
                               "parent": parent if within_roots(app.cfg, parent) else "",
                               "dirs": [{"name": n, "path": os.path.join(real, n)} for n in names]})

        def season_file(self, path):
            q = dict(p.split("=", 1) for p in urlsplit(self.path).query.split("&") if "=" in p)
            from urllib.parse import unquote
            name = unquote(q.get("name", ""))
            if not name or "/" in name or "\\" in name or name.startswith("."):
                return self._err("bad name")
            root = season_mod.downloads_root(app.cfg, SABnzbd(app.cfg.sab_url, app.cfg.sab_key))
            side = os.path.realpath(os.path.join(root, name + season_mod.SIDE_SUFFIX))
            if os.path.dirname(side) != os.path.realpath(root) or not os.path.isdir(side):
                return self._err("no such report", 404)
            if path == "/api/season/report":
                with open(os.path.join(side, "report.json"), encoding="utf-8") as fh:
                    r = json.load(fh)
                mi = os.path.join(side, "mediainfo.txt")
                if os.path.exists(mi):
                    with open(mi, encoding="utf-8", errors="replace") as fh:
                        r["mediainfo"] = fh.read()
                return self._json(r)
            rel = unquote(q.get("path", ""))
            target = os.path.realpath(os.path.join(side, rel))
            if not target.startswith(side + os.sep) or not os.path.isfile(target):
                return self._err("no such file", 404)
            ctype = {".png": "image/png", ".jpg": "image/jpeg", ".txt": "text/plain; charset=utf-8",
                     ".html": "text/html; charset=utf-8"}.get(os.path.splitext(target)[1].lower(),
                                                              "application/octet-stream")
            with open(target, "rb") as fh:
                return self._send(200, fh.read(), ctype)

        def test_connections(self):
            cfg = app.cfg
            out = {}
            checks = {
                "prowlarr": lambda: Prowlarr(cfg.prowlarr_url, cfg.prowlarr_key).status(),
                "sabnzbd": lambda: f"SABnzbd {SABnzbd(cfg.sab_url, cfg.sab_key).version()}",
                "qbittorrent": lambda: self._qb_check(cfg),
            }
            for name, fn in checks.items():
                try:
                    out[name] = {"ok": True, "text": fn()}
                except Exception as e:
                    out[name] = {"ok": False, "text": str(e) or type(e).__name__}
            return out

        @staticmethod
        def _qb_check(cfg):
            qb = QBittorrent(cfg.qbit_url, cfg.qbit_user, cfg.qbit_pass)
            qb.login()
            return f"qBittorrent {qb.app_version()} (WebAPI {'.'.join(map(str, qb.api_version()))})"

        def pair(self, body):
            torrent = _rel(body["torrent"])
            nzbs = [_rel(n) for n in body.get("usenet", [])]
            pr = Prowlarr(app.cfg.prowlarr_url, app.cfg.prowlarr_key)
            groups, nzbs, how = pair(app.cfg, pr, torrent, nzbs)
            return self._json({"groups": [[n.guid for n in g] for g in groups],
                               "usenet": [dataclasses.asdict(n) for n in nzbs], "how": how})

        def build(self, body):
            torrent = _rel(body["torrent"])
            busy = app.active_build(torrent.title)
            if busy:
                return self._err(f"{torrent.title} is already being built (job {busy.id})", 409)
            title, run = build_job(app, body)
            job = app.start_job(title, "build", run)
            return self._json({"id": job.id})

        def assemble(self, body):
            sources = [local_folder(app.cfg, s) for s in body.get("sources", []) if s.strip()]
            if not sources and not (body.get("options") or {}).get("fetch_missing"):
                # folders are optional - but then everything comes from Usenet
                return self._err("add a folder holding the NZB download, or tick "
                                 "\"Download missing files from Usenet\" to fetch everything")
            if not sources and not ((body.get("options") or {}).get("output_dir") or app.cfg.output_dir):
                return self._err("with no folders, say where to put the torrent's files "
                                 "(or set the output folder in Settings)")
            for src in sources:
                if not os.path.isdir(src):
                    return self._err(f"{src} does not exist on the NAS")
            tpath = (body.get("torrent_path") or "").strip()
            tpath = local_folder(app.cfg, tpath) if tpath else tpath
            if body.get("torrent_b64"):
                os.makedirs(app.cfg.torrent_dir, exist_ok=True)
                name = os.path.basename(body.get("torrent_name") or "upload.torrent")
                tpath = os.path.join(app.cfg.torrent_dir, f"upload-{int(time.time())}-{name}")
                with open(tpath, "wb") as fh:
                    fh.write(base64.b64decode(body["torrent_b64"]))
            if not tpath:
                return self._err("choose a .torrent file")
            opts = _opts(body.get("options", {}))
            job = app.start_job(os.path.basename(tpath), "assemble",
                                lambda cfg: execute_assemble(cfg, opts, tpath, sources))
            return self._json({"id": job.id})

    return H


def _is_ip(host: str) -> bool:
    """IP-literal Host headers are always fine: DNS rebinding needs a domain name."""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _is_local_client(addr: str) -> bool:
    """Loopback or a private LAN address (like the *arr apps' "local addresses")."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_private or ip.is_link_local


def own_names(bind: str) -> set[str]:
    """Names and addresses this machine answers to: the Host headers the GUI accepts."""
    names = {"127.0.0.1", "localhost", "::1"}
    hn = socket.gethostname()
    names |= {hn.lower(), f"{hn.lower()}.local", socket.getfqdn().lower()}
    try:
        names |= {a[4][0] for a in socket.getaddrinfo(hn, None)}
    except OSError:
        pass
    try:   # every IPv4 address of this machine (Linux; harmless elsewhere)
        import subprocess
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=3).stdout
        names |= {a for a in out.split() if a.count(".") == 3}
    except (OSError, subprocess.SubprocessError):
        pass
    try:   # the address used for outgoing LAN traffic (no packet is sent)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect(("192.0.2.1", 9))
            names.add(sk.getsockname()[0])
    except OSError:
        pass
    if bind not in ("0.0.0.0", "::", ""):
        names.add(bind.lower())
    return names


def serve(config_path: str | None, host: str, port: int, password: str | None,
          open_browser: bool, extra_hosts=(), username: str | None = None) -> int:
    allowed = own_names(host) | {h.lower() for h in extra_hosts}
    app = App(config_path)
    app.start_inbox()
    app.resume_builds()
    app.start_retention()
    app.start_space_guard()
    app.start_sab_category()
    app.start_cleaner()
    override = (username or app.cfg.gui_username or config_mod.DEFAULT_GUI_USER, password) \
        if password is not None else None
    httpd = ThreadingHTTPServer((host, port), make_handler(app, override, allowed))
    ips = [n for n in allowed if _is_ip(n) and n.count(".") == 3 and not n.startswith("127.")]
    lan = sorted(ips, key=lambda n: (not n.startswith("192.168."), n))[0] if ips else None
    url = f"http://{lan if host in ('0.0.0.0', '::') and lan else ('127.0.0.1' if host in ('0.0.0.0', '::') else host)}:{port}/"
    print(f"nzb2seed GUI on {url}  (config: {app.cfg.path})  - Ctrl+C to stop", flush=True)
    user, pw = override or (app.cfg.gui_username, app.cfg.gui_password)
    if not pw:
        print("login switched off: open to this machine and private-network (LAN) addresses only",
              flush=True)
    elif not override and pw == config_mod.DEFAULT_GUI_PASSWORD:
        print(f"login: {user} / {pw} (the default - change it in Settings)", flush=True)
    else:
        print(f"login: {user} / (password set)", flush=True)
    if open_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    import signal

    def on_term(*_):
        raise KeyboardInterrupt      # so "kill" also saves the job list on the way out
    signal.signal(signal.SIGTERM, on_term)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        try:
            app.store.flush()
        except OSError:
            pass
    return 0
