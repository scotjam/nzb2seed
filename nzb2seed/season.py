"""Grab a whole season from Usenet: one release group, one resolution, every episode.

Closest match first: season NZBs of the chosen group/resolution, then episode NZBs for
whatever is still missing. Every episode is checked against TVmaze's episode list and,
when srrDB knows the release, against the CRC of the original video - a repost with a
re-encoded or re-muxed video is replaced by the next post. The season ends up in
``<downloads>/<Season.Release.Name>/`` (one folder per episode release inside when built
from episodes); everything nzb2seed adds that was not part of the release - MediaInfo,
screenshots, links, the srrDB/predb check, the report - goes into the sidecar folder
``<Season.Release.Name>-metadata`` next to it, so it never ends up in a torrent.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field

from . import assemble as asm
from . import archives, matching, metadata, scenerar
from .clients import PP_REPAIR, PP_UNPACK, SAB_DONE, ApiError, Prowlarr, Release, SABnzbd
from .config import Config
from .pathmap import map_path
from .pipeline import (_EP, _RES, Abort, failed_before, gb, group_of, job_dir, reuse, sab_submit,
                       sab_wait, show_prefix)
from .report import Cancelled, check_cancel, info, progress, end_progress, step, warn

VIDEO = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".wmv", ".mpg", ".mpeg"}
SIDE_SUFFIX = "-metadata"


# ---------------------------------------------------------------- naming

def scene_name(title: str) -> str:
    """The release name without an uploader's suffix: '...x264-GRP-xpost' -> '...x264-GRP'."""
    g = group_of(title)
    if not g:
        return title
    m = re.search(rf"-{re.escape(g)}(?=$|[-.\s\[(])", title, re.I)
    return title[:m.end()] if m else title


def season_release_name(title: str) -> str:
    """'Show.S03E01.720p.HDTV.x264-GRP-xpost' -> 'Show.S03.720p.HDTV.x264-GRP'."""
    return re.sub(r"(?i)(?<![a-z0-9])(S\d{1,4})E\d{1,4}(?:-?E\d{1,4})*", r"\1", scene_name(title))


def episode_numbers(name: str, season: int) -> set[int]:
    m = _EP.search(matching.norm(name))
    if not m:
        return set()
    tok = m.group(0)
    if int(re.match(r"s(\d+)", tok).group(1)) != season:
        return set()
    nums = [int(x) for x in re.findall(r"e(\d+)", tok)]
    if len(nums) == 2 and "-" in tok and nums[1] > nums[0]:
        return set(range(nums[0], nums[1] + 1))          # S01E01-E03
    return set(nums)


# ---------------------------------------------------------------- options

@dataclass
class Option:
    prefix: str                       # normalised show prefix, e.g. 'show.name.2016.'
    group: str
    res: str | None
    seasons: list[Release] = field(default_factory=list)
    episodes: dict[int, list[Release]] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.prefix}|{self.group}|{self.res or ''}"

    def summary(self, wanted: list[int]) -> dict:
        have = sorted(e for e in wanted if self.episodes.get(e))
        example = (self.seasons or [r for rs in self.episodes.values() for r in rs] or [None])[0]
        return {"key": self.key, "prefix": self.prefix, "group": self.group.upper(), "res": self.res,
                "season_nzbs": len(self.seasons), "episodes_found": have,
                "episodes_missing": [e for e in wanted if e not in have],
                "complete": bool(self.seasons) or len(have) == len(wanted),
                "name": season_release_name(example.title) if example else "",
                "size": (max((r.size for r in self.seasons), default=0)
                         or sum(max(r.size for r in rs) for rs in self.episodes.values()))}


def classify(r: Release, show_norm: str, season: int):
    """(prefix, group, res, episode numbers or None for a season NZB), or None if unrelated."""
    n = matching.norm(r.title)
    if not n.startswith(show_norm + "."):
        return None
    # the show part must be exactly the show (optionally + year or country), not a longer title
    rest = show_prefix(r.title)[len(show_norm):].strip(".")
    if rest and not re.fullmatch(r"(?:(?:19|20)\d\d|us|uk|au|nz|ca)(?:\.(?:(?:19|20)\d\d|us|uk|au|nz|ca))?", rest):
        return None
    group, res = group_of(r.title), _RES.search(n)
    if not group:
        return None
    if _EP.search(n):
        eps = episode_numbers(r.title, season)
        if not eps:
            return None
        return show_prefix(r.title), group, res.group(0) if res else None, eps
    if not re.search(rf"(?<![a-z0-9])s0*{season}(?![0-9e])", n):
        return None
    return show_prefix(r.title), group, res.group(0) if res else None, None


def find_options(cfg: Config, pr: Prowlarr, show_name: str, season: int, wanted: list[int]) -> list[Option]:
    """Every (show prefix, release group, resolution) that has this season on Usenet."""
    show_norm = matching.norm(show_name)
    seen, results = set(), []

    def search(q):
        for r in pr.search(q, cfg.indexer_ids, cfg.categories):
            if r.protocol == "usenet" and r.guid not in seen:
                seen.add(r.guid)
                results.append(r)
    search(f"{show_name} S{season:02d}")
    found_eps = set()
    for r in results:
        c = classify(r, show_norm, season)
        if c and c[3]:
            found_eps |= c[3]
    for e in wanted:                                   # episodes the season search missed
        if e not in found_eps:
            search(f"{show_name} S{season:02d}E{e:02d}")
    opts: dict[str, Option] = {}
    for r in results:
        c = classify(r, show_norm, season)
        if not c:
            continue
        prefix, group, res, eps = c
        o = opts.setdefault(f"{prefix}|{group}|{res or ''}", Option(prefix, group, res))
        if eps is None:
            o.seasons.append(r)
        else:
            for e in eps:
                o.episodes.setdefault(e, []).append(r)
    for o in opts.values():
        o.seasons.sort(key=lambda r: -(r.grabs or 0))
        for rs in o.episodes.values():
            rs.sort(key=lambda r: -(r.grabs or 0))
    return sorted(opts.values(), key=lambda o: (
        not o.summary(wanted)["complete"], -len(o.summary(wanted)["episodes_found"]),
        -int((o.res or "0p")[:-1] or 0)))


# ---------------------------------------------------------------- what a download holds

def videos_in(d: str) -> list[str]:
    out = []
    for root, _, names in os.walk(d):
        for n in names:
            p = os.path.join(root, n)
            if os.path.splitext(n)[1].lower() in VIDEO and "sample" not in p.lower().replace(d.lower(), ""):
                out.append(p)
    return out


def episodes_in(d: str, season: int, single: int | None = None) -> dict[int, str]:
    """{episode number: video file} in a download folder. For a one-episode post, the largest
    video counts as that episode even when its name is obfuscated. Episodes still packed in
    RAR sets count too (the value is then the set's first volume)."""
    found: dict[int, str] = {}
    vids = videos_in(d)
    for p in vids:
        for e in episode_numbers(os.path.basename(p), season) or episode_numbers(os.path.relpath(p, d), season):
            found.setdefault(e, p)
    if single is not None and single not in found and vids:
        found[single] = max(vids, key=os.path.getsize)
    if not vids:
        for first in archives.rar_sets(d):
            try:
                inside = [(n, s) for n, s in archives.list_contents(first)
                          if os.path.splitext(n)[1].lower() in VIDEO and "sample" not in n.lower()]
            except Exception:
                continue
            eps = set()
            for n, _ in inside:
                eps |= episode_numbers(os.path.basename(n), season)
            eps = eps or episode_numbers(os.path.relpath(first, d), season)
            if not eps and single is not None and inside:
                eps = {single}
            for e in eps:
                found.setdefault(e, first)
    return found


def packed_video(d: str, tmp: str) -> str | None:
    """Unpack one episode's video from the first RAR set under ``d`` into ``tmp``."""
    for first in archives.rar_sets(d):
        inside = [(n, s) for n, s in archives.list_contents(first)
                  if os.path.splitext(n)[1].lower() in VIDEO and "sample" not in n.lower()]
        if inside:
            name = max(inside, key=lambda x: x[1])[0]
            archives.extract(first, [name], tmp)
            p = os.path.join(tmp, *name.replace("\\", "/").split("/"))
            return p if os.path.exists(p) else None
    return None


class Srr:
    """srrDB lookups, cached per release."""

    def __init__(self):
        self.cache: dict[str, dict | None] = {}

    def details(self, release: str) -> dict | None:
        if release not in self.cache:
            try:
                self.cache[release] = metadata.srrdb_details(release)
            except Exception as e:           # srrDB being down must not stop a grab
                warn(f"srrDB: {e}")
                self.cache[release] = None
        return self.cache[release]

    def video_crc(self, release: str) -> dict[str, tuple[int, str]]:
        d = self.details(release) or {}
        return {os.path.basename(f["name"]).lower(): (f["size"], f["crc"].upper())
                for f in d.get("archived-files", []) if os.path.splitext(f["name"])[1].lower() in VIDEO}


def genuine(srr: Srr, release: str, video: str) -> tuple[bool | None, str]:
    """Is ``video`` the release's original video? (True/False, or None when srrDB cannot say)."""
    known = srr.video_crc(release)
    if not known:
        return None, "srrDB has no CRC for this release"
    size = os.path.getsize(video)
    match = [c for n, (s, c) in known.items() if s == size]
    if not match:
        return False, f"size {size} differs from the original ({', '.join(str(s) for s, _ in known.values())})"
    progress(f"CRC check of {os.path.basename(video)}")
    crc = metadata.crc32(video)
    end_progress()
    return (crc in match), (f"CRC {crc} matches the original" if crc in match
                            else f"CRC {crc} differs from the original {match[0]} (re-encoded or re-muxed repost)")


# ---------------------------------------------------------------- the grab

class _Skip:
    """``with _Skip("E03"):`` - an unexpected error costs that one item, not the whole grab
    (cancelling still stops everything)."""

    def __init__(self, what: str, notes: list | None = None):
        self.what, self.notes = what, notes

    def __enter__(self):
        return self

    def __exit__(self, kind, err, tb):
        if err is None or isinstance(err, Cancelled) or not isinstance(err, Exception):
            return False
        msg = f"{self.what}: skipped after an error: {err}"
        warn(msg)
        if self.notes is not None:
            self.notes.append(msg)
        return True


def downloads_root(cfg: Config, sab: SABnzbd) -> str:
    complete = (sab.config().get("misc", {}) or {}).get("complete_dir") or "/downloads"
    return map_path(complete, cfg.sab_to_local)


PACKING = {"scene": "Keep scene RARs", "unpack": "Unpack all"}


def grab_season(cfg: Config, show_id: int, season: int, key: str, packing: str | None = None,
                screens: int = 4) -> dict:
    """``packing``: "scene" keeps (or rebuilds from srrDB's .srr) the original scene RAR set of
    every RAR'd release, "unpack" leaves the unpacked video. None = the configured default."""
    packing = packing or cfg.season_packing or "scene"
    if packing not in PACKING:
        raise Abort(f"unknown packing {packing!r} (use one of: {', '.join(PACKING)})")
    pr = Prowlarr(cfg.prowlarr_url, cfg.prowlarr_key, cfg.outbound_proxy)
    sab = SABnzbd(cfg.sab_url, cfg.sab_key)
    srr = Srr()
    metadata.configure(cfg.flaresolverr_url, cfg.outbound_proxy)
    try:
        return _grab_season(cfg, pr, sab, srr, show_id, season, key, packing, screens)
    finally:
        metadata.close_session()


def _grab_season(cfg, pr, sab, srr, show_id: int, season: int, key: str, packing: str,
                 screens: int) -> dict:
    step("Looking up the season")
    show = metadata.tvmaze_show(show_id)
    episodes = metadata.tvmaze_episodes(show_id, season)
    wanted = [e["number"] for e in episodes]
    if not wanted:
        raise Abort(f"TVmaze lists no episodes for {show['name']} season {season}")
    info(f"{show['name']} season {season}: {len(wanted)} episodes on TVmaze")
    opt = next((o for o in find_options(cfg, pr, show["name"], season, wanted) if o.key == key), None)
    if opt is None:
        raise Abort("that release group / resolution is no longer found for this season")
    info(f"{opt.group.upper()} {opt.res or ''}: {len(opt.seasons)} season NZB(s), "
         f"episode NZBs for {len(opt.episodes)} of {len(wanted)} episodes")
    pp = PP_UNPACK        # never +Delete; the archives stay until the RAR decision below
    info(f"packing: {PACKING[packing]}")

    root = downloads_root(cfg, sab)
    example = (opt.seasons or [r for e in sorted(opt.episodes) for r in opt.episodes[e]])[0]
    name = season_release_name(example.title)
    dest = os.path.join(root, name)
    side = dest + SIDE_SUFFIX
    owned = asm.Owned(os.path.join(side, "owned.json"))
    if os.path.exists(dest) and os.path.abspath(dest) not in owned.dirs:
        raise Abort(f"{dest} already exists and was not created by nzb2seed; not touching it")
    info(f"season folder: {dest}")

    have: dict[int, tuple[str, str]] = {}      # episode -> (folder or file it came from, release name)
    partial: list[tuple[str, str]] = []        # season downloads that fell short: (folder, release)
    notes: list[str] = []
    in_place: set[str] = set()
    if os.path.isdir(dest):
        # an earlier grab of this season: its episodes stay, only what is missing is fetched
        for child in sorted(os.listdir(dest)):
            p = os.path.join(dest, child)
            if os.path.isdir(p):
                for e, _ in episodes_in(p, season).items():
                    if e in wanted and e not in have:
                        have[e] = (p, child)
                        in_place.add(p)
        if not have:
            for e in episodes_in(dest, season):
                if e in wanted:
                    have[e] = (dest, name)
                    in_place.add(dest)
        if have:
            info(f"already in the season folder from an earlier grab: "
                 + ", ".join(f"E{e:02d}" for e in sorted(have)))

    def accept(d: str, eps: dict[int, str], release: str, check: bool) -> set[int]:
        ok = set()
        for e, video in eps.items():
            if e in have or e not in wanted:
                continue
            if check and os.path.splitext(video)[1].lower() in VIDEO:
                good, why = genuine(srr, release, video)
                if good is False:
                    warn(f"E{e:02d} in {release}: {why}")
                    continue
            ok.add(e)
            have[e] = (d, release)
        return ok

    # 1. season NZBs of this group/resolution, all of them before going down a level
    if opt.seasons:
        step("Trying season NZBs")
    for rel in opt.seasons:
        check_cancel()
        if set(wanted) <= set(have):
            break
        with _Skip(rel.title, notes):
            if failed_before(cfg, sab, rel):
                continue
            again = reuse(cfg, sab, rel, lambda d: set(wanted) <= set(episodes_in(d, season)))
            if again and again[1]:
                d = again[1]
            else:
                try:
                    nzo = again[0] if again else sab_submit(cfg, pr, sab, rel, None, pp)
                except ApiError as e:
                    warn(f"{rel.indexer}: {e}")
                    continue
                status, slot = sab_wait(sab, {nzo: rel.title})[nzo]
                if status not in SAB_DONE:
                    continue
                d = job_dir(cfg, slot, rel.title)
            got = accept(d, episodes_in(d, season), scene_name(rel.title), check=False)
            info(f"{rel.title}: holds {len(got)} of {len(wanted)} episodes")
            partial.append((d, scene_name(rel.title)))

    # 2. episode NZBs for whatever is still missing - every candidate before giving up
    missing = [e for e in wanted if e not in have]
    if missing:
        step(f"Downloading {len(missing)} episode(s) on their own")
    queue = {e: list(opt.episodes.get(e, [])) for e in missing}
    pending: dict[str, tuple[int, Release]] = {}

    def submit_next(e: int):
        while queue[e]:
            rel = queue[e].pop(0)
            with _Skip(f"E{e:02d} {rel.title}", notes):
                if failed_before(cfg, sab, rel):
                    continue
                again = reuse(cfg, sab, rel, lambda d: e in episodes_in(d, season, e))
                if again and again[1]:
                    if accept(again[1], {e: episodes_in(again[1], season, e)[e]}, scene_name(rel.title), True):
                        return
                    continue
                try:
                    nzo = again[0] if again else sab_submit(cfg, pr, sab, rel, None, pp)
                except ApiError as err:
                    warn(f"{rel.indexer}: {err}")
                    continue
                pending[nzo] = (e, rel)
                return
        msg = f"E{e:02d}: no {opt.group.upper()} {opt.res or ''} post of this episode worked out"
        warn(msg)
        notes.append(msg)

    for e in missing:
        submit_next(e)
    while pending:
        batch = dict(pending)
        pending.clear()
        for nzo, (status, slot) in sab_wait(sab, {n: r.title for n, (_, r) in batch.items()}).items():
            e, rel = batch[nzo]
            if status in SAB_DONE:
                done_ok = False
                with _Skip(f"E{e:02d} {rel.title}", notes):
                    d = job_dir(cfg, slot, rel.title)
                    eps = episodes_in(d, season, e)
                    done_ok = e in eps and bool(accept(d, {e: eps[e]}, scene_name(rel.title), True))
                    if e not in eps:
                        warn(f"{rel.title} does not hold E{e:02d}")
                if done_ok:
                    continue
            submit_next(e)

    if not have:
        raise Abort(f"none of the {len(wanted)} episodes could be downloaded from "
                    f"{opt.group.upper()} {opt.res or ''}; nothing was created")

    # 3. lay the season out: one release folder per source, sidecar for everything else
    step("Laying out the season")
    owned.makedirs(dest)
    owned.dirs.add(os.path.abspath(dest))
    releases: dict[str, str] = {}              # release name -> its folder in the season folder
    season_srcs = {p for p, _ in partial}
    laid: set[int] = set()                     # verified episodes that made it into the season folder

    def move_dir(src: str, dst: str):
        if os.path.abspath(src) == os.path.abspath(dst) or os.path.exists(dst):
            return
        os.replace(src, dst)                   # SABnzbd's folder and the season folder share a disk
        owned.dirs.add(os.path.abspath(dst))

    sources = {have[e][0] for e in wanted if e in have}
    if len(sources) == 1 and set(wanted) <= set(have) and next(iter(sources)) in season_srcs - in_place:
        # one complete season release: its contents are the season folder
        src = next(iter(sources))
        for n in os.listdir(src):
            p = os.path.join(src, n)
            if os.path.isdir(p):
                move_dir(p, os.path.join(dest, n))
            else:
                asm._move(p, os.path.join(dest, n), owned)
        releases[name] = dest
        laid |= set(have)
    else:
        for e in sorted(have):
            with _Skip(f"placing E{e:02d}", notes):
                src, release = have[e]
                if src in in_place:
                    releases[os.path.basename(src) if src != dest else name] = src
                    laid.add(e)
                    continue
                if src not in season_srcs:
                    # a single-episode download: the whole folder is that episode's release
                    target = os.path.join(dest, scene_name(release))
                    move_dir(src, target)
                    releases[os.path.basename(target)] = target
                    laid.add(e)
                    continue
                # an episode out of a season download that fell short
                video = episodes_in(src, season).get(e)
                if video is None:
                    continue
                parent = os.path.dirname(video)
                if parent != src and episode_numbers(os.path.basename(parent), season) == {e}:
                    target = os.path.join(dest, os.path.basename(parent))    # its own release folder
                    move_dir(parent, target)
                else:
                    ep_name = re.sub(rf"(?i)(?<![a-z0-9])S{season:02d}(?![0-9e])", f"S{season:02d}E{e:02d}",
                                     season_release_name(release), count=1)
                    target = os.path.join(dest, ep_name)
                    owned.makedirs(target)
                    for n in os.listdir(src):
                        p = os.path.join(src, n)
                        if os.path.isfile(p) and e in episode_numbers(n, season):
                            asm._move(p, os.path.join(target, n), owned)
                releases[os.path.basename(target)] = target
                laid.add(e)
    owned.save()

    # 4. MediaInfo and screenshots - while the videos are still unpacked
    step("MediaInfo and screenshots")
    os.makedirs(side, exist_ok=True)
    media = {}
    vids = sorted(videos_in(dest))
    tmp = os.path.join(side, "tmp-unpacked")
    if not vids:
        # already packed (e.g. an earlier grab): a Sample (same encode) is enough; unpack only without one
        samples = sorted(p for r, _, ns in os.walk(dest) for p in (os.path.join(r, n) for n in ns)
                         if os.path.splitext(p)[1].lower() in VIDEO and "sample" in p.lower())
        if samples:
            vids = samples[:1]
            info(f"MediaInfo and screenshots from the sample {os.path.basename(samples[0])}")
        else:
            try:
                info("no episode has a sample: unpacking one episode temporarily for MediaInfo and screenshots")
                v = packed_video(dest, tmp)
                vids = [v] if v else []
            except Exception as e:
                warn(f"could not unpack an episode for MediaInfo: {e}")
    if vids:
        video = vids[0]
        try:
            with open(os.path.join(side, "mediainfo.txt"), "w", encoding="utf-8") as fh:
                fh.write(metadata.mediainfo(video))
            media["mediainfo_of"] = (os.path.relpath(video, dest).replace(os.sep, "/")
                                     if os.path.abspath(video).startswith(os.path.abspath(dest) + os.sep)
                                     else os.path.basename(video) + " (unpacked temporarily)")
        except Exception as e:
            warn(f"MediaInfo failed: {e}")
        try:
            shots = metadata.screenshots(video, os.path.join(side, "screens"), screens)
            media["screenshots"] = [os.path.relpath(x, side).replace(os.sep, "/") for x in shots]
        except Exception as e:
            warn(f"screenshots failed: {e}")
    shutil.rmtree(tmp, ignore_errors=True)

    # 5. check each release against srrDB, keep or rebuild its scene RARs, tidy it
    step("Checking the releases against srrDB and predb")
    checks = []
    fetcher = FileFetcher(cfg, pr, sab, set())
    for release, folder in sorted(releases.items()):
        result = None
        with _Skip(f"checking {release}", notes):
            result = check_release(srr, release, folder, packing, owned, fetcher)
        checks.append(result or {"release": release, "folder": folder, "srrdb": None, "files": [],
                                 "video": [], "predb": None, "predb_me": None, "nfo": None,
                                 "error": "the check failed; see the notes"})
    owned.save()

    # 6. the report in the sidecar
    step("Writing the report")
    placed = episodes_in(dest, season)
    report = {
        "name": name, "folder": dest, "sidecar": side, "created": time.time(),
        "show": show, "season": season, "group": opt.group.upper(), "res": opt.res,
        "links": metadata.links(show), "packing": PACKING[packing],
        "episodes": [{"number": e["number"], "name": e["name"], "airdate": e["airdate"],
                      "present": e["number"] in placed or e["number"] in laid,
                      "file": os.path.relpath(placed[e["number"]], dest).replace(os.sep, "/")
                      if e["number"] in placed else None}
                     for e in episodes],
        "releases": checks, "notes": notes,
    }
    report.update(media)
    with open(os.path.join(side, "report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1)
    write_html(report, side)
    owned.save()
    got, total = sum(1 for e in report["episodes"] if e["present"]), len(wanted)
    missing = [f"E{e['number']:02d}" for e in report["episodes"] if not e["present"]]
    if missing:
        warn("missing: " + ", ".join(missing))
    return {"result": f"{got}/{total} episodes" + ("" if not missing else " - missing " + ", ".join(missing)),
            "report": name}


def check_release(srr: Srr, release: str, folder: str, packing: str, owned, fetch=None) -> dict:
    """Compare a release folder with the files the original release shipped with (srrDB):
    download missing ones into the folder, flag those srrDB cannot provide, verify the
    video's CRC, and remove download junk that was never part of the release."""
    det = srr.details(release)
    out = {"release": release, "folder": folder, "srrdb": f"{metadata.SRRDB_WEB}/{release}" if det else None,
           "files": [], "video": [], "predb": None, "predb_me": None, "xrel": None, "nfo": None, "rars": None}
    try:
        out["predb"] = metadata.predb(release)
    except Exception as e:
        warn(f"predb.net: {e}")
    for key_, fn in (("predb_me", metadata.predb_me), ("xrel", metadata.xrel)):
        try:
            out[key_] = fn(release)
            if not out[key_].get("available"):
                info(f"{release}: {out[key_]['why']}")
        except Exception as e:
            out[key_] = {"available": False, "why": f"{key_}: {e}"}
    present = {os.path.relpath(os.path.join(r, n), folder).replace(os.sep, "/").lower(): os.path.join(r, n)
               for r, _, ns in os.walk(folder) for n in ns}
    wanted = metadata.release_files(det) if det else []
    keep = set()
    for f in wanted:
        rel = f["name"].replace("\\", "/")
        have = present.get(rel.lower()) or next((p for k, p in present.items()
                                                 if os.path.basename(k) == os.path.basename(rel).lower()), None)
        state = "present"
        if have and os.path.getsize(have) == f["size"]:
            keep.add(os.path.abspath(have))
        else:
            data = None
            try:
                data = metadata.srrdb_file(release, rel)
            except Exception:
                pass
            dst = os.path.join(folder, *rel.split("/"))
            if data and len(data) == f["size"]:
                owned.makedirs(os.path.dirname(dst))
                with open(dst, "wb") as fh:
                    fh.write(data)
                owned.add_file(dst)
                keep.add(os.path.abspath(dst))
                state = "downloaded from srrDB"
                info(f"{release}: downloaded {rel} from srrDB")
            elif fetch and f.get("crc") and fetch(release, rel, f["size"], f["crc"], dst):
                owned.add_file(dst)
                keep.add(os.path.abspath(dst))
                state = "downloaded from another Usenet post"
                info(f"{release}: got {rel} from another post of the release (size and CRC match)")
            else:
                why = getattr(fetch, "last_why", None)
                state = "missing - not available on srrDB" + (f"; other posts: {why}" if why else "")
                warn(f"{release}: {rel} is missing and srrDB cannot provide it")
        out["files"].append({"name": rel, "size": f["size"], "state": state})
    # the video(s): original CRC?
    for v in videos_in(folder):
        good, why = genuine(srr, release, v) if det else (None, "not on srrDB")
        out["video"].append({"name": os.path.relpath(v, folder).replace(os.sep, "/"),
                             "size": os.path.getsize(v), "genuine": good, "why": why})
        keep.add(os.path.abspath(v))
    if det is None:
        out["files"].append({"name": "-", "size": 0, "state": "release not on srrDB"})

    # the RAR decision: keep the scene RARs, rebuild them, or leave the video unpacked
    scene_ok = False
    vols = scenerar.volumes(det) if det else []
    if packing == "scene" and vols:
        ok, problems = scenerar.check_volumes(folder, det)
        if ok:
            scene_ok, out["rars"] = True, f"the {len(vols)} scene RAR volumes are here; every CRC matches srrDB"
        else:
            good = next((os.path.join(folder, *v["name"].split("/")) for v in out["video"] if v["genuine"]), None)
            if good:
                progress(f"rebuilding the scene RARs of {release}")
                rebuilt, why = scenerar.rebuild(release, det, good, folder)
                end_progress()
                scene_ok, out["rars"] = rebuilt, why if rebuilt else f"left unpacked: {why}"
            else:
                out["rars"] = ("left as downloaded: no video with the original CRC to rebuild the scene RARs from"
                               + (f" ({'; '.join(problems[:2])})" if problems else ""))
        (info if scene_ok else warn)(f"{release}: {out['rars']}")
    elif packing == "scene":
        out["rars"] = "not a RAR'd release (or not on srrDB): kept as it is"
    else:
        out["rars"] = "unpacked, as chosen"
    if scene_ok:
        names = {os.path.basename(v["name"]).lower() for v in vols}
        for r, _, ns in os.walk(folder):
            for n in ns:
                if n.lower() in names:
                    keep.add(os.path.abspath(os.path.join(r, n)))
                    owned.add_file(os.path.join(r, n))
        for v in videos_in(folder):
            keep.discard(os.path.abspath(v))           # the unpacked copy of what is in the RARs
    unpacked_present = any(os.path.abspath(v) in keep for v in videos_in(folder))

    # remove what never belonged to the release (par2, nzb, poster extras, non-scene RARs,
    # and - when the scene RARs are kept - the unpacked copy of the video)
    for r, _, ns in os.walk(folder):
        for n in ns:
            p = os.path.abspath(os.path.join(r, n))
            ext = os.path.splitext(n)[1].lower()
            volume = bool(re.search(r"\.(rar|r\d{2,3}|\d{3})$", n, re.I))
            stray = ext in (".par2", ".nzb", ".srr", ".srs", ".txt", ".url", ".exe", ".html")
            if p in keep or os.path.basename(p).lower().startswith("sample") and ext in VIDEO:
                continue
            if stray or (volume and (scene_ok or unpacked_present)) or (ext in VIDEO and scene_ok) or \
                    (det and not volume and ext not in VIDEO):
                os.remove(p)
                owned.forget_file(p)
    for r, ds, _ in os.walk(folder, topdown=False):
        for dname in ds:
            try:
                os.rmdir(os.path.join(r, dname))
            except OSError:
                pass
    nfo = next((p for p in (os.path.join(r, n) for r, _, ns in os.walk(folder) for n in ns)
                if p.lower().endswith(".nfo")), None)
    if nfo:
        with open(nfo, "rb") as fh:
            out["nfo"] = {"name": os.path.basename(nfo), "text": metadata.nfo_text(fh.read())}
    return out


# ---------------------------------------------------------------- one missing file from another post

class FileFetcher:
    """Get one file of a release (e.g. its Sample) that the post used lacked and srrDB does
    not store: find other posts of the same release whose NZB lists that file, queue an NZB
    trimmed to just that file, and keep it only if its size and CRC match the original."""

    def __init__(self, cfg: Config, pr: Prowlarr, sab: SABnzbd, used: set[str]):
        self.cfg, self.pr, self.sab = cfg, pr, sab
        self.used = used                      # SABnzbd job folders created for this (ours)
        self.posts: dict[str, list[Release]] = {}

    def leftovers(self, job: str) -> list[str]:
        """Folders of earlier helper downloads for this same file (named after our helper job)."""
        try:
            root = downloads_root(self.cfg, self.sab)
            return [os.path.join(root, n) for n in os.listdir(root)
                    if (n == job or n.startswith(job + ".")) and os.path.isdir(os.path.join(root, n))]
        except Exception:
            return []

    def __call__(self, release: str, relpath: str, size: int, crc: str, dest: str) -> bool:
        from . import nzbinfo
        self.last_why = None
        name = os.path.basename(relpath)
        job = f"{release}.{os.path.splitext(name)[0]}"
        # an earlier helper download of this file may already be on disk
        old = self.leftovers(job)
        for d in old:
            for rt, _, ns in os.walk(d):
                for n in ns:
                    g = os.path.join(rt, n)
                    if os.path.getsize(g) == size and metadata.crc32(g).upper() == crc.upper():
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        os.replace(g, dest)
                        info(f"{release}: {name} was already downloaded earlier - used that copy")
                        for x in old:
                            shutil.rmtree(x, ignore_errors=True)
                        return True
        for x in old:
            shutil.rmtree(x, ignore_errors=True)       # ours, and not the right file
        if release not in self.posts:
            q = release.replace(".", " ")
            self.posts[release] = [r for r in self.pr.search(q, self.cfg.indexer_ids, self.cfg.categories)
                                   if r.protocol == "usenet" and scene_name(r.title).lower() == release.lower()]
        for r in self.posts[release]:
            check_cancel()
            try:
                trimmed = nzbinfo.trim(self.pr.fetch(r), name)
            except Exception:
                continue
            if trimmed is None:
                continue
            info(f"{release}: {name} is in {r.title} ({r.indexer}) - downloading just that file")
            try:
                nzo = self.sab.add_nzb(trimmed, job, self.cfg.sab_category, PP_REPAIR, self.cfg.sab_priority)
                status, slot = sab_wait(self.sab, {nzo: name})[nzo]
                if status not in SAB_DONE:
                    continue
                d = job_dir(self.cfg, slot, job)
            except (ApiError, Abort) as e:
                warn(f"{r.indexer}: {e}")
                continue
            if os.path.isfile(d):
                # SABnzbd reports a one-file job by the file; its folder is only ours if named after the job
                parent = os.path.dirname(d)
                files = [d]
                d = parent if matching.norm(os.path.basename(parent)).startswith(matching.norm(job)) else None
            else:
                files = [os.path.join(rt, n) for rt, _, ns in os.walk(d) for n in ns]
            if d:
                self.used.add(d)
            seen = []
            for g in files:
                g_size = os.path.getsize(g)
                g_crc = metadata.crc32(g).upper() if g_size == size else "-"
                if g_size == size and g_crc == crc.upper():
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    os.replace(g, dest)
                    if d:
                        shutil.rmtree(d, ignore_errors=True)
                    return True
                seen.append(f"{os.path.basename(g)}: {g_size} bytes" + (f", CRC {g_crc}" if g_crc != "-" else ""))
            self.last_why = (f"{r.title} ({r.indexer}) had " + ("; ".join(seen) or "nothing")
                             + f" - the original is {size} bytes, CRC {crc.upper()}")
            warn(self.last_why)
            if d:
                shutil.rmtree(d, ignore_errors=True)
            else:
                for g in files:
                    os.remove(g)                    # the stray file of our own one-file job
        return False


# ---------------------------------------------------------------- the report page (sidecar)

def _esc(s) -> str:
    return (str(s) if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def write_html(r: dict, side: str):
    """A self-contained report.html in the sidecar, for opening straight from the share."""
    rows = "".join(f"<tr><td>E{e['number']:02d}</td><td>{_esc(e['name'])}</td><td>{_esc(e['airdate'])}</td>"
                   f"<td class={'ok' if e['present'] else 'bad'}>{'present' if e['present'] else 'MISSING'}</td>"
                   f"<td>{_esc(e.get('file') or '')}</td></tr>" for e in r["episodes"])
    rel_html = ""
    for c in r["releases"]:
        files = "".join(f"<tr><td>{_esc(f['name'])}</td><td>{f['size']}</td>"
                        f"<td class={'ok' if f['state'] != 'missing - not available on srrDB' else 'bad'}>"
                        f"{_esc(f['state'])}</td></tr>" for f in c["files"])
        vids = "".join(f"<li>{_esc(v['name'])}: {_esc(v['why'])}</li>" for v in c["video"])
        pre = c.get("predb") or {}
        pre_line = (f"predb.net: pre {time.strftime('%Y-%m-%d %H:%M', time.gmtime(pre['pretime']))} UTC, {_esc(pre.get('section'))}"
                    + (f", NUKED: {_esc(pre.get('reason'))}" if pre.get("nuked") else "")) if pre.get("pretime") else "predb.net: not found"
        for label, key_ in (("predb.me", "predb_me"), ("xrel.to", "xrel")):
            me = c.get(key_) or {}
            pre_line += f" · {label}: " + (_esc(me.get("why")) if not me.get("available") else
                                           ("found" + (f", NUKED: {_esc(me.get('reason', ''))}" if me.get("nuked") else "")
                                            if me.get("found") else "not found"))
        link = f" - <a href='{_esc(c['srrdb'])}'>srrDB</a>" if c.get("srrdb") else ""
        nfo = f"<details><summary>{_esc(c['nfo']['name'])}</summary><pre class=nfo>{_esc(c['nfo']['text'])}</pre></details>" if c.get("nfo") else ""
        rars = f"<p>RARs: {_esc(c.get('rars'))}</p>" if c.get("rars") else ""
        rel_html += (f"<h3>{_esc(c['release'])}</h3><p>{pre_line}{link}</p>{rars}<ul>{vids}</ul>"
                     f"<table><tr><th>File of the original release</th><th>Size</th><th>State</th></tr>{files}</table>{nfo}")
    links = " · ".join(f"<a href='{_esc(u)}'>{_esc(k)}</a>" for k, u in r["links"].items())
    shots = "".join(f"<a href='{_esc(s)}'><img src='{_esc(s)}'></a>" for s in r.get("screenshots", []))
    mi = ""
    if os.path.exists(os.path.join(side, "mediainfo.txt")):
        with open(os.path.join(side, "mediainfo.txt"), encoding="utf-8") as fh:
            mi = f"<h2>MediaInfo ({_esc(r.get('mediainfo_of'))})</h2><pre>{_esc(fh.read())}</pre>"
    notes = ("<h2>Notes</h2><ul>" + "".join(f"<li class=bad>{_esc(n)}</li>" for n in r.get("notes", [])) + "</ul>"
             if r.get("notes") else "")
    page = f"""<!doctype html><meta charset=utf-8><title>{_esc(r['name'])}</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;max-width:1100px;margin:24px auto;padding:0 16px}}
table{{border-collapse:collapse;margin:8px 0}}td,th{{border:1px solid #ccc;padding:3px 8px;text-align:left}}
.ok{{color:#23884f}}.bad{{color:#c23b32;font-weight:bold}}pre{{background:#f3f3f3;padding:10px;overflow:auto}}
pre.nfo{{font-family:Consolas,monospace;line-height:1.1}}img{{width:48%;margin:1%}}</style>
<h1>{_esc(r['name'])}</h1><p>{_esc(r['show']['name'])} season {r['season']} - {_esc(r['group'])} {_esc(r['res'] or '')} - {_esc(r.get('packing', ''))} - {links}</p>
<h2>Episodes (TVmaze)</h2><table><tr><th></th><th>Title</th><th>Aired</th><th></th><th>File</th></tr>{rows}</table>
{notes}<h2>Releases</h2>{rel_html}{mi}<h2>Screenshots</h2>{shots}"""
    with open(os.path.join(side, "report.html"), "w", encoding="utf-8") as fh:
        fh.write(page)


def reports(cfg: Config, sab: SABnzbd) -> list[dict]:
    root = downloads_root(cfg, sab)
    out = []
    for n in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        p = os.path.join(root, n, "report.json")
        if n.endswith(SIDE_SUFFIX) and os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as fh:
                    r = json.load(fh)
                out.append({"name": r["name"], "created": r.get("created"),
                            "episodes": sum(1 for e in r["episodes"] if e["present"]),
                            "total": len(r["episodes"]), "show": r["show"]["name"], "season": r["season"]})
            except (OSError, ValueError, KeyError):
                pass
    return sorted(out, key=lambda r: -(r.get("created") or 0))
