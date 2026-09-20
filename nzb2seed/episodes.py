"""Which episodes a show has: TVmaze, TMDB, TheTVDB or IMDb - or all of them.

TVmaze has an API; TMDB and TheTVDB are read from their public web pages (no API key),
through the same outbound proxy as every other lookup. IMDb comes from its official
episode dataset (its web pages answer automated requests with a bot check, which
nzb2seed does not try to get around). "All sources" takes, per season, the list of whichever
source knows the most episodes - TVmaze's lists are often incomplete for small shows.

Everything read is cached on disk for a day (``cache/metadata`` next to the config), so
planning a series twice does not ask the sites twice. Settings has a button to clear it.
"""
from __future__ import annotations

import gzip
import html
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request

from . import matching, metadata

SOURCES = {"all": "All sources", "tvmaze": "TVmaze", "tmdb": "TMDB", "tvdb": "TheTVDB", "imdb": "IMDb"}
ORDER = ["tvmaze", "tmdb", "tvdb", "imdb"]          # preferred on a tie
TMDB = "https://www.themoviedb.org"
TVDB = "https://thetvdb.com"
IMDB_DATASET = "https://datasets.imdbws.com/title.episode.tsv.gz"
IMDB_DATASET_DAYS = 7
TAB = chr(9)
CACHE_HOURS = 24

CACHE_DIR: str | None = None


class Unavailable(Exception):
    """A source that cannot answer for this show (not listed there, blocked, down)."""


# ---------------------------------------------------------------- cache

def configure_cache(config_path: str):
    global CACHE_DIR
    CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(config_path)), "cache", "metadata")


def _cache_file(key: str) -> str | None:
    if not CACHE_DIR:
        return None
    return os.path.join(CACHE_DIR, re.sub(r"[^a-z0-9._-]", "_", key.lower()) + ".json")


def cached(key: str, fn):
    """``fn()``'s value, from the disk cache while it is younger than CACHE_HOURS.
    Failures (Unavailable) are cached too, so a blocked site is not asked every time."""
    path = _cache_file(key)
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
            if time.time() - d["at"] < CACHE_HOURS * 3600:
                if "unavailable" in d:
                    raise Unavailable(d["unavailable"])
                return d["value"]
        except (OSError, ValueError, KeyError):
            pass
    try:
        value, entry = fn(), None
        entry = {"at": time.time(), "value": value}
    except Unavailable as e:
        value, entry = e, {"at": time.time(), "unavailable": str(e)}
    if path:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(entry, fh)
        os.replace(tmp, path)
    if isinstance(value, Unavailable):
        raise value
    return value


def clear_cache() -> int:
    """Remove everything cached; returns how many entries there were."""
    if not CACHE_DIR or not os.path.isdir(CACHE_DIR):
        return 0
    n = sum(1 for x in os.listdir(CACHE_DIR) if x.endswith(".json"))
    shutil.rmtree(CACHE_DIR, ignore_errors=True)
    return n


def _page(url: str) -> str:
    text, _, why = metadata.get_text(url)
    if text is None:
        raise Unavailable(why or f"{url} not found")
    return text


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def _eps(pairs) -> dict[int, list[dict]]:
    """{season: [{"number", "name", "airdate"}]} from (season, number, name, airdate)."""
    out: dict[int, dict[int, dict]] = {}
    for s, n, name, date in pairs:
        if s > 0 and n > 0:
            out.setdefault(s, {}).setdefault(n, {"number": n, "name": name or "", "airdate": date or ""})
    return {s: [eps[n] for n in sorted(eps)] for s, eps in sorted(out.items())}


# ---------------------------------------------------------------- the sources

def tvmaze(show: dict) -> dict[int, list[dict]]:
    def get():
        out = {}
        for s in metadata.tvmaze_seasons(show["id"]):
            eps = metadata.tvmaze_episodes(show["id"], s["number"])
            if eps:
                out[s["number"]] = eps
        return out
    return {int(k): v for k, v in cached(f"tvmaze-{show['id']}", get).items()}


def tmdb_id(show: dict) -> int:
    """TMDB's id for the show: its TV search, matched on the premiere year and the name."""
    def get():
        page = _page(f"{TMDB}/search/tv?query={urllib.parse.quote(show['name'])}")
        year = (show.get("premiered") or "")[:4]
        want = matching.norm(show["name"]).replace("&", "")
        best = None
        for m in re.finditer(r'href="/tv/(\d+)-([a-z0-9-]+)"', page):
            chunk = page[m.end():m.end() + 3000]
            d = re.search(r'release_date[^>]*>\s*([^<]+)<', chunk)
            y = re.search(r"(?:19|20)\d\d", d.group(1)).group(0) if d and re.search(r"(?:19|20)\d\d", d.group(1)) else ""
            slug_ok = m.group(2).replace("-", ".").startswith(re.sub(r"\.+", ".", want).strip("."))
            if year and y == year and slug_ok:
                return int(m.group(1))
            if best is None and year and y == year:
                best = int(m.group(1))
        if best is None:
            raise Unavailable(f"TMDB has no show {show['name']!r} from {year or 'that year'}")
        return best
    return cached(f"tmdb-id-{show['id']}", get)


def tmdb(show: dict) -> dict[int, list[dict]]:
    def get():
        tid = tmdb_id(show)
        seasons = sorted({int(n) for n in re.findall(rf'/tv/{tid}[^"]*/season/(\d+)"', _page(f"{TMDB}/tv/{tid}/seasons"))})
        pairs = []
        for s in seasons:
            page = _page(f"{TMDB}/tv/{tid}/season/{s}")
            for m in re.finditer(r'<h3><a[^>]*data-episode-number="(\d+)" data-season-number="(\d+)"[^>]*>(.*?)</a>', page, re.S):
                pairs.append((int(m.group(2)), int(m.group(1)), _text(m.group(3)), ""))
        return _eps(pairs)
    return {int(k): v for k, v in cached(f"tmdb-{show['id']}", get).items()}


def tvdb_slug(show: dict) -> str:
    def get():
        if not show.get("tvdb"):
            raise Unavailable("TVmaze does not know the show's TheTVDB id")
        page = _page(f"{TVDB}/dereferrer/series/{show['tvdb']}")
        slugs = [s for s in re.findall(r'/series/([a-z][a-z0-9-]*)["/]', page) if s not in ("create",)]
        if not slugs:
            raise Unavailable("TheTVDB did not name the show's page")
        return max(set(slugs), key=slugs.count)
    return cached(f"tvdb-slug-{show['id']}", get)


def tvdb(show: dict) -> dict[int, list[dict]]:
    def get():
        page = _page(f"{TVDB}/series/{tvdb_slug(show)}/allseasons/official")
        pairs = []
        # the air date follows the name, before the next episode's label
        for m in re.finditer(r'episode-label">\s*S(\d+)E(\d+)\s*</span>\s*<a[^>]*>(.*?)</a>'
                             r'((?:(?!episode-label).){0,600})', page, re.S):
            date = re.search(r"<li>\s*([A-Z][a-z]+ \d{1,2}, \d{4})\s*</li>", m.group(4))
            airdate = ""
            if date:
                try:
                    airdate = time.strftime("%Y-%m-%d", time.strptime(date.group(1), "%B %d, %Y"))
                except ValueError:
                    pass
            pairs.append((int(m.group(1)), int(m.group(2)), _text(m.group(3)), airdate))
        if not pairs:
            raise Unavailable("TheTVDB lists no episodes for the show")
        return _eps(pairs)
    return {int(k): v for k, v in cached(f"tvdb-{show['id']}", get).items()}


def _imdb_dataset() -> str:
    """IMDb's official episode dataset (title.episode.tsv.gz: every episode's parent show,
    season and number - free for personal use), downloaded through the proxy and kept for
    a week. IMDb's web pages are not read: they answer automated requests with a bot check."""
    if not CACHE_DIR:
        raise Unavailable("no cache folder for IMDb's dataset")
    path = os.path.join(CACHE_DIR, "imdb-title.episode.tsv.gz")
    if os.path.isfile(path) and time.time() - os.path.getmtime(path) < IMDB_DATASET_DAYS * 86400:
        return path
    os.makedirs(CACHE_DIR, exist_ok=True)
    part = path + ".part"
    try:
        req = urllib.request.Request(IMDB_DATASET, headers={"User-Agent": metadata.UA})
        with metadata._urlopen(req, timeout=300) as r, open(part, "wb") as fh:
            shutil.copyfileobj(r, fh, 1 << 20)
        os.replace(part, path)
    except (OSError, urllib.error.URLError) as e:
        if os.path.exists(part):
            os.remove(part)
        if os.path.isfile(path):
            return path                      # an older copy is better than none
        raise Unavailable(f"IMDb's episode dataset could not be downloaded ({e})")
    return path


def imdb(show: dict) -> dict[int, list[dict]]:
    def get():
        tt = show.get("imdb")
        if not tt:
            raise Unavailable("TVmaze does not know the show's IMDb id")
        pairs = []
        with gzip.open(_imdb_dataset(), "rt", encoding="utf-8") as fh:
            key = TAB + tt + TAB
            for line in fh:
                if key in line:
                    _, parent, season, number = line.rstrip().split(TAB)
                    if parent == tt and season.isdigit() and number.isdigit():
                        pairs.append((int(season), int(number), "", ""))
        if not pairs:
            raise Unavailable("IMDb lists no numbered episodes for the show")
        return _eps(pairs)
    return {int(k): v for k, v in cached(f"imdb-{show['id']}", get).items()}


READERS = {"tvmaze": tvmaze, "tmdb": tmdb, "tvdb": tvdb, "imdb": imdb}


# ---------------------------------------------------------------- the list to use

def episode_lists(show: dict, source: str = "all", log=None) -> tuple[dict[int, list[dict]], dict[int, str]]:
    """({season: episodes}, {season: source used}). With "all", each season comes from the
    source that lists the most episodes for it. ``log(text)`` hears about sources that
    could not answer."""
    names = ORDER if source in ("all", "", None) else [source]
    got: dict[str, dict] = {}
    for name in names:
        if name not in READERS:
            raise ValueError(f"unknown episode source {name!r} (use one of: {', '.join(SOURCES)})")
        try:
            got[name] = READERS[name](show)
        except Unavailable as e:
            if log:
                log(f"{SOURCES[name]}: {e}")
        except Exception as e:                      # a site changing its pages must not stop a grab
            if log:
                log(f"{SOURCES[name]}: could not be read ({type(e).__name__}: {e})")
    lists, used = {}, {}
    for s in sorted({s for v in got.values() for s in v}):
        # the most episodes; on a tie the list with air dates, then the source order
        best = max((n for n in got if got[n].get(s)),
                   key=lambda n: (len(got[n][s]), sum(1 for e in got[n][s] if e.get("airdate")), -ORDER.index(n)))
        lists[s], used[s] = got[best][s], best
    return lists, used


def all_names(show: dict) -> dict[tuple[int, int], set[str]]:
    """{(season, episode): names} from every source that has names (TVmaze, TMDB, TheTVDB);
    cached like everything else. Sources that cannot answer are left out quietly."""
    out: dict[tuple[int, int], set[str]] = {}
    for name in ("tvmaze", "tmdb", "tvdb"):
        try:
            lists = READERS[name](show)
        except Exception:
            continue
        for s, eps in lists.items():
            for e in eps:
                if e.get("name"):
                    out.setdefault((int(s), int(e["number"])), set()).add(e["name"])
    return out


def season_episodes(show: dict, season: int, source: str = "all", log=None) -> tuple[list[dict], str | None]:
    lists, used = episode_lists(show, source, log)
    return lists.get(season, []), used.get(season)


def seasons_summary(show: dict, source: str = "all") -> list[dict]:
    """For the season picker: [{"number", "episodes", "premiere", "source"}]."""
    lists, used = episode_lists(show, source)
    return [{"number": s, "episodes": len(eps),
             "premiere": min((e["airdate"] for e in eps if e.get("airdate")), default=None),
             "source": SOURCES[used[s]]} for s, eps in lists.items()]
