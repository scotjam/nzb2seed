"""Pair a torrent release with the Usenet release(s) that contain the same files."""
from __future__ import annotations

import re

from .clients import Release

_SEP = re.compile(r"[\s._]+")
_SEASON_PACK = re.compile(r"^(?P<pre>.+?)\.s(?P<season>\d{1,4})\.(?P<post>.+)$")
_HAS_EPISODE = re.compile(r"\.s\d{1,4}e\d{1,4}", re.I)


def norm(title: str) -> str:
    t = _SEP.sub(".", title.strip().lower())
    t = re.sub(r"\.?-\.?", "-", t)
    return t.strip(".")


def exact_matches(torrent: Release, nzbs: list[Release]) -> list[Release]:
    key = norm(torrent.title)
    return [n for n in nzbs if norm(n.title) == key]


def season_pack_parts(title: str):
    """For 'Show.S01.1080p.BluRay.x264-GRP' return (pre, season, post); else None."""
    n = norm(title)
    if _HAS_EPISODE.search(n):
        return None
    m = _SEASON_PACK.match(n)
    return (m["pre"], int(m["season"]), m["post"]) if m else None


def episode_query(title: str) -> str | None:
    """Search text likely to find the individual episodes of a season pack."""
    parts = season_pack_parts(title)
    if not parts:
        return None
    pre, _, post = parts
    return f"{pre} {post}".replace(".", " ").replace("-", " ")


def episode_matches(torrent: Release, nzbs: list[Release]) -> list[Release]:
    """One NZB per episode whose name is the season pack's name with SxxEyy in place of Sxx."""
    parts = season_pack_parts(torrent.title)
    if not parts:
        return []
    pre, season, post = parts
    rx = re.compile(rf"^{re.escape(pre)}\.s0*{season}(?P<ep>(?:-?e\d{{1,4}})+)\.{re.escape(post)}$")
    best: dict[str, Release] = {}
    for n in nzbs:
        m = rx.match(norm(n.title))
        if m:
            ep = m["ep"].lstrip("-")
            cur = best.get(ep)
            if cur is None or (n.grabs or 0) > (cur.grabs or 0):
                best[ep] = n
    return [best[k] for k in sorted(best)]


def rank_nzbs(nzbs: list[Release]) -> list[Release]:
    """Most-grabbed first: popular posts are the ones most likely to be complete."""
    return sorted(nzbs, key=lambda r: (r.grabs or 0), reverse=True)
