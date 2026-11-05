"""SABnzbd's "Abort jobs that cannot be completed" (``fail_hopeless_jobs``, on by default).

With it on, SABnzbd stops downloading a post the moment it can tell the post cannot be
repaired, so its folder holds only what came before that. For an ordinary download that
saves time; for nzb2seed it throws away the very data a torrent could be built from: most
of a broken post usually checks out piece by piece. Here it is read, turned off for good
when you ask, or turned off only while the rest of such a post downloads and then put back
(a note on disk puts it back after a restart, too)."""
from __future__ import annotations

import json
import os
import threading

ABORTED = "Aborted, cannot be completed"

_lock = threading.Lock()
_holds = 0                  # posts downloading in full right now, with the abort off for them
_restore = False            # it was on before nzb2seed turned it off: put it back after


def is_aborted(slot: dict | None) -> bool:
    """Did SABnzbd stop this job early, because it could tell it could not be repaired?"""
    return str((slot or {}).get("fail_message") or "").startswith(ABORTED)


def aborts(sab) -> bool | None:
    """Is SABnzbd's abort on? None when SABnzbd cannot be asked."""
    try:
        v = (sab.config().get("misc") or {}).get("fail_hopeless_jobs")
    except Exception:                       # noqa: BLE001 - a status check, never fatal
        return None
    if v is None:
        return None
    return str(v).strip().lower() in ("1", "true", "on", "yes")


def _set(sab, on: bool):
    sab._call("set_config", section="misc", keyword="fail_hopeless_jobs", value="1" if on else "0")


def _marker(cfg) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(cfg.path)), "sab_abort_restore.json")


def _note(cfg, on: bool):
    p = _marker(cfg)
    if on:
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"fail_hopeless_jobs": 1}, fh)
    else:
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


def turn_off(cfg, sab):
    """Settings: posts are downloaded in full from now on - SABnzbd's abort stays off."""
    global _restore
    with _lock:
        _set(sab, False)
        _restore = False                    # nothing to put back when a download finishes
        _note(cfg, False)


def hold(cfg, sab) -> bool:
    """One post is about to download in full: the abort goes off until it is released.
    False when SABnzbd could not be asked or changed (the post is then not downloaded)."""
    global _holds, _restore
    with _lock:
        if _holds == 0:
            now = aborts(sab)
            if now is None:
                return False
            if now:
                try:
                    _set(sab, False)
                except Exception:           # noqa: BLE001
                    return False
                _restore = True
                _note(cfg, True)
            else:
                _restore = False
        _holds += 1
        return True


def release(cfg, sab):
    """That post has finished: when it was the last one, the abort is put back as it was."""
    global _holds, _restore
    with _lock:
        if _holds == 0:
            return
        _holds -= 1
        if _holds or not _restore:
            return
        try:
            _set(sab, True)
        except Exception:                   # noqa: BLE001 - the note puts it back on the next start
            return
        _restore = False
        _note(cfg, False)


def restore_after_restart(cfg, sab) -> bool:
    """A restart while a post downloaded in full: put the abort back. True when it was."""
    with _lock:
        if _holds or not os.path.exists(_marker(cfg)):
            return False
        try:
            _set(sab, True)
        except Exception:                   # noqa: BLE001 - tried again on the next start
            return False
        _note(cfg, False)
        return True
