"""Is a release worth building? Answered from what your own torrents actually upload.

This module measures: how much every torrent from a tracker, of a size, of a kind, from a
release group has uploaded for each GB it occupies. What to *do* about it lives in
rules.py, where the answer is a list of rules you wrote rather than a score.

The measure is **upload per GB stored**, not total upload: a 50 GB remux that returns
50 GB is worth the same as a 5 GB episode that returns 5 GB, and a disk is only so big.

Three rules keep it honest:

* only torrents older than ``demand_age_days`` count - a new one has not had its chance;
* a group of fewer than ``demand_min_sample`` torrents is never used to refuse anything,
  so a tracker you have barely used gets a fair trial instead of being locked out by a
  few unlucky torrents;
* it reads your own history only, and says which numbers it refused on, so it can be
  argued with.
"""
from __future__ import annotations

import json
import os
import re
import time
from urllib.parse import urlsplit

GB = 1024 ** 3
BUCKETS = ((1 * GB, "<1G"), (5 * GB, "1-5G"), (15 * GB, "5-15G"),
           (30 * GB, "15-30G"), (60 * GB, "30-60G"), (float("inf"), ">60G"))
_EP = re.compile(r"(?<![a-z0-9])s\d{1,3}e\d{1,3}", re.I)
_SEASON = re.compile(r"(?<![a-z0-9])s\d{1,3}(?![0-9e])", re.I)


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def host_of(url: str) -> str:
    try:
        h = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    return h.lower()


def tracker_of(trackers) -> str:
    for u in trackers or []:
        h = host_of(u)
        if h:
            return h
    return ""


def bucket_of(size: int) -> str:
    for lim, name in BUCKETS:
        if size < lim:
            return name
    return ">60G"


_TYPES = (
    # (name, pattern). First match wins, so the specific ones come first.
    ("xxx", r"(?<![a-z0-9])(xxx|porn|brazzers|onlyfans|naughtyamerica|blacked|hentai)(?![a-z0-9])"),
    ("sport", r"(?<![a-z0-9])(f1|formula\.?1|motogp|nfl|nba|nhl|mlb|uefa|epl|premier\.league|"
              r"ligue\.?1|laliga|serie\.?a|bundesliga|ufc|wwe|aew|grand\.prix|world\.cup)(?![a-z0-9])"),
    ("discography", r"(?<![a-z0-9])(discography|complete\.works|anthology|box\.?set)(?![a-z0-9])"),
    ("audiobook", r"(?<![a-z0-9])(audiobook|unabridged|audible|m4b)(?![a-z0-9])"),
    ("ebook", r"(?<![a-z0-9])(ebook|epub|mobi|azw3|retail\.pdf|comic|cbr|cbz|magazine)(?![a-z0-9])"),
    ("game", r"(?<![a-z0-9])(repack|gog|codex|plaza|razor1911|empress|fitgirl|dodi|"
             r"nsw|switch|ps[345]|xbox|steamrip)(?![a-z0-9])"),
    ("software", r"(?<![a-z0-9])(x64|win(?:10|11|64)|macos|multilingual|keygen|portable|"
                 r"activated|cracked)(?![a-z0-9])"),
    ("music", r"(?<![a-z0-9])(flac|mp3|320kbps|web\.flac|vinyl|cd|ep|single|album|"
              r"24bit|16bit|hi\.?res)(?![a-z0-9])"),
    ("anime", r"(?<![a-z0-9])(erai\.?raws|subsplease|horriblesubs|judas|multisub|"
              r"\[?anime\]?|dual\.audio\.jpn)(?![a-z0-9])"),
)
_TYPE_RES = [(name, re.compile(pat, re.I)) for name, pat in _TYPES]
_VIDEO = re.compile(r"(?<![a-z0-9])(1080p|2160p|720p|480p|576p|bluray|web-?dl|webrip|hdtv|"
                    r"remux|x264|x265|h\.?26[45]|hevc|dvdrip|bdrip)(?![a-z0-9])", re.I)
_GROUP = re.compile(r"-([A-Za-z0-9_]{2,20})(?:\.[A-Za-z0-9]{2,4})?$")


def kind_of(name: str) -> str:
    """What kind of thing this release is, from its name.

    The video kinds are decided first when the name looks like video, because "album" and
    "cd" turn up inside film names often enough to matter; everything else falls back to
    the word patterns above, then to "other"."""
    n = (name or "").replace(" ", ".")
    if _VIDEO.search(n):
        for label, pat in _TYPE_RES[:2]:              # xxx and sport beat any video kind
            if pat.search(n):
                return label
        if _EP.search(n):
            return "tv episode"
        if _SEASON.search(n):
            return "tv season"
        return "movie"
    for label, pat in _TYPE_RES:
        if pat.search(n):
            return label
    if _EP.search(n):
        return "tv episode"
    if _SEASON.search(n):
        return "tv season"
    return "other"


def group_of(name: str) -> str:
    """The release group, from the tail of the name. "" when it has none."""
    m = _GROUP.search((name or "").strip().replace(" ", "."))
    return m.group(1).upper() if m else ""


def facets(host: str, size: int, name: str) -> dict:
    """What a release is, on every axis a rule can be written about."""
    return {"tracker": host, "type": kind_of(name), "size": bucket_of(size),
            "group": group_of(name), "tracker+type": f"{host} {kind_of(name)}"}


def facet_keys(host: str, size: int, name: str) -> list[str]:
    """The rule names this release would be matched by, e.g. "tracker:x.org"."""
    return [f"{k}:{v}" for k, v in facets(host, size, name).items() if v]


def keys_for(host: str, size: int, name: str) -> list[str]:
    """Most specific first: this tracker at this size and kind, then looser, then the lot."""
    b, k = bucket_of(size), kind_of(name)
    return [f"{host}|{b}|{k}", f"{host}|{b}", f"{host}", "*"]


def collapse(rows: list[dict]) -> list[dict]:
    """Count data once, and credit every copy of it to the torrent that brought it in.

    A cross-seed is the same files registered with a second tracker: it takes no extra
    disk, so counting it as its own line makes a tracker look as though it wasted space it
    never used, and makes the copy look dead when the data it points at is seeding
    perfectly well somewhere else.

    Each group of torrents sharing one path becomes a single line: the disk of the
    earliest (the one whose grab created the files), and the upload of all of them. That
    is the number a decision needs - "if I build this, what comes back for the space?" -
    because the cross-seeds only exist because the first one did.
    """
    groups: dict[tuple, list[dict]] = {}
    for t in rows:
        # the same release under two trackers is the same bytes: cross-seed hard-links it
        # into a folder of its own, so the path is no use for spotting it - the name and
        # the exact byte count are.
        name, size = _norm(t.get("name") or ""), t.get("size") or 0
        key = (name, size) if name and size else ((t.get("content_path") or "").strip(),)
        groups.setdefault(key, []).append(t)
    out = []
    for items in groups.values():
        first = min(items, key=lambda x: x.get("added_on") or 0)
        up = sum(x.get("uploaded") or 0 for x in items)
        out.append({**first, "uploaded": up, "copies": len(items),
                    "cross_uploaded": up - (first.get("uploaded") or 0)})
    return out


def summarise(torrents: list[dict], age_days: float = 7.0, now: float | None = None) -> dict:
    """{key: [how many, median upload per GB stored]} from qBittorrent's own numbers.

    Pass rows through collapse() first unless they have been already: a cross-seed counted
    on its own makes the tracker that brought the data in look wasteful."""
    now = now or time.time()
    seen: dict[str, list[float]] = {}
    for t in torrents:
        size = t.get("size") or 0
        if size <= 0 or (now - (t.get("added_on") or 0)) < age_days * 86400:
            continue
        host = host_of(t.get("tracker") or "")
        if not host:
            continue
        ratio = (t.get("uploaded") or 0) / size
        for key in keys_for(host, size, t.get("name") or ""):
            seen.setdefault(key, []).append(ratio)
    out = {}
    for key, vals in seen.items():
        vals.sort()
        out[key] = [len(vals), round(vals[len(vals) // 2], 4)]
    return out


def look_up(stats: dict, host: str, size: int, name: str, least: int) -> tuple[str, int, float] | None:
    """The most specific group with enough torrents to mean anything."""
    for key in keys_for(host, size, name):
        row = stats.get(key)
        if row and row[0] >= least:
            return key, int(row[0]), float(row[1])
    return None


def rule_stats(rows: list[dict], age_days: float = 7.0, now: float | None = None) -> dict:
    """{"rule:<kind>:<value>": [how many, median return]} - the evidence behind each rule."""
    now = now or time.time()
    seen: dict[str, list[float]] = {}
    for t in collapse(rows):
        size = t.get("size") or 0
        if size <= 0 or (now - (t.get("added_on") or 0)) < age_days * 86400:
            continue
        host = host_of(t.get("tracker") or "")
        ratio = (t.get("uploaded") or 0) / size
        for key in facet_keys(host, size, t.get("name") or ""):
            seen.setdefault("rule:" + key, []).append(ratio)
    return {k: [len(v), round(sorted(v)[len(v) // 2], 4)] for k, v in seen.items()}


def rules(rows: list[dict], age_days: float = 7.0, least: int = 8, dead_pct: int = 60,
          ratio: float = 0.05, now: float | None = None) -> list[dict]:
    """Concrete rules worth considering, each standing on its own evidence.

    A rule is offered when a group of at least ``least`` torrents has both a poor return
    and a majority that never uploaded at all - and it reports how much disk it would
    have saved and how much upload it would have cost, so the trade is visible per rule
    rather than hidden inside one number."""
    now = now or time.time()
    old = [t for t in collapse(rows)
           if (t.get("size") or 0) > 0 and now - (t.get("added_on") or 0) >= age_days * 86400]
    groups: dict[str, list] = {}
    for t in old:
        host = host_of(t.get("tracker") or "")
        for key in facet_keys(host, t["size"], t.get("name") or ""):
            groups.setdefault(key, []).append(t)
    out = []
    for key, items in groups.items():
        if key.split(":", 1)[0] in ("tracker", "tracker+type"):
            continue          # see rules.NOT_PROPOSED: a weak tracker is where the work is
        if len(items) < least:
            continue
        vals = sorted((x.get("uploaded") or 0) / x["size"] for x in items)
        med = vals[len(vals) // 2]
        dead = sum(1 for v in vals if v == 0)
        if med > ratio or 100 * dead / len(items) < dead_pct:
            continue
        kind, _, value = key.partition(":")
        out.append({
            "rule": key, "what": kind, "value": value, "n": len(items),
            "ratio": round(med, 2), "dead": round(100 * dead / len(items)),
            "saves_gb": round(sum(x["size"] for x in items) / GB, 1),
            "costs_gb": round(sum(x.get("uploaded") or 0 for x in items) / GB, 1),
        })
    out.sort(key=lambda r: (-r["saves_gb"], r["costs_gb"]))
    return out


class Stats:
    """The summary, kept in a file and refreshed now and then."""

    def __init__(self, path: str, max_age: float = 3600.0):
        self.path, self.max_age = path, max_age

    def load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            return {"at": 0, "stats": {}}
        return {"at": d.get("at") or 0, "stats": d.get("stats") or {}}

    def save(self, stats: dict):
        tmp = f"{self.path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"at": time.time(), "stats": stats}, fh)
        os.replace(tmp, self.path)

    def current(self, cfg, qb, force: bool = False) -> dict:
        held = self.load()
        if not force and held["stats"] and time.time() - held["at"] < self.max_age:
            return held["stats"]
        if qb is None:
            return held["stats"]
        try:
            rows = qb.torrents()
        except Exception:
            return held["stats"]
        stats = summarise(collapse(rows), cfg.demand_age_days)
        try:
            self.save(stats)
        except OSError:
            pass
        return stats


def table(stats: dict, least: int = 8, top: int = 25) -> list[dict]:
    """The per-tracker summary the Settings page shows, worst first."""
    rows = [{"where": k, "n": v[0], "ratio": v[1]}
            for k, v in stats.items() if "|" not in k and k != "*" and v[0] >= least]
    rows.sort(key=lambda r: r["ratio"])
    return rows[:top]


def _pct(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    i = min(len(vals) - 1, max(0, int(round(p * (len(vals) - 1)))))
    return sorted(vals)[i]


def overview(rows: list[dict], age_days: float = 7.0, least: int = 8,
             now: float | None = None) -> dict:
    """Everything the dashboard shows: what your seeding actually returns, which trackers
    and sizes pay for their space, and a limit worth starting from.

    The recommendations are rules, not a score: "this tracker", "releases of this kind",
    "this release group", "this size" - each offered only where a group of your own
    torrents both returns almost nothing and is mostly dead, and each carrying the disk it
    would save and the upload it would cost."""
    now = now or time.time()
    old = [t for t in collapse(rows)
           if (t.get("size") or 0) > 0 and now - (t.get("added_on") or 0) >= age_days * 86400]
    ratios = sorted((t.get("uploaded") or 0) / t["size"] for t in old)
    stored = sum(t["size"] for t in old)
    up = sum(t.get("uploaded") or 0 for t in old)
    dead = [t for t in old if not t.get("uploaded")]
    stats = summarise(old, age_days=0, now=now)        # already filtered by age

    offer = rules(old, age_days=0, least=least, now=now)

    def group(name, keyfn):
        by: dict[str, list] = {}
        for t in old:
            by.setdefault(keyfn(t), []).append((t.get("uploaded") or 0) / t["size"])
        out = [{"where": k, "n": len(v), "ratio": round(_pct(v, 0.5), 2),
                "dead": round(100 * sum(1 for x in v if x == 0) / len(v))}
               for k, v in by.items() if len(v) >= least]
        out.sort(key=lambda r: r["ratio"])
        return {"what": name, "rows": out}

    copies = sum(t.get("copies", 1) - 1 for t in old)
    cross_up = sum(t.get("cross_uploaded") or 0 for t in old)
    return {
        "torrents": len(old), "stored_gb": round(stored / GB, 1), "uploaded_gb": round(up / GB, 1),
        "cross_seeds": copies, "cross_uploaded_gb": round(cross_up / GB, 1),
        "overall_ratio": round(up / stored, 2) if stored else 0,
        "dead": len(dead), "dead_gb": round(sum(t["size"] for t in dead) / GB, 1),
        "median_ratio": round(_pct(ratios, 0.5), 2),
        "rules": offer,
        "by": [group("tracker", lambda t: host_of(t.get("tracker") or "") or "(none)"),
               group("type", lambda t: kind_of(t.get("name") or "")),
               group("size", lambda t: bucket_of(t["size"])),
               group("group", lambda t: group_of(t.get("name") or "") or "(none)"),
               group("category", lambda t: t.get("category") or "(none)")],
        "stats": stats,
    }
