"""The "File list to seeds" tab: a list of torrent names pasted from a tracker's
web page, checked one by one for Usenet posts that could build them.

For each name Prowlarr asks Usenet for posts of it and, only if there are some, the chosen
tracker for the torrent. A name is *plausible* when Usenet has a post of the same release
(the same name, at least as big as the torrent), or, for a season pack, posts of each
episode of the same release adding up to the torrent's size. Only those are offered for
building; nothing is downloaded by checking.

Names copied from a web page come with whatever was beside them - sizes, dates, seeders,
the uploader. Each line is read for the part that looks like a release name.

The checking is done here, not in the page: lists queue up and are checked one after
another (a phone's browser pauses a page it is not showing), and a restart carries on.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import os
import re
import threading
import time

from . import matching
from .clients import Prowlarr, Release
from .config import Config

# ---------------------------------------------------------------- the rules (Advanced options)
# How pasted names are read and matched. These are the defaults, which suit UNIT3D sites
# (each torrent listed under its uploader's release name, the uploader in a column of its
# own) and do no harm elsewhere; the tab's Advanced options
# change them for one tracker (or for every tracker without its own), for a tracker whose
# pages read badly. Kept in tracklist_rules.json next to the config.
DEFAULT_RULES = {
    "strip_uploader": True,       # the word after a release group is the uploader, and left off
    "uploader_words": ["anon", "anonymous", "uploader"],         # stand-ins for a hidden uploader
    "no_group_words": ["nogroup", "nogrp", "unknown"],           # a group that says there is none
    "skip_words": ["patch", "update"],   # lines with any of these words are never names
    "renamed_match": True,        # match names a tracker writes its own way by what the release is
    "must_agree": ["year", "source", "codec", "hdr", "service"],  # ...and these have to agree
    "title_year_search": True,    # also search a film by its title and year alone
    "groupless_max_pct": 6,       # a post matched with no group: at most this much bigger than the torrent
    "other_tags": ["audiobook", "ebook", "e-book", "pdf", "epub", "mobi", "azw", "azw3", "mp3", "m4b",
                   "flac", "multi", "pack", "comic", "cbr", "cbz", "retail", "repack", "portable",
                   "x64", "x86", "mac", "macos", "win", "windows"],     # end tags of other kinds of names
    "repost_suffixes": ["xpost", "asrequested", "obfuscated", "postbot", "repost"],
    "not_release_words": ["update", "patch", "trainer", "crack", "dlc"],  # posts that are not the game itself
}
AGREE = ("year", "source", "codec", "hdr", "service")
_LISTS = ("uploader_words", "no_group_words", "skip_words", "other_tags", "repost_suffixes", "not_release_words")
_active = threading.local()


def clean_rules(r: dict) -> dict:
    """Rules as given (from the page), checked: unknown keys dropped, lists of plain words."""
    out = {}
    for k, v in (r or {}).items():
        if k not in DEFAULT_RULES:
            continue
        if k in _LISTS:
            words = v.split(",") if isinstance(v, str) else (v if isinstance(v, list) else [])
            out[k] = list(dict.fromkeys(str(w).strip().lower() for w in words if str(w).strip()
                                        and re.fullmatch(r"[\w+.-]{1,40}", str(w).strip())))
        elif k == "must_agree":
            out[k] = [x for x in (v if isinstance(v, list) else []) if x in AGREE]
        elif k == "groupless_max_pct":
            try:
                out[k] = max(0.0, min(100.0, float(v)))
            except (TypeError, ValueError):
                pass
        else:
            out[k] = bool(v)
    return out


class RulesStore:
    """{"default": {...}, "trackers": {name: {...}}}: the rules saved from the tab."""

    def __init__(self, path: str):
        self.path, self.lock = path, threading.Lock()
        try:
            with open(path, encoding="utf-8") as fh:
                self.data = json.load(fh)
        except (OSError, ValueError):
            self.data = {}
        self.data.setdefault("default", {})
        self.data.setdefault("trackers", {})

    def rules(self, tracker: str = "") -> dict:
        """The rules in force for this tracker: its own, else the default ones."""
        own = self.data["trackers"].get((tracker or "").strip())
        return {**DEFAULT_RULES, **clean_rules(self.data["default"]), **clean_rules(own or {})}

    def own(self, tracker: str) -> bool:
        return bool(tracker and tracker.strip() in self.data["trackers"])

    def save(self, tracker: str, rules: dict):
        with self.lock:
            r = clean_rules(rules)
            if tracker:
                self.data["trackers"][tracker.strip()] = r
            else:
                self.data["default"] = r
            self._write()

    def reset(self, tracker: str):
        with self.lock:
            if tracker:
                self.data["trackers"].pop(tracker.strip(), None)
            else:
                self.data["default"] = {}
            self._write()

    def _write(self):
        tmp = f"{self.path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=1)
        os.replace(tmp, self.path)


class using:
    """``with using(rules):`` - reading and matching in this thread follow these rules."""

    def __init__(self, rules):
        self.rules = {**DEFAULT_RULES, **(rules or {})}

    def __enter__(self):
        self.before = getattr(_active, "rules", None)
        _active.rules = self.rules
        return self.rules

    def __exit__(self, *exc):
        _active.rules = self.before


def rule(key: str):
    return (getattr(_active, "rules", None) or DEFAULT_RULES)[key]


def _words_rx(words):
    words = [re.escape(w) for w in words if w]
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(words) + r")(?![a-z0-9])", re.I) if words else None

ENOUGH = 0.97                # posts this much of the torrent's size are big enough (par2 aside)
GIB = 1024 ** 3
_RELEASEISH = re.compile(r"(?<![a-z0-9])(?:s\d{1,3}(?:e\d{1,4})?|(?:19|20)\d{2}|(?:2160|1080|720|576|480)[pi]"
                         r"|web-?dl|webrip|bluray|hdtv|remux|x26[45]|h\.?26[45])(?![a-z0-9])", re.I)
_GROUP_END = re.compile(r"-[A-Za-z0-9]{2,}$")
_NOISE = re.compile(r"^(?:\d+(?:[.,]\d+)?\s*(?:[kmgt]i?b|bytes?)|\d+|[\d\s:/.-]+|.{0,7})$", re.I)


def loose(name: str) -> str:
    """Letters and digits only: "Show S01 1080p WEB-DL DDP5 1" is "Show.S01.1080p.WEB-DL.DDP5.1"."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


_YEAR = re.compile(r"^(?:19|20)\d{2}$")
_PREFIX = re.compile(r"^(?:on torrent|torrent)\s+", re.I)    # "On Torrent <name>" in a comment list


def _score(cell: str) -> int:
    """How much a cell of a pasted row looks like a release name (0: not at all).

    A release name carries at least two of a resolution, a source, a codec, a season or
    episode, a year - and not only years: "Film (2019)" in a list of titles, or the
    "2024-2026" of a page's footer, is not a torrent. And a title comes first: "1080p
    WEB-DL" alone is a heading."""
    c = cell.strip()
    if _NOISE.match(c):
        return 0
    marks = _RELEASEISH.findall(c)
    if len(marks) < 2 or all(_YEAR.match(m) for m in marks):
        return 0
    # the title is what comes before the first mark that is not a year (a film can be named after a year)
    first = next(m for m in _RELEASEISH.finditer(c) if not _YEAR.match(m.group()))
    if not re.search(r"[^\W_]", c[:first.start()]):        # no title before it: "1080p WEB-DL", a heading
        return 0
    # a release names a resolution, a year or a season - a page's row of source filters
    # ("BDRip BluRay REMUX WEB-DL WEBRip") names only sources
    if not re.search(r"(?<![a-z0-9])(?:(?:19|20)\d{2}|s\d{1,3}|(?:2160|1080|720|576|480)[pi])(?![a-z0-9])", c, re.I):
        return 0
    return len(marks) * 3 + (2 if _GROUP_END.search(c) else 0) + (1 if " " not in c and "." in c else 0)


_NOT_GROUP = {"dl", "rip", "ray", "hd", "ma", "es"}       # WEB-DL, Blu-ray, DTS-HD MA ...


def bare(name: str) -> str:
    """The release name without the uploader a tracker's list shows after it, which comes
    along when the list is copied: "Film 2009 1080p BluRay x264-GRP Some-User" is "Film 2009
    1080p BluRay x264-GRP", "... DTS-HD MA 5.1 Uploader" loses "Uploader". Only a name
    written with spaces, or a dotted one followed by a space, carries one."""
    n = (name or "").strip()
    toks = n.split(" ")
    if not rule("strip_uploader") or len(toks) < 2 or _RELEASEISH.search(toks[-1]) \
            or (len(toks) == 2 and "." not in toks[0]):
        return n
    m = _GROUP_END.search(toks[-2])
    if (m and m.group()[1:].lower() not in _NOT_GROUP) or toks[-1].lower() in rule("uploader_words"):
        return " ".join(toks[:-1])
    return n


# Not only video (the tab's "less reliable parsing" option): books, audiobooks, software and
# games name no resolution or source, so they are told by how such names end instead
_SCENE_END = re.compile(r"\s-\s[A-Z0-9][A-Za-z0-9]{1,}(?:\s\[[^\]]+\])*$")        # "... - GRP [MULTI]"
_VERSION = re.compile(r"(?<![a-z0-9])v\d+(?:\.\d+)+(?![0-9])", re.I)            # "v1.2.345"
_WORDS = re.compile(r"[^\W\d_]{2,}")

# A scene game or program as a tracker lists it - "Title v1.0.3 2026 - GRP [MULTI]" - and
# as its group posts it on Usenet: "Title-GRP" (a repost adds "-xpost" or the like)
_OTHER = re.compile(r"^(?P<body>.+?)\s+-\s+(?P<group>[A-Za-z0-9]{2,})(?P<tags>(?:\s*\[[^\]]*\])*)\s*$")
_TITLE_END = re.compile(r"\s+(?:v\d[\w.]*|(?:19|20)\d{2}|\d{6,})(?:\s|$)", re.I)  # version, year or build


def other_facets(name: str) -> dict | None:
    """Title and group of a game or program named the scene's way, or None: no group, or
    one that names none ("NoGroup") - then only its exact name will do."""
    m = _OTHER.match(bare(name).strip())
    if not m:
        return None
    group = m.group("group")
    if group.lower() in rule("no_group_words"):
        return None
    body = m.group("body")
    end = _TITLE_END.search(body)
    title = re.sub(r"\s+", " ", (body[:end.start()] if end else body).replace(":", " ")).strip()
    return {"title": title, "key": loose(title), "group": group.lower()} if loose(title) else None


def other_alike(post: str, o: dict) -> bool:
    """Is ``post`` the same game or program from the same group: "Title-GRP", with no
    update, patch or trainer in its name (those are not the release)?"""
    p = post.strip()
    reposts = [re.escape(w) for w in rule("repost_suffixes")]
    if reposts:
        p = re.sub(r"-(?:" + "|".join(reposts) + r")$", "", p, flags=re.I)
    m = re.match(r"^(?P<title>.+)-(?P<group>[A-Za-z0-9]+)$", p)
    not_it = _words_rx(rule("not_release_words"))
    if not m or m.group("group").lower() != o["group"] or (not_it and not_it.search(m.group("title"))):
        return False
    return loose(m.group("title")) == o["key"]


def _other_score(cell: str) -> int:
    """How much a cell looks like the name of a book, audiobook, program or game (0: not
    at all) - less reliably than a video release can be told: it ends in a tag such as
    [AUDIOBOOK] or [PDF], or in a scene group after a dash ("- GRP"), or carries a
    version number, or is "Title - Author" with a few words on each side."""
    c = cell.strip()
    if _NOISE.match(c) or len(c) > 200:
        return 0
    words = _WORDS.findall(c)
    if len(words) < 2:
        return 0
    tags = [re.escape(t) for t in rule("other_tags")]
    tag_end = re.search(r"\[(?:" + "|".join(tags) + r")\]$", c, re.I) if tags else None
    if tag_end or (_SCENE_END.search(c) and (_VERSION.search(c) or re.search(r"\[[^\]]+\]$", c))):
        return 2
    if _VERSION.search(c) and len(words) >= 2:
        return 1
    parts = c.split(" - ")
    if len(parts) == 2 and all(len(_WORDS.findall(p)) >= 2 for p in parts) and len(words) <= 16 \
            and not re.search(r"\d+\s*(?:[kmgt]i?b|seeders?|leechers?)\b", c, re.I):
        return 1
    return 0


def parse(text: str, any_kind: bool = False) -> list[str]:
    """The release names in pasted text: one per line at most, each the cell of that line
    (tab-separated, as a copied table is) that looks most like one, without the uploader
    after it. Duplicates once. ``any_kind``: books, audiobooks, programs and games too,
    told apart less reliably (see _other_score)."""
    score = (lambda c: _score(c) * 10 or _other_score(c)) if any_kind else _score
    skip = _words_rx(rule("skip_words"))          # "[PATCH]": no release of its own
    out, seen = [], set()
    for line in (text or "").splitlines():
        if skip and skip.search(line):
            continue
        cells = [c.strip() for c in re.split(r"\t|\s{3,}", line) if c.strip()]
        best = max(cells, key=score, default="")
        if not best or not score(best):
            continue
        name = bare(_PREFIX.sub("", re.sub(r"\.torrent$", "", best, flags=re.I)).strip())
        if loose(name) not in seen:
            seen.add(loose(name))
            out.append(name)
    return out


_JOB_PREFIX = re.compile(r"^(?:auto|download over bittorrent):\s*", re.I)
_VIDEO = re.compile(r"\.(?:mkv|mp4|avi|ts|m2ts)$", re.I)


def job_key(title: str) -> str:
    """A job's title as the torrent's name: "auto: Film.2020.1080p-Rel.mkv" is "Film.2020.1080p-Rel"."""
    return loose(_VIDEO.sub("", _JOB_PREFIX.sub("", title.strip())))


def short_query(name: str) -> str | None:
    """The title and year (or season) of a release name: "Film.2020.1080p.Remux-Rel" is
    "Film 2020", "Show.S01.1080p-Rel" is "Show S01". Some trackers find
    nothing for a whole release name but find it by its title."""
    c = re.sub(r"[._]+", " ", name).strip()
    marks = list(_RELEASEISH.finditer(c))
    first = next((m for m in marks if not _YEAR.match(m.group())), None)
    if first is None:
        return None
    title = c[:first.start()].strip()
    if re.fullmatch(r"s\d{1,3}(?:e\d{1,4})?", first.group(), re.I):
        title += " " + first.group()
    return title if title and loose(title) != loose(name) else None


def title_year(name: str) -> str | None:
    """A film's title and year without what follows the year before the resolution - an
    edition or a cut ("Film 1985 Directors Cut 1080p ..." is "Film 1985"), which indexers
    often write differently or not at all. None when that is no shorter than short_query."""
    c = re.sub(r"[._]+", " ", bare(name)).strip()
    m = next((m for m in re.finditer(r"(?<![0-9])(?:19|20)\d{2}(?![0-9])", c) if re.search(r"[^\W_]", c[:m.start()])), None)
    if not m or _SE.search(c):
        return None
    out = c[:m.end()].strip()
    return out if loose(out) != loose(short_query(name) or "") else None


def _same(a: str, b: str) -> bool:
    return matching.norm(a) == matching.norm(b) or loose(a) == loose(b)


_SE = re.compile(r"(?<![a-z0-9])s(\d{1,3})(?:[\s._-]*e(\d{1,4}))?(?![a-z0-9])", re.I)
_RES = re.compile(r"(?<![a-z0-9])(2160|1080|720|576|480)[pi](?![a-z0-9])", re.I)
_GROUP = re.compile(r"-([A-Za-z0-9@]{2,})$")


def _codec(name: str) -> str | None:
    if re.search(r"(?<![a-z0-9])(?:[xh][\s.]?265|hevc)(?![0-9])", name, re.I):
        return "265"
    if re.search(r"(?<![a-z0-9])(?:[xh][\s.]?264|avc)(?![0-9])", name, re.I):
        return "264"
    return None


def _kind(name: str) -> str | None:
    if re.search(r"(?<![a-z])remux(?![a-z])", name, re.I):
        return "remux"
    if re.search(r"(?<![a-z])web(?:-?dl|-?rip)?(?![a-z])", name, re.I):
        return "web"
    if re.search(r"(?<![a-z])blu-?ray(?![a-z])", name, re.I):
        # a disc as it is ("Blu-ray AVC", "UHD Blu-ray HEVC") names the disc's own video
        # format; an encode of it names its encoder ("x264") instead
        if re.search(r"(?<![a-z0-9])(?:avc|vc-?1|mpeg-?2|hevc)(?![a-z0-9])", name, re.I) and \
                not re.search(r"(?<![a-z0-9])(?:[xh][\s.]?26[45])(?![0-9])", name, re.I):
            return "disc"
        return "bluray"
    return None


def _audio(name: str) -> str | None:
    """The main audio as named, one name each: "DD+ 5.1" and "DDP5.1" are the same."""
    n = name.lower()
    for key, rx in (("truehd", r"truehd"), ("dtshdma", r"dts[\s.-]?hd[\s.-]?ma"), ("dtsx", r"dts[\s.-]?x(?![a-z0-9])"),
                    ("pcm", r"l?pcm"), ("flac", r"flac"), ("ddp", r"dd\+|ddp|e-?ac-?3"),
                    ("dts", r"dts"), ("dd", r"(?<![a-z])dd(?![a-z+])|ac-?3"), ("aac", r"aac"), ("opus", r"opus")):
        if re.search(rx, n):
            return key
    return None


def _hdr(name: str) -> frozenset:
    """Dolby Vision and HDR10/HDR10+ as named: "DV HDR10+", "DoVi.HDR10Plus"."""
    n = name.lower().replace("hdr10plus", "hdr10+").replace("hdr10p", "hdr10+")
    out = set()
    if re.search(r"(?<![a-z0-9])(?:dv|dovi|dolby[\s.]?vision)(?![a-z0-9])", n):
        out.add("dv")
    if "hdr10+" in n:
        out.add("hdr10+")
    elif re.search(r"(?<![a-z0-9])hdr(?:10)?(?![a-z0-9+])", n):
        out.add("hdr")
    return frozenset(out)


# streaming services as release names tag them, each under one name
_SERVICES = {"amzn": "amzn", "nf": "nf", "atvp": "atvp", "atv": "atvp", "hmax": "max", "max": "max",
             "dsnp": "dsnp", "hulu": "hulu", "pcok": "pcok", "pmtp": "pmtp", "stan": "stan",
             "it": "itunes", "itunes": "itunes", "ma": "ma", "cr": "cr", "sho": "sho", "roku": "roku",
             "crav": "crav", "nlz": "nlz", "dscp": "dscp", "bbc": "bbc", "red": "red"}


def _service(name: str) -> str | None:
    """The streaming service a web release names - only in front of WEB-DL/WEBRip, where it
    sits ("NF WEB-DL"), so "MA" in "DTS-HD MA" is not read as one."""
    m = re.search(r"(?<![a-z0-9])([a-z]{2,6})[\s.](?:web(?:-?dl|-?rip)?)(?![a-z])", name, re.I)
    return _SERVICES.get(m.group(1).lower()) if m else None


def facets(name: str) -> dict:
    """What makes a release that release, however a tracker writes its name: title, year,
    season and episode, resolution, source, codec and group."""
    c = re.sub(r"[._]+", " ", bare(name)).strip()
    marks = list(_RELEASEISH.finditer(c))
    first = next((m for m in marks if not _YEAR.match(m.group())), None)
    head = c[:first.start()] if first else c
    year = re.findall(r"(?<![0-9])((?:19|20)\d{2})(?![0-9])", head)
    title = re.sub(r"(?<![0-9])(?:19|20)\d{2}(?![0-9])|[()\[\]]", " ", head).replace("&", " and ")
    se = _SE.search(c)
    res = _RES.search(c)
    grp = _GROUP.search(c.strip())
    if grp is None and re.search(r"\s-\s+\S+$", c):
        # "... BluRay DTS x264 - GrE": the scene dash spaced out (pipeline's reading of it)
        from .pipeline import group_of
        g = group_of(c)
        grp = re.search(r"-\s+(" + re.escape(g) + r")$", c, re.I) if g else None
    return {"title": loose(title), "year": year[-1] if year else None,
            "season": int(se.group(1)) if se else None,
            "episode": int(se.group(2)) if se and se.group(2) else None,
            "res": res.group(1) if res else None, "kind": _kind(c), "codec": _codec(c), "hdr": _hdr(c), "service": _service(c), "audio": _audio(c),
            "group": grp.group(1).lower() if grp and grp.group(1).lower() not in _NOT_GROUP
            and grp.group(1).lower() not in rule("no_group_words") else None}


def alike(post: str, name: str, episode_of_pack: bool = False) -> bool:
    """Is ``post`` the release ``name`` is, written another way - as a tracker that renames
    releases in its own style ("DD+ 5.1", "iT", spaces) shows it? The same title, year or
    season, resolution, source, codec and group (pipeline.group_match: a group clear in one
    name is looked for in the other; with no group to go by on either, the source, codec
    and audio must be named alike). ``episode_of_pack``: ``post`` may be one episode of the
    season ``name`` is."""
    from .pipeline import group_match, group_of
    a, b = facets(post), facets(name)
    no_group = {w.lower() for w in rule("no_group_words")}
    same_group = group_match(bare(name), post, ignore=no_group)
    if same_group is False or not b["res"] or a["res"] != b["res"]:
        return False
    if same_group is None and (group_of(post) or "") not in ("", *no_group):
        return False            # a release with no group is never a post that names one
    if same_group is None:
        # a release with no group to go by: the source and codec must be named alike on
        # both, and the service of a web release ("Show 1080p" alone says too little)
        if not (a["kind"] and a["kind"] == b["kind"] and a["codec"] and a["codec"] == b["codec"]):
            return False
        if b["kind"] == "web" and a["service"] != b["service"]:
            return False
        if not (a["audio"] and a["audio"] == b["audio"]):
            return False
    if a["title"] != b["title"] or a["season"] != b["season"]:
        return False
    if episode_of_pack:
        if b["episode"] is not None or a["episode"] is None:
            return False
    elif a["episode"] != b["episode"]:
        return False
    agree = rule("must_agree")
    if "hdr" in agree and a["hdr"] != b["hdr"]:   # HDR10+ or not is another file, even where one name says nothing
        return False
    for k, f in (("year", "year"), ("source", "kind"), ("codec", "codec"), ("service", "service")):
        if k in agree and a[f] and b[f] and a[f] != b[f]:
            return False
    return True


# How a list goes about its names (the tab's advanced options). "normal": built from Usenet
# only - the safe way, nothing comes over BitTorrent. The other two download from the tracker
# over BitTorrent, which uses its ratio and brings hit-and-run obligations there: "fallback"
# for names Usenet cannot build, "torrent" for every name, Usenet not searched at all.
MODES = ("normal", "fallback", "torrent")


def check(cfg: Config, pr: Prowlarr, name: str, tracker: str, usenet_ids: list[int],
          mode: str = "normal") -> dict:
    """check_usenet(), and for a list set to download over BitTorrent the torrent to download:
    "download" is then True on a name to be downloaded whole from ``tracker`` instead of built."""
    tracker = (tracker or "").strip()
    if mode not in MODES[1:] or not tracker:
        return check_usenet(cfg, pr, name, tracker, usenet_ids)
    if mode == "torrent":
        out = {"name": name, "torrent": None, "others": [], "nzbs": [], "plausible": False, "on_usenet": False}
        same, torrents = on_tracker(cfg, pr, bare(name), tracker)
        if not same:
            out["others"] = [t.title for t in torrents[:8]]
            out["verdict"] = f"not found on {tracker} by that name" + (
                f" ({len(torrents)} other result{'s' if len(torrents) != 1 else ''})" if torrents else "")
            return out
        out["torrent"] = dataclasses.asdict(same[0])
        return _by_bittorrent(out, tracker, "Usenet not searched")
    out = check_usenet(cfg, pr, name, tracker, usenet_ids)
    if out["plausible"]:
        return out
    if out["torrent"] is None and not out["on_usenet"]:      # the tracker was not asked yet
        same, torrents = on_tracker(cfg, pr, bare(name), tracker)
        if not same:
            out["others"] = [t.title for t in torrents[:8]]
            out["verdict"] = f"no Usenet post of this release, and not found on {tracker} by that name"
            return out
        out["torrent"] = dataclasses.asdict(same[0])
    if out["torrent"] is None:
        return out
    return _by_bittorrent(out, tracker, out["verdict"])


def _by_bittorrent(out: dict, tracker: str, why: str) -> dict:
    """A torrent to download whole over BitTorrent - unless nobody seeds it."""
    if out["torrent"].get("seeders") == 0:
        out["verdict"] = f"{why} - on {tracker}, but with no seeders, so it cannot be downloaded"
        return out
    out["download"] = True
    out["verdict"] = f"{why} - to download from {tracker} over BitTorrent"
    return out


def on_tracker(cfg: Config, pr: Prowlarr, name: str, tracker: str) -> tuple[list[Release], list[Release]]:
    """The torrents on ``tracker`` of this release, most seeded first, and all it found."""
    ids = [i for i, n in pr.indexers() if n and n.strip().lower() == tracker.lower()]
    if not ids:
        raise ValueError(f"{tracker} is not an enabled torrent indexer in Prowlarr")
    tracker_search = lambda q: [r for r in pr.search(q, ids, cfg.categories) if r.protocol == "torrent"]
    is_it = lambda t: _same(t.title, name) or alike(t.title, name)
    torrents = tracker_search(name)
    for q in (short_query(name), title_year(name) if rule("title_year_search") else None):   # by its title
        if q and not any(is_it(t) for t in torrents):
            torrents = tracker_search(q) or torrents
    return sorted((t for t in torrents if is_it(t)), key=lambda t: -(t.seeders or 0)), torrents


def check_usenet(cfg: Config, pr: Prowlarr, name: str, tracker: str, usenet_ids: list[int]) -> dict:
    """What Usenet has for one pasted name, and the torrent on ``tracker`` it would build.

    Usenet is asked first: the tracker is only searched for names Usenet has a post of, so
    a long list costs the tracker a search per plausible name, not one per name.

    With no ``tracker`` only Usenet is searched: "on_usenet" says whether it has the release,
    and nothing is plausible, as there is no torrent to build.

    Returns {"name", "torrent" (a Release as a dict, or None), "others" (titles the tracker
    found instead), "nzbs" (the posts a build should try), "plausible", "on_usenet",
    "verdict" (one line)}.
    """
    tracker = (tracker or "").strip()
    ids = [i for i, n in pr.indexers() if n and n.strip().lower() == tracker.lower()] if tracker else []
    if tracker and not ids:
        raise ValueError(f"{tracker} is not an enabled torrent indexer in Prowlarr")
    out = {"name": name, "torrent": None, "others": [], "nzbs": [], "plausible": False, "on_usenet": False}
    name = bare(name)              # a list checked before the uploader was taken off
    usenet = lambda q: [r for r in pr.search(q, usenet_ids, cfg.categories) if r.protocol == "usenet"]
    nzbs = usenet(name)
    exact = matching.rank_nzbs([n for n in nzbs if _same(n.title, name)])
    eps: list[Release] = []
    query = None if exact else matching.episode_query(matching.norm(name))
    if query:
        eps = matching.episode_matches(Release(name, "torrent", "", 0, 0, "", "", "", "", None, None, None),
                                       usenet(query) + nzbs)
    renamed = False
    groupless = facets(name)["group"] is None
    if not exact and not eps and rule("renamed_match") and (short_query(name) or title_year(name)):
        # a tracker that writes release names its own way ("DD+ 5.1", "iT", spaces) is
        # matched by what the release is, from a search by its title - and by its title
        # and year alone, as an edition after the year is written in many ways
        # first by title, year, season/episode, resolution and group: the posts of just that
        # release, which a search by title alone can bury under a thousand others
        from .pipeline import usenet_queries
        f = facets(name)
        parts = (usenet_queries(name, f["group"], f["res"] + "p")[0][1:]
                 if f["group"] and f["res"] else [])
        found = nzbs + [r for q in dict.fromkeys([*parts, short_query(name),
                                                   title_year(name) if rule("title_year_search") else None])
                        if q for r in usenet(q)]
        exact = matching.rank_nzbs([n for n in found if alike(n.title, name)])
        if not exact:
            best: dict[int, Release] = {}
            for n in found:
                if alike(n.title, name, episode_of_pack=True):
                    e = facets(n.title)["episode"]
                    if e not in best or (n.grabs or 0) > (best[e].grabs or 0):
                        best[e] = n
            eps = [best[e] for e in sorted(best)]
        renamed = bool(exact or eps)
    o = None if exact or eps or not rule("renamed_match") else other_facets(name)
    if o and loose(o["title"]) != loose(name):
        # a game or program the tracker lists with its version, year and tags, which its
        # group posts as just "Title-GRP": looked for by its title, matched by title and group
        exact = matching.rank_nzbs([n for n in nzbs + usenet(o["title"]) if other_alike(n.title, o)])
        renamed = bool(exact)
        groupless = False                  # matched by its group: none of the groupless caveats
    if not exact and not eps:
        out["verdict"] = "no Usenet post of this release" + (" - the tracker was not asked" if tracker else "")
        return out
    out["on_usenet"] = True
    if not tracker:
        posts = exact or eps
        where = ", ".join(sorted({n.indexer for n in posts}))
        out["nzbs"] = [dataclasses.asdict(n) for n in posts]
        maybe = renamed and groupless
        out["verdict"] = ((f"{'possibly the' if maybe else 'same'} release on Usenet ({len(exact)} post{'s' if len(exact) != 1 else ''}: {where}, "
                           f"largest {max(n.size for n in exact) / GIB:.2f} GB)" if exact else
                           f"season pack: {len(eps)} episode post{'s' if len(eps) != 1 else ''} of the same release "
                           f"on Usenet ({where}, {sum(n.size for n in eps) / GIB:.2f} GB in all)")
                          + (f" - named {posts[0].title} there" if renamed else "")
                          + (" - neither names a release group, so only the torrent's size could tell" if maybe else "")
                          + " - no tracker chosen, so no torrent was looked for")
        return out
    same, torrents = on_tracker(cfg, pr, name, tracker)
    if not same:
        out["others"] = [t.title for t in torrents[:8]]
        out["verdict"] = f"on Usenet, but not found on {tracker} by that name" + (
            f" ({len(torrents)} other result{'s' if len(torrents) != 1 else ''})" if torrents else "")
        return out
    t = same[0]
    out["torrent"] = dataclasses.asdict(t)
    if exact:
        # far larger than the torrent is another release by that name (the full game for a
        # crack fix, a remux for an encode): never downloaded to build this one
        huge = [n for n in exact if too_large(n.size, t.size)]
        exact = [n for n in exact if n not in huge]
        if huge and not exact:
            out["verdict"] = (f"a post by that name is on Usenet, but {huge[0].size / max(1, t.size):.0f}× the "
                              f"torrent's size ({gb_text(huge[0].size)} for {gb_text(t.size)}) - another release, "
                              "not downloaded")
            return out
    if exact:
        # with no group to go by, a post must be the torrent's size, not merely hold it
        big = [n for n in exact if not t.size or (n.size >= t.size * ENOUGH
                                                  and not (renamed and groupless and n.size > t.size * (1 + rule("groupless_max_pct") / 100)))]
        if big:
            out.update(nzbs=[dataclasses.asdict(n) for n in big], plausible=True,
                       verdict=f"same release on Usenet ({len(big)} post{'s' if len(big) != 1 else ''}: "
                               + ", ".join(sorted({n.indexer for n in big})) + ")"
                               + (f" - named {big[0].title} there" if renamed else ""))
        else:
            out["verdict"] = "same release on Usenet, but smaller than the torrent - files would be missing"
        return out
    total = sum(n.size for n in eps)
    if too_large(total, t.size):
        out["verdict"] = (f"episode posts of that name are on Usenet, but {gb_text(total)} in all for a "
                          f"{gb_text(t.size)} torrent - another release, not downloaded")
        return out
    if not t.size or total >= t.size * ENOUGH:
        out.update(nzbs=[dataclasses.asdict(n) for n in eps], plausible=True,
                   verdict=f"season pack: {len(eps)} episode posts of the same release on Usenet"
                           + (f" - named like {eps[0].title} there" if renamed else ""))
    else:
        out["verdict"] = (f"season pack: only {len(eps)} episode post{'s' if len(eps) != 1 else ''} "
                          "of the same release on Usenet - not the whole season")
    return out


def too_large(post: int, torrent: int) -> bool:
    """Is a post far larger than the torrent it would build - another release by that name?
    The same rule a build holds every post to (pipeline.size_verdict)."""
    from .pipeline import FAR_LARGER, FAR_LARGER_MIN
    return bool(post and torrent) and post > torrent * FAR_LARGER and post - torrent > FAR_LARGER_MIN


def gb_text(n: int) -> str:
    return f"{n / 1024 ** 3:.2f} GB" if n >= 1024 ** 3 else f"{n / 1024 ** 2:.1f} MB"


# ---------------------------------------------------------------- the queue

KEEP_LISTS = 20              # finished lists kept on the page, newest first
REUSE_NONE_FOR = 5 * 24 * 3600   # a name another list found no Usenet post of this recently is not searched again


def _age(s: float) -> str:
    return f"{max(1, round(s / 3600))} h" if s < 48 * 3600 else f"{round(s / 86400)} days"


class Queue:
    """Pasted lists, checked one after another on this machine.

    Each list is {"id", "tracker", "auto", "options", "mode", "added", "state", "rows"} (``mode``:
    one of MODES - how it goes about its names; "normal" when missing); its state is
    "queued", "checking", "done" or "stopped". Each row is {"name", "state", "result"?,
    "note"?, "job"?, "listed"?}, its state "waiting", "checking", "done", "error" or
    "skipped" (its list was stopped first). One name is checked at a time, whichever list
    it is in - a tracker limits how fast it is asked - and lists are taken oldest first.
    Kept in tracklist.json, so a restart carries on where it was.
    """

    def __init__(self, app, path: str, start: bool = True):
        self.app, self.path, self.lock = app, path, threading.RLock()
        try:
            with open(path, encoding="utf-8") as fh:
                self.lists: list[dict] = json.load(fh).get("lists", [])
        except (OSError, ValueError):
            self.lists = []
        for lst in self.lists:                   # cut short by a restart: carried on
            for r in lst["rows"]:
                if r["state"] == "checking":
                    r["state"] = "waiting"
        self.next_id = max((l["id"] for l in self.lists), default=0) + 1
        # the Advanced options: how names are read and matched, per tracker
        self.rules = RulesStore(os.path.join(os.path.dirname(os.path.abspath(path)), "tracklist_rules.json"))
        self.on_save = None
        self.wake = threading.Event()
        if start:
            threading.Thread(target=self.work, name="tracklist", daemon=True).start()

    def save(self):
        with self.lock:
            tmp = f"{self.path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"lists": self.lists}, fh)
            os.replace(tmp, self.path)
        if self.on_save:
            try:
                self.on_save()
            except Exception:                    # noqa: BLE001 - telling the page never stops the queue
                pass

    def snapshot(self) -> list[dict]:
        with self.lock:
            return copy.deepcopy(self.lists)

    def add(self, names: list[str], tracker: str, auto: bool, options: dict, mode: str = "normal") -> int:
        """A list to check. With no tracker it is searched on Usenet only, and never builds -
        nor downloads anything over BitTorrent, whatever ``mode`` says."""
        names = [n.strip() for n in names if n and n.strip()]
        if not names:
            raise ValueError("a list of names is needed")
        tracker = (tracker or "").strip()
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}")
        with self.lock:
            lst = {"id": self.next_id, "tracker": tracker, "auto": bool(auto) and bool(tracker),
                   "options": {**(options or {}), "pp": "auto"}, "mode": mode if tracker else "normal",
                   "added": time.time(), "state": "queued",
                   "rows": [{"name": n, "state": "waiting"} for n in dict.fromkeys(names)]}
            self.next_id += 1
            self.lists.insert(0, lst)
            ended = [l for l in self.lists if l["state"] in ("done", "stopped")]
            for old in ended[KEEP_LISTS:]:
                self.lists.remove(old)
        self.save()
        self.wake.set()
        return lst["id"]

    def _find(self, list_id) -> dict:
        lst = next((l for l in self.lists if l["id"] == int(list_id or 0)), None)
        if lst is None:
            raise ValueError("no such list")
        return lst

    def stop(self, list_id):
        with self.lock:
            lst = self._find(list_id)
            if lst["state"] in ("queued", "checking"):
                lst["state"] = "stopped"
                for r in lst["rows"]:
                    if r["state"] == "waiting":
                        r["state"] = "skipped"
        self.save()

    def remove(self, list_id):
        with self.lock:
            lst = self._find(list_id)
            if lst["state"] in ("queued", "checking"):
                raise ValueError("stop it first")
            self.lists.remove(lst)
        self.save()

    def build(self, list_id, names: list[str]) -> dict:
        """Start the builds of these plausible names of a list (Build ticked)."""
        with self.lock:
            lst = self._find(list_id)
            rows = [r for r in lst["rows"] if r["name"] in set(names)]
        started = sum(bool(self._build(lst, r)) for r in rows)
        self.save()
        return {"started": started}

    def recheck(self, list_id, name: str):
        """Search Usenet for a name again now - one whose "nothing on Usenet" came from an
        earlier list's search (Search again)."""
        with self.lock:
            lst = self._find(list_id)
            r = next((r for r in lst["rows"] if r["name"] == name), None)
            if r is None:
                raise ValueError("no such name in that list")
            if r["state"] in ("waiting", "checking"):
                return
            for k in ("result", "note", "listed"):
                r.pop(k, None)
            r.update(state="waiting", fresh=True)
            if lst["state"] not in ("queued", "checking"):
                lst["state"] = "queued"
        self.save()
        self.wake.set()

    def _build(self, lst: dict, r: dict):
        res = r.get("result") or {}
        if not (res.get("plausible") or res.get("download")) or r.get("job"):
            return None
        try:
            if res.get("plausible"):
                job = self.app.build_from_list(res["torrent"], res.get("nzbs") or [], lst["options"])
            else:                                # chosen for the list: downloaded over BitTorrent
                job = self.app.download_from_list(res["torrent"], lst["tracker"])
        except ValueError as e:                  # already on the jobs list, or being built
            r["note"] = str(e)
            return None
        r["job"] = job.id
        r.pop("note", None)
        return job

    def _next(self):
        with self.lock:
            for lst in reversed(self.lists):     # oldest first
                if lst["state"] not in ("queued", "checking"):
                    continue
                row = next((r for r in lst["rows"] if r["state"] == "waiting"), None)
                if row is None:
                    lst["state"] = "done"
                    continue
                lst["state"], row["state"] = "checking", "checking"
                return lst, row
        return None, None

    def _none_lately(self, lst: dict, name: str, working: set[int] | None) -> tuple[int, dict] | None:
        """(the list, when) that searched Usenet for ``name`` in the last 5 days and found no
        post of it - under the same rules as ``lst`` (they decide what counts as a post of
        it). Only a list that builds from Usenet alone takes it: one that would download
        from the tracker instead still asks the tracker. A result taken this way keeps the
        time of the search, so it is never carried on past 5 days. ``working``: the Usenet
        indexers that would be searched now - a search that went without one of them (it
        was off, or Prowlarr had set it aside after failures) is not taken: it is searched
        again, as is one that does not say which it went to."""
        if (lst.get("mode") or "normal") != "normal" or not working:
            return None
        key, rules = job_key(name), self.rules.rules(lst["tracker"])
        now = time.time()
        with self.lock:
            for other in self.lists:
                if other is lst:
                    continue
                for r in other["rows"]:
                    res = r.get("result") or {}
                    if r.get("state") != "done" or job_key(r["name"]) != key:
                        continue
                    if res.get("on_usenet") is not False or res.get("torrent") or \
                            not str(res.get("verdict", "")).startswith("no Usenet post"):
                        continue
                    when = res.get("searched") or 0
                    if not (working <= set(res.get("indexers") or ())):
                        continue
                    if now - when < REUSE_NONE_FOR and self.rules.rules(other["tracker"]) == rules:
                        return other["id"], res
        return None

    def step(self) -> bool:
        """Check the next waiting name; False when there is none (or nzb2seed is paused)."""
        from .pipeline import usenet_indexers
        from .slots import PAUSED
        if PAUSED.is_set():
            return False
        lst, row = self._next()
        self.save()
        if lst is None:
            return False
        before = self.app.listed(row["name"])
        if before:                               # on the jobs list already: not worth a search
            with self.lock:
                row.update(state="done", listed=before.id,
                           result={"name": row["name"], "torrent": None, "others": [], "nzbs": [],
                                   "plausible": False, "verdict": "not searched"})
            self.save()
            return True
        fresh = row.pop("fresh", False)
        cfg = self.app.cfg
        pr = Prowlarr(cfg.prowlarr_url, cfg.prowlarr_key)
        mode = lst.get("mode") or "normal"
        working = None
        if mode != "torrent":
            try:                                 # what a search would go to now
                working = set(usenet_indexers(cfg, pr)) - pr.paused_ids()
            except Exception:                    # noqa: BLE001 - not known: nothing is taken as searched
                working = None
        gone = None if fresh else self._none_lately(lst, row["name"], working)
        if gone:                                 # another list found nothing on Usenet just now
            prev, was = gone
            when = was["searched"]
            with self.lock:
                row.update(state="done", result={
                    "name": row["name"], "torrent": None, "others": [], "nzbs": [], "plausible": False,
                    "on_usenet": False, "searched": when, "reused": prev, "indexers": was.get("indexers"),
                    "verdict": f"no Usenet post of this release - as list {prev} found "
                               f"{_age(time.time() - when)} ago, not searched again (Search again asks Usenet now)"
                               + (" - the tracker was not asked" if lst["tracker"] else "")})
            self.save()
            return True
        try:
            with using(self.rules.rules(lst["tracker"])):
                ids = usenet_indexers(cfg, pr) if mode != "torrent" else []
                result = check(cfg, pr, row["name"], lst["tracker"], ids, mode)
            listed = self.app.listed(result["torrent"]["title"]) if result.get("torrent") else None
            if mode != "torrent":
                result["searched"] = time.time()  # when Usenet was searched for it, and where
                if working is not None:
                    result["indexers"] = sorted(working & set(ids))
            with self.lock:
                row.update(state="done", result=result)
                if listed:
                    row["listed"] = listed.id
            if not listed and lst["auto"] and (result.get("plausible") or result.get("download")):
                self._build(lst, row)
        except Exception as e:                   # noqa: BLE001 - one name never stops the list
            with self.lock:
                row.update(state="error", note=str(e) or type(e).__name__)
        self.save()
        return True

    def work(self):
        while True:
            try:
                busy = self.step()
            except Exception as e:               # noqa: BLE001 - the queue keeps going
                print(f"tracker list: {e}", flush=True)
                busy = False
            if not busy:
                self.wake.wait(60)
                self.wake.clear()
