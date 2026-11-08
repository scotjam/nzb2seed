"""Keep a file's original bytes before anything changes them.

A file built from Usenet is one version of a release. When it fails a few pieces of the
torrent nzb2seed built it for, those pieces get replaced - from another post, or downloaded
over BitTorrent - and the file becomes the torrent's version. But other trackers often carry
the release as the Usenet post had it: their torrents (cross-seed finds them) then need the
very bytes that were replaced, and once those are gone the only way back is a download.

So before a file is changed, its original is kept: a second name for the same bytes in
``<drive>/.nzb2seed-originals/<infohash>/<path in the torrent>``, on the same drive, so
keeping it is instant. Whatever then changes the file writes into a copy of its own, never
into the kept bytes. mend.py tries these originals on stopped torrents that are short, the
same way it tries another torrent's files.

A kept original is let go after ``keep_originals_days``. That removes only its name here:
a torrent mend finished from it keeps the bytes.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time

DIR = ".nzb2seed-originals"
_lock = threading.Lock()


def _state_path(cfg) -> str | None:
    path = getattr(cfg, "path", None)
    return os.path.join(os.path.dirname(os.path.abspath(path)), "originals.json") if path else None


def _load(cfg) -> list[dict]:
    if _state_path(cfg) is None:
        return []
    try:
        with open(_state_path(cfg), encoding="utf-8") as fh:
            return list(json.load(fh).get("kept") or [])
    except (OSError, ValueError, AttributeError):
        return []


def _save(cfg, kept: list[dict]):
    path = _state_path(cfg)
    if path is None:
        return
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"kept": kept}, fh, indent=1)
    os.replace(tmp, path)


def _drive_root(path: str) -> str:
    """The top of the filesystem ``path`` is on, so a hard link to it can be made there."""
    p = os.path.dirname(os.path.abspath(path))
    dev = os.stat(p).st_dev
    while True:
        up = os.path.dirname(p)
        if up == p or os.path.ismount(p):
            return p
        try:
            if os.stat(up).st_dev != dev:
                return p
        except OSError:
            return p
        p = up


def _kept_path(path: str, rel: str, infohash: str) -> str:
    return os.path.join(_drive_root(path), DIR, (infohash or "unknown")[:16], *rel.split("/"))


def keep(cfg, path: str, rel: str, infohash: str, why: str = "") -> str | None:
    """Give ``path``'s current bytes a second name before they are changed, unless an
    original of this file of this torrent is already kept (the first one is the one that
    matters: it is the version as built). Returns the kept path, or None if it could not
    be kept. The caller must then not write into ``path`` itself: it shares its bytes with
    the kept name now (``assemble.own_copy`` gives it its own, or replace it)."""
    if not float(getattr(cfg, "keep_originals_days", 3) or 0) or _state_path(cfg) is None:
        return None                       # switched off, or nowhere to note it
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return None
    with _lock:
        kept = _load(cfg)
        for k in kept:
            if k.get("hash") == infohash and k.get("rel") == rel and os.path.isfile(k.get("path", "")):
                return k["path"]
        dst = _kept_path(path, rel, infohash)
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.lexists(dst):
                os.remove(dst)
            os.link(path, dst)
        except OSError as e:
            print(f"originals: could not keep {path}: {e}", flush=True)
            return None
        kept.append({"path": dst, "rel": rel, "size": os.path.getsize(dst), "hash": infohash,
                     "at": time.time(), "why": why})
        _save(cfg, kept)
        return dst


def keep_before_writes(cfg, path: str, rel: str, infohash: str, why: str = "") -> bool:
    """Keep the original, then give ``path`` bytes of its own for whatever writes into it
    next (qBittorrent downloading pieces). Needs room for one copy of the file; without it,
    nothing is kept and False is returned."""
    from .assemble import own_copy
    try:
        size = os.path.getsize(path)
        free = shutil.disk_usage(os.path.dirname(path)).free
    except OSError:
        return False
    if free < size * 1.05 + 1024 ** 3:
        return False
    if keep(cfg, path, rel, infohash, why) is None:
        return False
    own_copy(path)
    return True


def candidates(cfg, rel: str, size: int) -> list[str]:
    """Kept originals that could be this file: the same path in the torrent, the same size."""
    return [k["path"] for k in _load(cfg)
            if k.get("rel") == rel and k.get("size") == size and os.path.isfile(k.get("path", ""))]


def kept_roots(cfg) -> set[str]:
    """The first part of every kept original's path in its torrent: the torrent names (the
    root folder, or the file itself) that may find a kept original to try."""
    return {(k.get("rel") or "").split("/")[0] for k in _load(cfg) if os.path.isfile(k.get("path", ""))}


def prune(cfg, now: float | None = None) -> int:
    """Let go of originals kept longer than ``keep_originals_days``, and forget any that are
    gone. Returns how many names were removed. The bytes stay wherever else they are used."""
    days = float(getattr(cfg, "keep_originals_days", 3) or 0)
    now = time.time() if now is None else now
    removed = 0
    with _lock:
        kept, left = _load(cfg), []
        for k in kept:
            p = k.get("path", "")
            if not os.path.isfile(p):
                continue
            if now - float(k.get("at") or 0) >= days * 86400:
                try:
                    os.remove(p)
                    removed += 1
                    _tidy(p)
                except OSError:
                    left.append(k)
                continue
            left.append(k)
        if left != kept:
            _save(cfg, left)
    return removed


def _tidy(path: str):
    """Remove the folders a let-go original leaves empty, up to .nzb2seed-originals itself."""
    d = os.path.dirname(path)
    while os.path.basename(d) and os.path.basename(d) != DIR:
        try:
            os.rmdir(d)
        except OSError:
            return
        d = os.path.dirname(d)
    try:
        os.rmdir(d)
    except OSError:
        pass


def written(qb, infohash: str, where) -> list[tuple[str, str]]:
    """[(name, path here)] of the files qBittorrent may write into when the torrent is started:
    the short ones, and a whole one sharing a piece with them (a piece is written whole)."""
    states = qb.piece_states(infohash)
    out = []
    for f in qb.files(infohash):
        if f.get("priority", 1) == 0:
            continue
        first, last = (f.get("piece_range") or [0, len(states) - 1])[:2]
        if f.get("progress", 0) >= 1 and all(x == 2 for x in states[first:last + 1]):
            continue
        p = where(f["name"])
        if p and os.path.isfile(p):
            out.append((f["name"], p))
    return out


def ready_to_download(cfg, qb, infohash: str, where, why: str = "") -> tuple[list[str], str]:
    """Make torrent ``infohash``'s files safe for qBittorrent to download into, before it is
    started: every file a missing piece touches - a short file, and a whole one sharing a
    piece with it (a piece is written whole, and before it is checked) - gets bytes of its
    own, its present version kept first as an original where Settings keep them. A file
    shared with the library or another torrent would otherwise change for them too.
    ``where(name)``: the file's path here, or None. Returns (files whose original was kept,
    why it must not start - "" when it is safe)."""
    from .assemble import own_copy
    paths = written(qb, infohash, where)
    room: dict[str, int] = {}
    for _, p in paths:
        if os.stat(p).st_nlink > 1:
            room[os.path.dirname(p)] = room.get(os.path.dirname(p), 0) + os.path.getsize(p)
    for d, need in room.items():
        if shutil.disk_usage(d).free < need * 1.05 + 1024 ** 3:
            return [], ("no room to give the files it would download into a copy of their own - "
                        "they are shared with other files, which a download would change too")
    kept = []
    for n, p in paths:
        try:
            if keep_before_writes(cfg, p, n, infohash, why):
                kept.append(n)
        except OSError:
            pass
        own_copy(p)                             # nothing to do once it has its own
    still = [n for n, p in paths if os.stat(p).st_nlink > 1]
    if still:
        return kept, (f"{still[0]}" + (f" (and {len(still) - 1} more)" if len(still) > 1 else "")
                      + " is still shared with other files, which a download would change too")
    return kept, ""
