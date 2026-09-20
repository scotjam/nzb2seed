"""Grab a whole series: every season TVmaze lists, one season grab after another.

With "skip what I have", the episodes already in a folder you point at (the show's
folder, any layout underneath) and in qBittorrent are left out - any copy counts,
whatever its release group or resolution. Episodes are recognised by the SxxEyy in file,
folder and torrent names (for a season-pack torrent, in its file names).

Each season uses one release group and resolution. The series gets one default - the
group/resolution that covers the most of the episodes still wanted across all seasons -
and a season that has nothing from it gets its most complete option at the same
resolution. The plan is written to the job's log before anything is downloaded.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from . import episodes as episodes_mod, grab, matching, metadata
from . import season as season_mod
from .clients import ApiError, Prowlarr, QBittorrent, SABnzbd
from .config import Config
from .pipeline import Abort
from .report import Cancelled, info, step, warn

_EPS = re.compile(r"(?<![a-z0-9])s(\d{1,4})((?:[.\-]?e\d{1,4})+)(?![0-9])")


def episodes_named(name: str) -> set[tuple[int, int]]:
    """{(season, episode)} named in a file/folder/torrent name: S02E03, S02E03E04, S01E01-E03."""
    out = set()
    for m in _EPS.finditer(matching.norm(name)):
        s = int(m.group(1))
        nums = [int(x) for x in re.findall(r"e(\d+)", m.group(2))]
        if len(nums) == 2 and "-" in m.group(2) and nums[1] > nums[0]:
            nums = list(range(nums[0], nums[1] + 1))
        out |= {(s, e) for e in nums}
    return out


def _add(have: dict, pairs, where: dict | None = None, label: str = ""):
    for s, e in pairs:
        have.setdefault(s, set()).add(e)
        if where is not None:
            where.setdefault((s, e), label)


def _named(n: str, names, show_norm) -> set[tuple[int, int]]:
    """The episodes a file/folder name stands for: SxxEyy, else its episode name."""
    eps = episodes_named(n)
    if not eps and names and show_norm:
        hit = names.match(os.path.splitext(n)[0], show_norm)
        eps = {hit} if hit else set()
    return eps


def library_episodes(folder: str, show_norm: str | None = None,
                     year: int | None = None, names=None) -> tuple[dict[int, set[int]], dict]:
    """Episodes in a show's folder - every file and folder name underneath counts (videos,
    RAR sets and release folders alike). Season folder numbering is not used. A folder or
    file named after the show with another year - 'Show (1998) S01-S07' next to the 2016
    revival - is another show of that name and is left out, with all it contains."""
    def other_show(name: str) -> bool:
        mine = bool(show_norm) and season_mod.mentions_show(name, show_norm)
        return mine and not season_mod.same_show(name, show_norm, year)
    have: dict[int, set[int]] = {}
    where: dict = {}
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not other_show(d)]
        for n in dirs + files:
            if not other_show(n):
                _add(have, _named(n, names, show_norm), where, os.path.join(root, n))
    return have, where


def qbit_episodes(qb: QBittorrent, show_norm: str, year: int | None,
                  names=None) -> tuple[dict[int, set[int]], dict]:
    """Episodes of this show in qBittorrent, whatever their state or save path. Torrents
    naming another year are another show of the same name."""
    have: dict[int, set[int]] = {}
    where: dict = {}
    for t in qb.torrents():
        name = t.get("name", "")
        eps = _named(name, names, show_norm) if names and not episodes_named(name) else set()
        if not eps and not season_mod.same_show(name, show_norm, year):
            continue
        eps = eps or episodes_named(name)
        if not eps:                                   # a season pack: look at its files
            try:
                for f in qb.files(t["hash"]):
                    eps |= episodes_named(os.path.basename(f.get("name", "")))
            except ApiError:
                continue
        _add(have, eps, where, f"qBittorrent: {name}")
    return have, where


@dataclass
class SeasonPlan:
    season: int
    listed: list[int]
    have: set[int] = field(default_factory=set)
    options: list = field(default_factory=list)       # season.Option, found for the missing episodes
    choices: list = field(default_factory=list)       # the options to grab from, highest priority first
    why: str = ""

    @property
    def choice(self):
        return self.choices[0] if self.choices else None

    @property
    def missing(self) -> list[int]:
        return [e for e in self.listed if e not in self.have]

    def covers(self, o) -> int:
        return len(self.missing) if o.seasons else sum(1 for e in self.missing if o.episodes.get(e))

    def covered(self) -> int:
        """How many missing episodes the choices supply together."""
        if any(o.seasons for o in self.choices):
            return len(self.missing)
        return sum(1 for e in self.missing if any(o.episodes.get(e) for o in self.choices))


def owned(cfg: Config, show: dict, library, names=None) -> dict[int, set[int]]:
    """Episodes you already have: in ``library`` (the show's folder, or several) and in
    qBittorrent - named SxxEyy or, with ``names``, after the episode."""
    have: dict[int, set[int]] = {}
    year = int(show["premiered"][:4]) if show.get("premiered") else None
    names = names if names is not None else season_mod.names_for(cfg, show)
    for folder in ([library] if isinstance(library, str) else list(library or [])):
        if not os.path.isdir(folder):
            raise Abort(f"the folder {folder} does not exist")
        lib, _ = library_episodes(folder, matching.norm(show["name"]), year, names)
        for s, es in lib.items():
            have.setdefault(s, set()).update(es)
        info(f"in {folder}: {sum(len(v) for v in lib.values())} episode(s)")
    try:
        qb = QBittorrent(cfg.qbit_url, cfg.qbit_user, cfg.qbit_pass)
        q, _ = qbit_episodes(qb, matching.norm(show["name"]), year, names)
        for s, es in q.items():
            have.setdefault(s, set()).update(es)
        info(f"in qBittorrent: {sum(len(v) for v in q.values())} episode(s)")
    except ApiError as e:
        warn(f"qBittorrent could not be read ({e}); only the folder counts")
    return have


def choose_priority(plans: list[SeasonPlan], priority: list[tuple]):
    """Your priority list of (group, resolution): per season, every option matching it that
    has anything missing, in that order."""
    for p in plans:
        p.choices, p.why = [], ""
        if not p.missing:
            p.why = "nothing missing"
            continue
        for g, r in priority:
            same = sorted((o for o in p.options if o.group == g and o.res == r and p.covers(o)),
                          key=p.covers, reverse=True)
            p.choices += [o for o in same if o not in p.choices]
        p.why = "your priority list" if p.choices else (
            "not on Usenet" if not any(p.covers(o) for o in p.options) else "none of your picked releases has it")
    return priority[0] if priority else None


def choose(plans: list[SeasonPlan], prefer_group: str | None = None, prefer_res: str | None = None,
           fill: bool = True):
    """One group/resolution for the series, a same-resolution fallback per season - and with
    ``fill``, every other option at that resolution after it, to fill the gaps."""
    score: dict[tuple, int] = {}
    for p in plans:
        for o in p.options:
            k = (o.group, o.res)
            score[k] = score.get(k, 0) + p.covers(o)
    if prefer_group or prefer_res:
        keep = {k: v for k, v in score.items()
                if (not prefer_group or k[0] == prefer_group.lower()) and (not prefer_res or k[1] == prefer_res.lower())}
        score = keep or score
    if not score:
        return None
    # a resolution in the name first (an option without one is an unknown quantity), then how
    # many of the missing episodes it covers, then the higher resolution
    best = max(score, key=lambda k: (k[1] is not None, score[k], int((k[1] or "0p")[:-1] or 0)))
    for p in plans:
        p.choices, p.why = [], ""
        if not p.missing:
            p.why = "nothing missing"
            continue
        same = [o for o in p.options if (o.group, o.res) == best and p.covers(o)]
        same_res = [o for o in p.options if o.res == best[1] and p.covers(o)]
        if same:
            p.choices = [max(same, key=p.covers)]
            p.why = "the series' group and resolution"
        elif same_res:
            p.choices = [max(same_res, key=p.covers)]
            p.why = f"{best[0].upper()} has nothing for this season - the most complete {best[1] or ''} option"
        elif not any(p.covers(o) for o in p.options):
            p.why = "not on Usenet"
        else:
            p.why = f"nothing at {best[1] or 'this resolution'} for the missing episodes"
        if fill and p.choices and p.covered() < len(p.missing):
            before = p.covered()
            p.choices += sorted((o for o in same_res if o not in p.choices), key=p.covers, reverse=True)
            if p.covered() > before:
                p.why += f"; gaps filled from {len(p.choices) - 1} more {best[1] or ''} release(s)"
            else:
                del p.choices[1:]
    return best


def this_show(plans: list[SeasonPlan], year: int | None):
    """Leave out options that are another show of the same name: named with another year, or
    - when this show's releases carry its year (a revival: 'Show.2016.S01...') - with none."""
    if not year:
        return
    tagged = any(str(year) in re.findall(r"(?:19|20)\d\d", o.prefix) for p in plans for o in p.options)
    for p in plans:
        keep = []
        for o in p.options:
            years = re.findall(r"(?:19|20)\d\d", o.prefix)
            if years and int(years[0]) != year:
                continue
            if tagged and not years:
                continue
            keep.append(o)
        p.options = keep


def plan_series(cfg: Config, pr, show_id: int, library: str | None, skip_owned: bool,
                prefer_group: str | None = None, prefer_res: str | None = None,
                source: str = "all", priority: list[tuple] | None = None) -> tuple[dict, list[SeasonPlan]]:
    show = metadata.tvmaze_show(show_id)
    step(f"Planning {show['name']}")
    lists, used = episodes_mod.episode_lists(show, source, log=info)
    if not lists:
        raise Abort(f"no episode list for {show['name']} from {episodes_mod.SOURCES[source]}")
    info("episode lists: " + ", ".join(f"S{s:02d} {len(v)} from {episodes_mod.SOURCES[used[s]]}"
                                       for s, v in lists.items()))
    have = owned(cfg, show, library) if skip_owned else {}
    plans = options_for(cfg, pr, show, lists, have)
    if priority:
        choose_priority(plans, priority)
        info("your priority list: " + " > ".join(f"{g.upper()} {r or ''}".strip() for g, r in priority))
    else:
        best = choose(plans, prefer_group, prefer_res)
        info("series default: " + (f"{best[0].upper()} {best[1] or ''}" if best else "none found on Usenet"))
    for p in plans:
        line = f"S{p.season:02d}: {len(p.listed)} episodes, {len(p.have)} already yours, {len(p.missing)} to grab"
        if p.choices:
            names = " > ".join(f"{o.group.upper()} {o.res or ''}".strip() for o in p.choices)
            line += f" -> {names}: {p.covered()}/{len(p.missing)} ({p.why})"
        elif p.missing:
            line += f" -> skipped: {p.why or 'nothing on Usenet'}"
        info(line)
    return show, plans


def options_for(cfg: Config, pr, show: dict, lists: dict, have: dict) -> list[SeasonPlan]:
    """A plan per season with the options found for its missing episodes (one shared pool
    of searches for the whole show)."""
    plans = [SeasonPlan(n, [e["number"] for e in eps], have.get(n, set()) & {e["number"] for e in eps})
             for n, eps in lists.items()]
    wanted = {p.season: p.missing for p in plans if p.missing}
    names = season_mod.names_for(cfg, show)
    found = season_mod.find_series_options(cfg, pr, show["name"], wanted, names) if wanted else {}
    for p in plans:
        p.options = found.get(p.season, [])
    this_show(plans, int(show["premiered"][:4]) if show.get("premiered") else None)
    return plans


def summary(plans: list[SeasonPlan]) -> dict:
    """For the GUI: every (group, resolution) over all seasons, with what it covers."""
    rows: dict[str, dict] = {}
    for p in plans:
        for o in p.options:
            k = f"{o.group}|{o.res or ''}"
            row = rows.setdefault(k, {"key": k, "group": o.group.upper(), "res": o.res, "episodes": {},
                                      "season_nzbs": 0, "name": o.summary(p.missing)["name"]})
            eps = set(p.missing) if o.seasons else {e for e in p.missing if o.episodes.get(e)}
            if eps:
                row["episodes"].setdefault(p.season, set()).update(eps)
            row["season_nzbs"] += len(o.seasons)
    out = []
    for r in rows.values():
        if r["episodes"]:
            eps = {n: sorted(v) for n, v in r["episodes"].items()}
            out.append(dict(r, episodes=eps, seasons={n: len(v) for n, v in eps.items()},
                            total=sum(len(v) for v in eps.values())))
    out.sort(key=lambda r: (-r["total"], -int((r["res"] or "0p")[:-1] or 0)))
    return {"options": out, "seasons": [{"season": p.season, "episodes": len(p.listed), "have": len(p.have),
                                         "missing": len(p.missing)} for p in plans]}


def parse_pair(x) -> tuple:
    """'group|720p' (or ('group', '720p')) -> ('group', '720p'); no resolution -> None."""
    g, r = (x.split("|", 1) + [""])[:2] if isinstance(x, str) else (list(x) + [None])[:2]
    return g.lower(), (r or "").lower() or None


def grab_series(cfg: Config, show_id: int, packing: str | None = None, screens: int = 4,
                library=None, skip_owned: bool = False, plan_only: bool = False,
                prefer_group: str | None = None, prefer_res: str | None = None,
                source: str | None = None, priority: list | None = None) -> dict:
    source = source or cfg.episode_source or "all"
    priority = [parse_pair(x) for x in priority or []]
    sab = SABnzbd(cfg.sab_url, cfg.sab_key)
    pr = grab.Source(cfg, Prowlarr(cfg.prowlarr_url, cfg.prowlarr_key), sab)
    metadata.configure(cfg.flaresolverr_url, cfg.outbound_proxy)
    episodes_mod.configure_cache(cfg.path)
    try:
        show, plans = plan_series(cfg, pr, show_id, library, skip_owned, prefer_group, prefer_res, source,
                                  priority)
    finally:
        pr.close()
        metadata.close_session()
    todo = [p for p in plans if p.choices]
    wanted = sum(len(p.missing) for p in plans)
    if plan_only:
        return {"result": f"plan: {len(todo)} season(s) to grab, {wanted} episode(s) missing"}
    if not todo:
        return {"result": "nothing to grab" if not wanted else f"{wanted} episode(s) missing, none on Usenet"}
    results = []
    for p in todo:
        step(f"{show['name']} season {p.season}: " + " > ".join(f"{o.group.upper()} {o.res or ''}".strip()
                                                                  for o in p.choices))
        try:
            r = season_mod.grab_season(cfg, show_id, p.season, [o.key for o in p.choices], packing, screens,
                                       skip=p.have, source=source)
            results.append(f"S{p.season:02d} {r['result']}")
        except Cancelled:
            raise
        except (Abort, ApiError) as e:
            warn(f"season {p.season}: {e}")
            results.append(f"S{p.season:02d} failed: {e}")
    for line in results:
        info(line)
    return {"result": "; ".join(results)}
