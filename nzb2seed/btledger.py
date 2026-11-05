"""What each torrent has taken over BitTorrent, across every time nzb2seed started it.

A tracker counts what you download of a torrent in total, not per attempt: a torrent
started at 97% that downloads its 3%, is removed, rebuilt and started again at 96% has
taken 7% there, not 4%. So every time nzb2seed starts a torrent that is not complete, it
writes down how much that start may download, and qBittorrent's own count of what it did
download. The next start of the same torrent has to fit what is left of the tracker's
limit after all of it.

One record per infohash, with one entry per time the torrent was in qBittorrent
(qBittorrent's ``added_on`` tells them apart): ``allowed`` - the most it was let download,
``seen`` - the most qBittorrent was seen to have downloaded. An entry still in
qBittorrent counts what it has downloaded so far; one that is gone counts the larger of
the two, since nobody saw the end of it.
"""
from __future__ import annotations

import json
import os
import threading
import time

_lock = threading.Lock()


def _path(cfg) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(cfg.path)), "bt_downloads.json")


def _load(cfg) -> dict:
    try:
        with open(_path(cfg), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save(cfg, d: dict):
    tmp = f"{_path(cfg)}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=1)
    os.replace(tmp, _path(cfg))


def taken(cfg, infohash: str, info: dict | None) -> int:
    """Bytes this torrent has already taken (or was let take) over BitTorrent, on every
    earlier start - ``info``: qBittorrent's row for it now, if it is there."""
    rec = _load(cfg).get(infohash) or {}
    now = (info or {}).get("added_on")
    total = 0
    for e in rec.get("entries", []):
        if info and now is not None and e.get("added_on") == now:
            continue                                        # counted below, as it is now
        total += max(int(e.get("allowed") or 0), int(e.get("seen") or 0))
    if info:
        total += int(info.get("downloaded") or 0)
    return total


def record_start(cfg, infohash: str, tracker: str, info: dict, short: int | None):
    """A start of a torrent that is ``short`` bytes from complete."""
    with _lock:
        d = _load(cfg)
        rec = d.setdefault(infohash, {"entries": []})
        rec.update(tracker=tracker, size=info.get("total_size") or info.get("size") or rec.get("size"))
        got = int(info.get("downloaded") or 0)
        mine = next((e for e in rec["entries"] if e.get("added_on") == info.get("added_on")), None)
        if mine is None:
            mine = {"added_on": info.get("added_on"), "allowed": 0, "seen": 0, "first": time.time()}
            rec["entries"].append(mine)
        mine["allowed"] = max(int(mine.get("allowed") or 0), got + int(short or 0))
        mine["seen"] = max(int(mine.get("seen") or 0), got)
        mine["last"] = time.time()
        _save(cfg, d)


def observe(cfg, rows: list[dict]):
    """qBittorrent's rows: what each recorded torrent has downloaded so far."""
    with _lock:
        d = _load(cfg)
        changed = False
        for r in rows:
            rec = d.get(r.get("hash") or "")
            if not rec:
                continue
            for e in rec.get("entries", []):
                if e.get("added_on") == r.get("added_on") and int(r.get("downloaded") or 0) > int(e.get("seen") or 0):
                    e["seen"] = int(r.get("downloaded") or 0)
                    changed = True
        if changed:
            _save(cfg, d)
