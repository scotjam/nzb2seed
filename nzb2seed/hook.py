"""qBittorrent's "run external program on torrent finished", while the Library tab is on.

That program usually asks cross-seed to search for the torrent that just finished. A
library run finishes torrents all day (its builds, and cross-eyed's rechecks of moved
ones), and every cross-seed match is another torrent for qBittorrent to recheck from
the disk. So while the Library tab is switched on, qBittorrent calls nzb2seed instead:
nzb2seed runs the original program itself straight away when the library is idle, and
holds the calls back while it is busy building or moving - then runs them, a few at a
time, once it is done. Nothing is lost.

The original setting is saved (hook.json) before it is replaced, and put back when the
Library tab is switched off, at start-up if the tab is off, and by
``nzb2seed restore-qbit-hook`` (run that before removing nzb2seed).
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import subprocess
import threading
import time
from urllib.parse import urlsplit

HOOK_PATH = "/api/hook/finished"
PER_TICK = 3                     # held calls run per library poll, once it is idle
RUN_TIMEOUT = 120


def ours(program: str) -> bool:
    return HOOK_PATH in (program or "")


def fill(program: str, t: dict) -> str:
    """qBittorrent's placeholders, filled the way it would for torrent ``t``."""
    vals = {"N": t.get("name", ""), "L": t.get("category", ""), "G": t.get("tags", ""),
            "F": t.get("content_path", ""), "R": t.get("root_path", "") or t.get("content_path", ""),
            "D": t.get("save_path", ""), "C": str(t.get("num_files", "") or ""),
            "Z": str(t.get("total_size", "") or t.get("size", "") or ""), "T": t.get("tracker", ""),
            "I": t.get("infohash_v1", "") or t.get("hash", ""), "J": t.get("infohash_v2", ""),
            "K": t.get("hash", "")}
    return re.sub(r"%([NLGFRDCZTIJK])", lambda m: str(vals[m.group(1)]), program)


class Hook:
    def __init__(self, app, path: str):
        self.app = app
        self.path = path
        self.lock = threading.Lock()
        self.data = {"saved": None, "token": "", "queue": []}
        try:
            with open(path, encoding="utf-8") as f:
                self.data.update(json.load(f))
        except (OSError, ValueError):
            pass
        if not self.data.get("token"):
            self.data["token"] = secrets.token_urlsafe(18)
            self._save()

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=1)
        os.replace(tmp, self.path)

    # -- qBittorrent's setting
    def url(self) -> str:
        cfg = self.app.cfg
        base = (getattr(cfg, "library_hook_url", "") or "").rstrip("/")
        if not base:
            host = urlsplit(cfg.qbit_url or "").hostname or "127.0.0.1"
            base = f"http://{host}:{getattr(self.app, 'gui_port', 9797)}"
        return f"{base}{HOOK_PATH}?token={self.data['token']}&h=%I"

    def program(self) -> str:
        return f'curl -s -m 15 "{self.url()}"'

    def install(self) -> str:
        """Point qBittorrent's on-finished program at nzb2seed, saving the original first."""
        qb = self.app._qbit()
        if qb is None:
            return "no qBittorrent set up"
        prefs = qb.preferences()
        prog, on = prefs.get("autorun_program") or "", bool(prefs.get("autorun_enabled"))
        with self.lock:
            if ours(prog):
                if prog != self.program():   # moved port/address: refreshed, original kept
                    qb.set_preferences({"autorun_program": self.program(), "autorun_enabled": True})
                return "already in place"
            self.data["saved"] = {"enabled": on, "program": prog, "at": time.time()}
            self._save()
        qb.set_preferences({"autorun_program": self.program(), "autorun_enabled": True})
        return "installed"

    def restore(self) -> str:
        """Put qBittorrent's original on-finished program back; held calls still run."""
        with self.lock:
            saved = self.data.get("saved")
        if not saved:
            return "nothing saved - qBittorrent's setting was not changed by nzb2seed"
        qb = self.app._qbit()
        if qb is None:
            return "no qBittorrent set up"
        prefs = qb.preferences()
        if ours(prefs.get("autorun_program") or ""):
            qb.set_preferences({"autorun_program": saved.get("program") or "",
                                "autorun_enabled": bool(saved.get("enabled"))})
            msg = "restored"
        else:
            msg = "qBittorrent's setting had been changed by hand since - left as it is"
        with self.lock:
            self.data["saved"] = None
            self._save()
        return msg

    def sync(self):
        """Match qBittorrent's setting to the Library tab's switch (at start-up, on toggle)."""
        try:
            cfg = self.app.cfg
            if cfg.library_enabled and getattr(cfg, "library_hold_hook", True):
                r = self.install()
            elif self.data.get("saved"):
                r = self.restore()
            else:
                return
            print(f"qBittorrent on-finished program: {r}", flush=True)
        except Exception as e:                  # noqa: BLE001 - never stop the GUI over it
            print(f"qBittorrent on-finished program: {type(e).__name__}: {e}", flush=True)

    # -- the calls
    def check_token(self, token: str) -> bool:
        return hmac.compare_digest((token or "").encode(), self.data["token"].encode())

    def received(self, h: str, busy: bool) -> str:
        h = (h or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", h):
            return "not an infohash"
        if busy:
            with self.lock:
                if h not in self.data["queue"]:
                    self.data["queue"].append(h)
                    self._save()
            return "held until the library is idle"
        threading.Thread(target=self.forward, args=(h,), name="hook", daemon=True).start()
        return "passed on"

    def original(self) -> str:
        saved = self.data.get("saved")
        if saved is None:                       # switched off since: the setting is back
            try:
                prefs = self.app._qbit().preferences()
                if prefs.get("autorun_enabled") and not ours(prefs.get("autorun_program") or ""):
                    return prefs.get("autorun_program") or ""
            except Exception:                   # noqa: BLE001
                return ""
            return ""
        return (saved.get("program") or "") if saved.get("enabled") else ""

    def forward(self, h: str) -> bool:
        prog = self.original()
        if not prog:
            return True                         # the original did nothing: nothing to run
        try:
            t = self.app._qbit().info(h) or {"hash": h}
        except Exception:                       # noqa: BLE001
            t = {"hash": h}
        try:
            r = subprocess.run(fill(prog, t), shell=True, timeout=RUN_TIMEOUT,
                               capture_output=True, text=True)
            if r.returncode:
                print(f"hook: original program failed for {h[:8]} (exit {r.returncode}): "
                      f"{(r.stderr or r.stdout).strip()[:200]}", flush=True)
            return True
        except (OSError, subprocess.SubprocessError) as e:
            print(f"hook: original program for {h[:8]}: {e}", flush=True)
            return False

    def tick(self, busy: bool):
        """Called each library poll: once idle, the held calls run, a few at a time."""
        if busy:
            return
        with self.lock:
            batch = self.data["queue"][:PER_TICK]
        for h in batch:
            self.forward(h)
            with self.lock:
                if h in self.data["queue"]:
                    self.data["queue"].remove(h)
                self._save()

    def status(self) -> dict:
        return {"installed": bool(self.data.get("saved")), "held": len(self.data.get("queue") or [])}
