"""Stopping downloads before the disk fills up.

Off unless a limit is set. When the disk nzb2seed downloads to falls below the limit,
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


def wait_for_room(step=None, progress=None, sleep=None):
    """Hold a running build here while the disk is too full.

    A build that carried on would keep writing - unpacking, copying, hard-linking - into
    the very disk there is no room on, so every stage that is about to write waits here
    instead. It is a pause, not a failure: the build goes on by itself once there is room,
    and stopping the build still works while it waits."""
    from .report import check_cancel, info
    gate, said = GATE, False
    if gate is None:
        return
    while True:
        check_cancel()
        try:
            why = gate()
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


def watched(cfg: Config, sab: SABnzbd | None = None) -> list[str]:
    """The folders whose disk has to have room: where builds are put, and where SABnzbd
    unpacks. Only ones that exist here - a path this machine cannot see says nothing."""
    out = []
    for p in (cfg.output_dir, _sab_dir(cfg, sab)):
        if p and os.path.isdir(p) and p not in out:
            out.append(p)
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


def limits_set(cfg: Config) -> bool:
    return cfg.space_min_percent > 0 or cfg.space_min_gb > 0


def shortfall(cfg: Config, free: int, total: int, resuming: bool = False) -> str:
    """Why this disk is too full, or "" when it has room.

    ``resuming`` asks the question the other way round, against a slightly higher bar, so
    downloads do not start and stop again every few minutes around the limit. The answer
    ends with how much has to be freed to get going again, which is the number you act on."""
    margin = MARGIN if resuming else 1.0
    why, need = [], 0
    if cfg.space_min_gb > 0:
        want = cfg.space_min_gb * GB * margin
        if free < want:
            why.append(f"{free / GB:.1f} GB free, less than the {cfg.space_min_gb:g} GB limit")
            need = max(need, want - free)
    if cfg.space_min_percent > 0 and total:
        want = total * cfg.space_min_percent * margin / 100
        if free < want:
            why.append(f"{free * 100 / total:.1f}% free, less than the "
                       f"{cfg.space_min_percent:g}% limit")
            need = max(need, want - free)
    if need:
        why.append(f"free {need / GB:.1f} GB more to carry on"
                   + (f" (the limit plus a {MARGIN:g}x margin)" if resuming else ""))
    return "; ".join(why)


def check(cfg: Config, sab: SABnzbd | None = None, resuming: bool = False) -> dict:
    """The state of every watched disk, and whether any of them is too full."""
    disks = []
    for p in watched(cfg, sab):
        try:
            free, total = free_on(p)
        except OSError:
            continue
        disks.append({"path": p, "free": free, "total": total,
                      "percent": round(free * 100 / total, 1) if total else 0,
                      "why": shortfall(cfg, free, total, resuming) if limits_set(cfg) else ""})
    low = [d for d in disks if d["why"]]
    return {"limits_set": limits_set(cfg), "disks": disks, "low": bool(low),
            "why": "; ".join(f"{d['path']}: {d['why']}" for d in low)}


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
