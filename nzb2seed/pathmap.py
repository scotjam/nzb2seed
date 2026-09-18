"""Translate paths between how SABnzbd, this machine and qBittorrent see them.

Each mapping is a list of ``[from_prefix, to_prefix]`` pairs; the longest
matching prefix wins. Separators follow the style of the *target* prefix, so
``/downloads/x`` can become ``\\\\nas\\downloads\\x`` and back.
"""
from __future__ import annotations

import re

_WIN = re.compile(r"^([a-zA-Z]:|\\\\|//)")


def _is_windows(p: str) -> bool:
    return bool(_WIN.match(p)) or ("\\" in p and "/" not in p)


def _norm(p: str) -> str:
    p = p.replace("\\", "/")
    return p.rstrip("/") if len(p) > 1 else p


def map_path(path: str, pairs) -> str:
    if not pairs:
        return path
    src = _norm(path)
    best = None
    for frm, to in pairs:
        f = _norm(frm)
        fold = _is_windows(frm)
        a, b = (src.casefold(), f.casefold()) if fold else (src, f)
        if a == b or a.startswith(b + "/") or (b == "/" and a.startswith("/")):
            if best is None or len(f) > len(_norm(best[0])):
                best = (frm, to)
    if best is None:
        return path
    frm, to = best
    rest = src[len(_norm(frm)):].lstrip("/")
    sep = "\\" if _is_windows(to) else "/"
    base = to.rstrip("/\\")
    if not rest:
        return base or sep
    return base + sep + rest.replace("/", sep)
