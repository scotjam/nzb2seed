"""Stopping downloads before the disk fills up.

Off unless a limit is set. Two disks are watched, and each can have its own limit: the
**downloads** disk SABnzbd stages on, which empties again after every build, and the
**output** disk that keeps what is being seeded, which only ever grows. A disk with no
limit of its own falls back to the general one.

When either disk falls below its limit,
downloading is paused - SABnzbd's queue always, qBittorrent's downloads only if you ask
for that - and it carries on once there is room again, whether the room came from the
retention sweep or from you deleting something by hand.

Builds already running are held too, not left to finish: each one stops before its next
big write (starting a download, or unpacking and placing files) rather than filling the
disk it is already short of. A held build is paused, not failed - it carries on by itself,
and you can still stop it while it waits.

Two things it is careful about:

* it resumes **only what it paused**. A job you paused yourself stays paused, and the
  jobs it paused are remembered across restarts, so a restart cannot orphan them;
* pausing a *torrent* that is still downloading can cost you a private tracker's
  hit-and-run grace, so qBittorrent is left alone unless you switch that on yourself.

Seeding is never touched: a torrent that is finished keeps seeding while the guard holds
everything else, because seeding is what pays for the ratio.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time

from .clients import ApiError, QBittorrent, SABnzbd
from .config import Config
from .pathmap import map_path

GB = 1024 ** 3
GATE = None            # set by the GUI: () -> why the disk is too full, "" while it is fine
MARGIN = 1.1           # resume a little above the limit, not exactly at it
WAIT_POLL = 30.0


def wait_for_room(step=None, progress=None, sleep=None, role: str = "output"):
    """Hold a running build here while the disk it is about to write to is too full.

    ``role`` matters. A build waiting to *download* is waiting for staging room, and
    holding it back saves that disk. A build waiting to *place its files* is about to move
    them off staging and onto the output disk - holding that back saves nothing and frees
    nothing, and with several builds at once it is a deadlock: every build sits on a
    finished download, the staging disk stays full, and the one step that would empty it is
    the step being blocked. So each stage waits on its own disk only.

    It is a pause, not a failure: the build goes on by itself once there is room, and
    stopping the build still works while it waits."""
    from .report import check_cancel, info
    gate, said = GATE, False
    if gate is None:
        return
    while True:
        check_cancel()
        try:
            why = gate(role)
        except Exception:               # a broken gate must never wedge a build
            return
        if not why:
            if said:
                info("there is room again - carrying on")
            return
        if not said:
            said = True
            (step or info)(f"waiting for disk space: {why}")
        if progress:
            progress(f"waiting for disk space: {why}")
        (sleep or time.sleep)(WAIT_POLL)


DOWNLOADING = {"downloading", "metaDL", "stalledDL", "queuedDL", "forcedDL", "checkingDL",
               "allocating", "pausedDL"}
_lock = threading.Lock()


def watched(cfg: Config, sab: SABnzbd | None = None) -> list[tuple[str, str]]:
    """(folder, which disk it is) for every disk that has to have room.

    The two do different jobs and deserve different limits: the **downloads** disk is
    staging - it needs enough room for the release being fetched and unpacked, and empties
    again afterwards - while the **output** disk keeps everything being seeded, and filling
    it stops the whole thing. Only folders that exist here are watched: a path this machine
    cannot see says nothing about any disk."""
    out, seen = [], set()
    for p, role in ((cfg.output_dir, "output"), (_sab_dir(cfg, sab), "downloads")):
        if p and os.path.isdir(p) and p not in seen:
            seen.add(p)
            out.append((p, role))
    return out


def _sab_dir(cfg: Config, sab: SABnzbd | None) -> str:
    if sab is None:
        return ""
    try:
        return map_path(sab.incomplete_dir(), cfg.sab_to_local)
    except (ApiError, OSError):
        return ""


def free_on(path: str) -> tuple[int, int]:
    """(free, total) bytes of the disk holding ``path``."""
    u = shutil.disk_usage(path)
    return u.free, u.total


def limit_for(cfg: Config, role: str) -> tuple[float, float]:
    """(GB, percent) for this disk: its own limit, or the general one where it has none."""
    gb = float(getattr(cfg, f"space_{role}_min_gb", 0) or 0) or float(cfg.space_min_gb or 0)
    pct = float(getattr(cfg, f"space_{role}_min_percent", 0) or 0) or float(cfg.space_min_percent or 0)
    return gb, pct


def limits_set(cfg: Config) -> bool:
    return any(any(limit_for(cfg, role)) for role in ("output", "downloads"))


def shortfall(cfg: Config, free: int, total: int, resuming: bool = False,
              role: str = "output") -> str:
    """Why this disk is too full, or "" when it has room.

    ``resuming`` asks the question the other way round, against a slightly higher bar, so
    downloads do not start and stop again every few minutes around the limit.

    Two numbers are in play and they are not the same: downloading stops at the limit, and
    starts again only at the limit plus the margin. So the sentence states whichever bar it
    is testing against, and the amount to free is always measured against the one that gets
    things moving - telling someone to free 21 GB when 76 GB is what restarts it would be
    worse than saying nothing."""
    margin = MARGIN if resuming else 1.0
    min_gb, min_pct = limit_for(cfg, role)
    why, need = [], 0
    for limit_bytes, shown_free, unit in (
            (min_gb * GB if min_gb > 0 else 0, f"{free / GB:.1f} GB free", "gb"),
            (total * min_pct / 100 if min_pct > 0 and total else 0,
             f"{free * 100 / total:.1f}% free" if total else "", "pct")):
        if not limit_bytes or free >= limit_bytes * margin:
            continue
        limit = (f"{min_gb:g} GB" if unit == "gb" else f"{min_pct:g}%")
        if resuming:
            bar = limit_bytes * MARGIN
            shown = (f"{bar / GB:.1f} GB" if unit == "gb" else f"{bar * 100 / total:.1f}%")
            why.append(f"{shown_free}, and {shown} is needed before downloading starts again "
                       f"(the {limit} limit plus a {MARGIN:g}x margin, so it does not stop "
                       f"and start around the line)")
        else:
            why.append(f"{shown_free}, under the {limit} limit")
        need = max(need, limit_bytes * MARGIN - free)     # what restarts it, not what stopped it
    if need:
        why.append(f"free {need / GB:.1f} GB more to start downloading again"
                   + ("" if resuming else f" (the {MARGIN:g}x margin again: it takes more to "
                                          f"restart than it did to stop)"))
    return "; ".join(why)


def check(cfg: Config, sab: SABnzbd | None = None, resuming: bool = False) -> dict:
    """The state of every watched disk, and whether any of them is too full."""
    disks = []
    for p, role in watched(cfg, sab):
        try:
            free, total = free_on(p)
        except OSError:
            continue
        gb, pct = limit_for(cfg, role)
        disks.append({"path": p, "role": role, "free": free, "total": total,
                      "min_gb": gb, "min_percent": pct,
                      "percent": round(free * 100 / total, 1) if total else 0,
                      "why": shortfall(cfg, free, total, resuming, role) if (gb or pct) else ""})
    low = [d for d in disks if d["why"]]
    return {"limits_set": limits_set(cfg), "disks": disks, "low": bool(low),
            "why": "; ".join(f"{d['role']} disk {d['path']}: {d['why']}" for d in low)}


class Guard:
    """Pauses downloading while the disk is too full, and resumes what it paused."""

    def __init__(self, path: str):
        self.path = path                     # where the "we paused these" note lives

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            return {"sab": False, "torrents": []}
        return {"sab": bool(d.get("sab")), "torrents": list(d.get("torrents") or [])}

    def _save(self, state: dict):
        tmp = f"{self.path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=1)
        os.replace(tmp, self.path)

    def holding(self) -> bool:
        """Is the guard currently holding anything back?"""
        s = self._load()
        return bool(s["sab"] or s["torrents"])

    def run(self, cfg: Config, sab: SABnzbd | None, qb: QBittorrent | None, log=print) -> dict:
        """One pass: pause if the disk is too full, resume once it is not."""
        with _lock:
            if not limits_set(cfg):
                return {"held": False, "why": "", "paused": 0, "resumed": 0}
            state = self._load()
            held = bool(state["sab"] or state["torrents"])
            now = check(cfg, sab, resuming=held)
            if now["low"]:
                return {**self._pause(cfg, sab, qb, state, now["why"], log), "held": True,
                        "why": now["why"]}
            if held:
                return {**self._resume(sab, qb, state, log), "held": False, "why": ""}
            return {"held": False, "why": "", "paused": 0, "resumed": 0}

    def _pause(self, cfg: Config, sab, qb, state: dict, why: str, log) -> dict:
        n = 0
        if sab is not None and not state["sab"]:
            try:
                if not sab.queue_paused():          # already paused by you: leave it be
                    sab.pause_all()
                    state["sab"] = True
                    n += 1
                    log(f"disk is low ({why}) - SABnzbd's queue paused")
            except (ApiError, OSError) as e:
                log(f"could not pause SABnzbd: {e}")
        if cfg.space_pause_torrents and qb is not None:
            mine = set(state["torrents"])
            try:
                for t in qb.torrents():
                    h = str(t.get("hash", ""))
                    if t.get("state") in DOWNLOADING and t.get("state") != "pausedDL" and h not in mine:
                        qb.stop(h)                  # seeding torrents are never touched
                        mine.add(h)
                        n += 1
                        log(f"disk is low - stopped the download {t.get('name', h)[:60]}")
            except (ApiError, OSError) as e:
                log(f"could not pause torrents: {e}")
            state["torrents"] = sorted(mine)
        self._save(state)
        return {"paused": n, "resumed": 0}

    def _resume(self, sab, qb, state: dict, log) -> dict:
        n = 0
        if state["sab"] and sab is not None:
            try:
                sab.resume_all()
                n += 1
                log("there is room again - SABnzbd's queue resumed")
            except (ApiError, OSError) as e:
                log(f"could not resume SABnzbd: {e}")
                return {"paused": 0, "resumed": 0}   # keep the note: try again next time
            state["sab"] = False
        left = []
        for h in state["torrents"]:
            if qb is None:
                left.append(h)
                continue
            try:
                qb.start(h)                          # only the ones this guard stopped
                n += 1
            except (ApiError, OSError) as e:
                log(f"could not resume {h}: {e}")
                left.append(h)
        if state["torrents"] and n:
            log(f"there is room again - {len(state['torrents']) - len(left)} torrent(s) started again")
        state["torrents"] = left
        self._save(state)
        return {"paused": 0, "resumed": n}
