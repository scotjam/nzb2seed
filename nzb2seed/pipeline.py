"""The build pipeline, shared by the CLI and the GUI.

Output goes through ``report`` so the same code can print to a console or
feed a GUI job log.
"""
from __future__ import annotations

import dataclasses
import itertools
import os
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from . import assemble as asm
from . import archives, matching, nzbinfo
from .clients import (PP_REPAIR, PP_UNPACK, SAB_DONE, SAB_FAILED, ApiError,
                      Prowlarr, QBittorrent, Release, SABnzbd)
from .config import Config
from .ledger import Ledger
from .pathmap import map_path
from .report import ask as report_ask
from .report import check_cancel, end_progress, info, line, pieces, progress, step, warn
from .torrent import PieceVerifier, Torrent, parse

STOPPED = {"pausedUP", "pausedDL", "stoppedUP", "stoppedDL", "error", "missingFiles"}


class Abort(Exception):
    pass


@dataclass
class Options:
    pp: str | None = None          # "auto" | "repair" | "unpack" | None = config default
    output_dir: str | None = None
    no_cleanup: bool = False
    local_verify: bool = False
    no_qbit: bool = False
    start: bool = False
    dry_run: bool = False
    retry_bad: bool | None = None  # None = config default (behaviour.retry_bad_pieces)


def gb(n: int) -> str:
    return f"{n / 1024**3:.2f} GB" if n >= 1024**3 else f"{n / 1024**2:.1f} MB"


# ---------------------------------------------------------------- pairing

def search(cfg: Config, query: str) -> tuple[list[Release], list[Release]]:
    pr = Prowlarr(cfg.prowlarr_url, cfg.prowlarr_key)
    results = pr.search(query, cfg.indexer_ids, cfg.categories)
    return ([r for r in results if r.protocol == "torrent"],
            [r for r in results if r.protocol == "usenet"])


def pair(cfg: Config, pr: Prowlarr, torrent: Release, nzbs: list[Release]
         ) -> tuple[list[list[Release]], list[Release], str]:
    """Usenet groups for a torrent: each group is one release, later entries are
    fallbacks (same post on other indexers). Also returns the (possibly grown)
    NZB list and a short description of how the pairing was made."""
    exact = matching.rank_nzbs(matching.exact_matches(torrent, nzbs))
    if exact and torrent.size:
        # smaller NZBs cannot hold the whole torrent (re-checked against the real .torrent later)
        exact = [n for n in exact if n.size >= torrent.size] or exact
    if exact:
        return [exact], nzbs, f"exact name match on {len(exact)} indexer(s): " + \
            ", ".join(n.indexer for n in exact)
    # Nothing is ticked otherwise: the build finds NZBs itself, closest match first
    # (multi-season, then season, then episode) - ticking episodes here would skip that.
    return [], nzbs, "automatic"


def group_selection(nzbs: list[Release]) -> list[list[Release]]:
    """Hand-picked NZBs: identical names are one release with fallbacks."""
    groups: dict[str, list[Release]] = {}
    for n in nzbs:
        groups.setdefault(matching.norm(n.title), []).append(n)
    return list(groups.values())


def size_warning(torrent: Release, groups: list[list[Release]]):
    total = sum(g[0].size for g in groups)
    if torrent.size and total < torrent.size * 0.97:
        warn(f"NZB total {gb(total)} is smaller than the torrent ({gb(torrent.size)}); "
             "some torrent files will probably be missing")


# ---------------------------------------------------------------- SABnzbd

def sab_preflight(sab: SABnzbd, pp: int):
    try:
        conf = sab.config()
    except ApiError as e:
        warn(f"could not read SABnzbd config ({e})")
        return
    misc = conf.get("misc", {})
    if str(misc.get("ignore_samples", "0")) not in ("0", "", "False"):
        warn("SABnzbd 'ignore_samples' is on: Sample files will be skipped and the torrent "
             "cannot reach 100% if it has a Sample folder")
    if misc.get("unwanted_extensions") and str(misc.get("action_on_unwanted_extensions", "0")) != "0":
        warn(f"SABnzbd acts on unwanted extensions ({misc.get('unwanted_extensions')}); "
             "a job may be paused/aborted")
    if misc.get("cleanup_list"):
        warn(f"SABnzbd cleanup list removes: {misc.get('cleanup_list')} - these will be missing "
             "if the torrent contains them")
    if pp == PP_REPAIR and str(misc.get("deobfuscate_final_filenames", "0")) not in ("0", "False"):
        info("SABnzbd deobfuscation is on; renamed files are matched by size/hash instead of name")


MAX_PEEK = 8        # NZBs looked inside per build (each is a "grab" on most indexers)
_RAR = re.compile(r"\.(rar|r\d{2,3}|\d{3})$", re.I)


def packed_release(t: Torrent) -> bool:
    """Is the torrent a RAR'd release? Judged by bytes, so an unpacked release that
    only carries a small Subs/*.subs.rar still counts as unpacked."""
    rar = sum(f.length for f in t.real_files if _RAR.search(f.name))
    return rar > t.total_size / 2


def choose_pp(opts: Options, cfg: Config, t: Torrent) -> int:
    """RARs in the torrent: +Repair only, SABnzbd must not unpack. Unpacked files in the
    torrent: +Repair/Unpack (archives are kept, then removed as extras). Never +Delete."""
    mode = opts.pp or cfg.post_processing
    if mode == "repair":
        return PP_REPAIR
    if mode == "unpack":
        return PP_UNPACK
    return PP_REPAIR if packed_release(t) else PP_UNPACK


def rank_posts(pr: Prowlarr, t: Torrent, group: list[Release]):
    """Candidate posts for one release, best first: [(release, nzb bytes | None, score | None)].

    NZBs smaller than the torrent are dropped (they cannot hold all of it), same-size
    NZBs are treated as one post, and the rest are ranked by looking inside them."""
    big = [r for r in group if r.size >= t.total_size]
    if len(big) < len(group):
        info(f"skipping {len(group) - len(big)} NZB(s) smaller than the torrent ({gb(t.total_size)}); "
             "they cannot contain all of it")
    seen, uniq = set(), []
    for r in sorted(big, key=lambda r: -(r.grabs or 0)):
        if r.size not in seen:
            seen.add(r.size)
            uniq.append(r)
    ranked = []
    for r in uniq[:MAX_PEEK]:
        check_cancel()
        try:
            data = pr.fetch(r)
            sc = nzbinfo.score(nzbinfo.parse(data), t)
        except (ApiError, ET.ParseError) as e:
            warn(f"{r.indexer}: could not read the NZB ({e})")
            continue
        info(f"{r.indexer[:16]:<16} {gb(r.size):>9}  {sc.summary}")
        ranked.append((r, data, sc))
    ranked.sort(key=lambda x: (x[2].key, x[0].grabs or 0), reverse=True)
    return ranked + [(r, None, None) for r in uniq[MAX_PEEK:]]


def ledger_for(cfg: Config) -> Ledger:
    return Ledger(os.path.join(os.path.dirname(os.path.abspath(cfg.path)), "sab_jobs.json"))


_current_torrent = ""   # infohash of the torrent being built, recorded with each SABnzbd job


def sab_submit(cfg: Config, pr: Prowlarr, sab: SABnzbd, rel: Release, data: bytes | None, pp: int) -> str:
    nzb = data if data is not None else pr.fetch(rel)
    if b"<nzb" not in nzb[:4096].lower():
        raise ApiError(f"{rel.indexer} did not return an NZB for {rel.title}")
    nzo = sab.add_nzb(nzb, rel.title, cfg.sab_category, pp, cfg.sab_priority)
    ledger_for(cfg).record(nzo, rel.title, rel.guid, rel.indexer, rel.size, _current_torrent)
    mode = sab.ensure_pp(nzo, pp)
    info(f"{nzo}  {mode:<16} {rel.title}  ({rel.indexer}, {gb(rel.size)})")
    return nzo


PROBE_DIR = os.path.join(os.sep, "nonexistent-nzb2seed-probe")


def supplies(t: Torrent, d: str, target: str | None, unpack: bool = True) -> bool:
    """Does folder ``d`` hold ``target`` (a torrent file name), or - with target None - every
    file of the torrent? Checked by matching; if files are missing but the folder holds RAR
    archives that contain them, those files are unpacked into the folder first."""
    res = asm.assemble(t, [d], PROBE_DIR, dry_run=True, log=lambda *_: None)
    wanted = [f for f in res.missing if target is None or f.name == target]
    ok = not res.missing if target is None else any(os.path.basename(rel) == target for rel in res.placed)
    if ok or not unpack:
        return ok
    if unpack_from_archives(d, wanted):
        return supplies(t, d, target, unpack=False)
    return False


def unpack_from_archives(d: str, wanted: list) -> bool:
    """Extract the torrent files in ``wanted`` from RAR sets in download folder ``d``.
    A file is recognised by its exact size (and, for small files, its name too - posters
    often obfuscate the names of big ones). Returns True when anything was unpacked."""
    sets = archives.rar_sets(d)
    if not sets or not wanted:
        return False
    extracted = False
    for first in sets:
        name = os.path.basename(first)
        try:
            entries = archives.list_contents(first)
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            warn(f"cannot look inside {name}: {e}")
            continue
        members = []
        for f in wanted:
            for path, size in entries:
                same_name = os.path.basename(path.replace("\\", "/")).casefold() == f.name.casefold()
                if size == f.length and (same_name or f.length >= 1 << 20) and path not in members:
                    members.append(path)
                    break
        if not members:
            info(f"{name}: none of the needed files are inside ({len(entries)} file(s) in the archive)")
            continue
        info(f"{name} holds {len(members)} needed file(s): " + ", ".join(os.path.basename(m) for m in members[:3])
             + " - unpacking into the download folder")
        try:
            archives.extract(first, members, os.path.join(d, archives.UNPACK_DIR))
            extracted = True
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            warn(f"unpacking {name} failed: {e}")
    return extracted


def failed_before(cfg: Config, sab: SABnzbd, rel: Release) -> bool:
    """Did an earlier nzb2seed download of this same NZB fail in SABnzbd? Then it would
    fail again (the articles are gone), so it is skipped."""
    for j in ledger_for(cfg).matches(rel.title, rel.guid, rel.size):
        try:
            if sab.status(j["nzo"])[0] in SAB_FAILED:
                info(f"{rel.title} ({rel.indexer}): failed in SABnzbd before ({j['nzo']}) - skipping it")
                return True
        except ApiError:
            pass
    return False


def reuse(cfg: Config, sab: SABnzbd, rel: Release, ok) -> tuple[str, str | None] | None:
    """An earlier nzb2seed download of the same NZB that can be used instead of downloading
    it again: (nzo, folder) when finished and ``ok(folder)``, (nzo, None) while it is still
    downloading. None when there is nothing to reuse."""
    for j in ledger_for(cfg).matches(rel.title, rel.guid, rel.size):
        try:
            status, slot = sab.status(j["nzo"])
        except ApiError:
            continue
        if status.startswith("Queued:") or status not in SAB_DONE | SAB_FAILED | {"Unknown"}:
            info(f"{rel.title}: already downloading as {j['nzo']} - waiting for that instead")
            return j["nzo"], None
        if status not in SAB_DONE:
            continue
        try:
            d = job_dir(cfg, slot, j["title"])
        except Abort:
            continue                      # its files have been moved away since
        if ok(d):
            info(f"{rel.title}: reusing the finished download {j['nzo']} in {d}")
            return j["nzo"], d
    return None


def sab_wait(sab: SABnzbd, nzos: dict[str, str]) -> dict[str, tuple[str, dict]]:
    """Wait until every job has finished; return {nzo: (status, slot)}."""
    out: dict[str, tuple[str, dict]] = {}
    while len(out) < len(nzos):
        check_cancel()
        parts = []
        for nzo, title in nzos.items():
            if nzo in out:
                continue
            status, slot = sab.status(nzo)
            if status in SAB_DONE or status in SAB_FAILED:
                out[nzo] = (status, slot)
                msg = f"{status.lower()}: {title}"
                if slot.get("fail_message"):
                    msg += f" - {slot.get('fail_message')}"
                (info if status in SAB_DONE else warn)(msg)
            elif status.startswith("Queued:"):
                parts.append(f"{slot.get('percentage', '?')}% {slot.get('timeleft', '')}".strip())
            else:
                parts.append(status)
        if parts:
            progress(f"{len(out)}/{len(nzos)} done  " + "  ".join(parts))
        if len(out) < len(nzos):
            time.sleep(5)
    end_progress()
    return out


def job_dir(cfg: Config, slot: dict, title: str) -> str:
    storage = slot.get("storage") or ""
    local = map_path(storage, cfg.sab_to_local)
    if not os.path.exists(local):
        raise Abort(f"SABnzbd says the files are in {storage!r}, which maps to {local!r} here "
                    "but that does not exist - check the SABnzbd path translation in settings")
    if os.path.isfile(local):
        # SABnzbd reports the main file for some jobs; use its job folder, but only when the
        # folder really is this job's (never SABnzbd's shared complete folder)
        parent = os.path.dirname(local)
        if matching.norm(os.path.basename(parent)).startswith(matching.norm(title)):
            return parent
    return local


def missing_from(t: Torrent, d: str, output_dir: str) -> list:
    """Torrent files a finished job cannot supply (checked without touching anything)."""
    return asm.assemble(t, [d], output_dir, dry_run=True, log=lambda *_: None).missing


_EP = re.compile(r"(?<![a-z0-9])s\d{1,4}e\d{1,4}(?:-?e\d{1,4})*(?![0-9])")
_RES = re.compile(r"(?<![a-z0-9])(?:480|576|720|1080|2160)p(?![a-z0-9])")
MAX_INCOMPLETE = 3   # completed posts that turned out to lack files, before asking


def group_of(name: str) -> str | None:
    """Release group: the first dash-token after the episode/resolution part.
    'Show.S03E02.720p.HDTV.x264-GRP-xpost' -> 'grp', '...-grp.mkv' -> 'grp'."""
    n = matching.norm(name)
    anchor = max((m.end() for m in (_RES.search(n), _EP.search(n)) if m), default=0)
    i = n.find("-", anchor)
    if i < 0:
        return None
    return re.split(r"[-.\s\[\]()]", n[i + 1:])[0] or None


@dataclass
class Need:
    """What one Usenet download has to supply."""
    label: str                      # e.g. "S03E02" or the torrent name
    query: str                      # Prowlarr search text for alternatives
    group: str | None               # release group the alternative must come from
    res: str | None                 # resolution, if the name has one
    ep: str | None                  # episode token, e.g. "s03e02"
    min_size: int                   # an NZB smaller than this cannot hold the file(s)
    target: str                     # the torrent file (or torrent) it must supply
    results: list | None = None     # every Usenet result seen for this need
    level: str = "episode"          # "whole" | "season" | "episode": what kind of NZB fits
    season: int | None = None
    show: str = ""                  # normalised title prefix every fitting NZB starts with
    seasons: list = dataclasses.field(default_factory=list)   # for a multi-season whole


def need_for(t: Torrent, title: str, whole_torrent: bool) -> Need:
    """``whole_torrent``: one post must supply every file (single release); otherwise the
    post supplies the torrent file for ``title``'s episode (season pack from episodes)."""
    n = matching.norm(title)
    ep = _EP.search(n)
    ep_tok = ep.group(0) if ep else None
    target_file = None
    if ep_tok and not whole_torrent:
        files = [f for f in t.real_files if re.search(rf"(?<![a-z0-9]){ep_tok}(?![0-9])", matching.norm(f.name))]
        target_file = max(files, key=lambda f: f.length) if files else None
    if whole_torrent or target_file is None:
        main = max(t.real_files, key=lambda f: f.length)
        min_size, target = t.total_size, t.name
        group = group_of(main.name) or group_of(t.name) or group_of(title)
    else:
        min_size, target = target_file.length, target_file.name
        group = group_of(target_file.name) or group_of(title)
    res = _RES.search(n)
    if ep:
        query = n[:ep.end()]
    else:
        query = n[:n.find("-", res.end() if res else 0)] if "-" in n else n
    return Need(label=ep_tok.upper() if ep_tok else t.name, query=query.replace(".", " ").strip(),
                group=group, res=res.group(0) if res else None, ep=ep_tok,
                min_size=min_size, target=target)


def _fits(need: Need, r: Release) -> str | None:
    """Why ``r`` cannot be the alternative (None = it can)."""
    n = matching.norm(r.title)
    if need.show and not n.startswith(need.show):
        return "other title"
    if need.ep and not re.search(rf"(?<![a-z0-9]){need.ep}(?![0-9])", n):
        return "other episode"
    if not need.ep and _EP.search(n):
        return "single episode"
    if need.level == "season" and need.season is not None and \
            not re.search(rf"(?<![a-z0-9])s0*{need.season}(?![0-9e])", n):
        return "other season"
    if need.level == "whole" and len(need.seasons) > 1:
        tokens = set(re.findall(r"(?<![a-z0-9])s(\d{1,4})(?![0-9e])", n))
        if len(tokens) == 1 and not re.search(r"s\d{1,4}\.?-\.?s?\d{1,4}", n):
            return "single season"
    if need.res and need.res not in _RES.findall(n):
        return "other resolution"
    if need.group and group_of(r.title) != need.group:
        return "other release group"
    if r.size < need.min_size:
        return "smaller than the torrent's file - cannot hold it"
    return None


def usenet_results(cfg: Config, pr: Prowlarr, need: Need, known: list[Release]) -> list[Release]:
    """All Usenet results for the need's search, plus the ones already known; cached."""
    if need.results is None:
        found = [r for r in pr.search(need.query, cfg.indexer_ids, cfg.categories) if r.protocol == "usenet"]
        seen, out = set(), []
        for r in list(known) + found:
            if r.guid not in seen:
                seen.add(r.guid)
                out.append(r)
        need.results = out
    return need.results


def find_alternatives(cfg: Config, pr: Prowlarr, need: Need, tried: list[Release],
                      known: list[Release] = ()) -> list[Release]:
    """Other posts of the same release (same group, episode and resolution) that are at
    least as large as the file they must supply. Posts with a size already tried are
    skipped: the same post on another indexer fails the same way."""
    tried_guids = {r.guid for r in tried}
    tried_sizes = {r.size for r in tried}
    step(f"Looking for another {need.group.upper() if need.group else 'matching'} post of {need.label}"
         f" (at least {gb(need.min_size)})")
    out, sizes = [], set()
    for r in sorted(usenet_results(cfg, pr, need, known), key=lambda r: -(r.grabs or 0)):
        if r.guid in tried_guids or r.size in tried_sizes or r.size in sizes or _fits(need, r):
            continue
        sizes.add(r.size)
        out.append(r)
    for r in out:
        info(f"candidate: {r.title}  ({r.indexer}, {gb(r.size)}, {r.grabs} grabs)")
    if not out:
        warn(f"no other {need.group.upper() if need.group else ''} post of {need.label} is large enough")
    return out


def ask_for_post(cfg: Config, pr: Prowlarr, need: Need, tried: list[Release],
                 known: list[Release] = ()) -> Release | None:
    """Nothing fits automatically: show the search results from the same release group as
    the torrent's file (other groups can never match it byte for byte) and let the person
    pick. Nothing from that group left: stop."""
    tried_guids = {r.guid for r in tried}
    results = [r for r in usenet_results(cfg, pr, need, known)
               if r.guid not in tried_guids and (not need.group or group_of(r.title) == need.group)]
    if not results:
        warn(f"no other {need.group.upper() + ' ' if need.group else ''}posts of {need.label} to choose from")
        return None
    results.sort(key=lambda r: (r.size < need.min_size, -(r.grabs or 0)))
    choices = [{"title": r.title, "indexer": r.indexer, "size": r.size, "size_text": gb(r.size),
                "grabs": r.grabs, "note": _fits(need, r) or ""} for r in results]
    prompt = (f"No automatic replacement for {need.label}. Pick the {need.group.upper() + ' ' if need.group else ''}"
              f"NZB to download instead - it has to hold {need.target} ({gb(need.min_size)}).")
    idx = report_ask(prompt, choices)
    if idx is None:
        return None
    info(f"you picked: {results[idx].title} ({results[idx].indexer})")
    return results[idx]


def used_before(cfg: Config, need: "Need") -> list[Release]:
    """Posts nzb2seed downloaded earlier for this need (same episode and release group, or the
    same release for a whole torrent), as Releases, so a repair does not fetch them again."""
    out = []
    for j in Ledger(ledger_for(cfg).path)._load():
        title = j.get("title", "")
        n = matching.norm(title)
        if need.ep and not re.search(rf"(?<![a-z0-9]){need.ep}(?![0-9])", n):
            continue
        if need.group and group_of(title) != need.group:
            continue
        out.append(Release(title, "usenet", j.get("indexer", ""), 0, j.get("size") or 0,
                           j.get("guid") or j.get("nzo", ""), "", "", "", 0, None, None))
    return out


def placed_before(cfg: Config, opts: "Options", t: Torrent) -> set[str]:
    """Torrent files an earlier run of nzb2seed already put in place (right size, ours)."""
    out_dir = opts.output_dir or cfg.output_dir
    if not out_dir:
        return set()
    owned = owned_record(cfg, t)
    done = set()
    for f in t.real_files:
        p = asm.target_path(out_dir, f)
        if owned.owns(p) and os.path.isfile(p) and os.path.getsize(p) == f.length:
            done.add(f.relpath)
    return done


class Unit:
    """A part of the torrent that one NZB could supply: the whole torrent, one season of a
    multi-season torrent, or one episode. Units form a tree (whole -> seasons -> episodes);
    a unit is only split into its children when no NZB of its own kind works out."""

    def __init__(self, t: Torrent, title: str, level: str, files: list, show: str,
                 season: int | None = None, ep: str | None = None, seasons: list | None = None):
        self.level = level                      # "whole" | "season" | "episode"
        self.files = files
        self.children: list[Unit] = []
        self.queue: list[Release] = []
        self.seeded: list[Release] = []         # NZBs the person picked for exactly this unit
        self.tried: list[Release] = []
        self.searched = False
        self.done = False
        videos = [f for f in files if f.length >= 1 << 20] or files
        groups = {g for g in (group_of(f.name) for f in videos) if g}
        self.mixed = len(groups) > 1            # e.g. a pack with one episode from another group
        group = groups.pop() if len(groups) == 1 else (None if self.mixed else group_of(title))
        n = matching.norm(title)
        res = _RES.search(n) or next((m for m in (_RES.search(matching.norm(f.name)) for f in videos) if m), None)
        main = max(files, key=lambda f: f.length)
        if level == "episode":
            label, query = ep.upper(), f"{show} {ep}"
        elif level == "season":
            label, query = f"S{season:02d}", f"{show} s{season:02d}"
        else:
            label, query = t.name, n
        self.need = Need(label=label, query=query.replace(".", " ").strip(), group=group,
                         res=res.group(0) if res else None, ep=ep, min_size=sum(f.length for f in files),
                         target=main.name, level=level, season=season, show=show,
                         seasons=seasons or [])

    # the interface bad-piece repair and next_post() rely on
    @property
    def known(self) -> list[Release]:
        return self.seeded

    @property
    def whole(self) -> bool:
        return self.level == "whole"

    def covers(self, f) -> bool:
        return f.relpath in {x.relpath for x in self.files}

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()

    def satisfied_by(self, t: Torrent, d: str) -> bool:
        """Does download folder ``d`` hold every file of this unit? RAR archives in it are
        looked into (and the needed files unpacked) before answering no."""
        want = {f.relpath for f in self.files}
        found = set(asm.find_sources(t, [d])) & want
        if found == want:
            return True
        missing = [f for f in self.files if f.relpath not in found]
        if unpack_from_archives(d, missing):
            return want <= set(asm.find_sources(t, [d]))
        return False


def _season_episode(t: Torrent, f) -> tuple[int | None, str | None]:
    rel = "/".join(f.parts[1:]) if t.multi_file else f.name
    m = _EP.search(matching.norm(f.name)) or _EP.search(matching.norm(rel))
    if m:
        return int(re.match(r"s(\d+)", m.group(0)).group(1)), m.group(0)
    s = re.search(r"(?<![a-z0-9])(?:s|season\.?)(\d{1,4})(?![0-9e])", matching.norm(rel))
    return (int(s.group(1)) if s else None), None


def show_prefix(title: str) -> str:
    """'Show.2016.S03E02.720p...' -> 'show.2016.'; for a movie, up to the resolution."""
    n = matching.norm(title)
    m = _EP.search(n) or re.search(r"(?<![a-z0-9])s\d{1,4}(?![0-9])", n) or _RES.search(n)
    return n[:m.start()] if m else n.split("-")[0]


def build_units(t: Torrent, title: str) -> Unit:
    """The tree of units for a torrent: a movie or an episode is one unit; a season pack is a
    season with episodes under it; a multi-season pack is the whole with seasons under it."""
    show = show_prefix(title)
    info_ = {f.relpath: _season_episode(t, f) for f in t.real_files}
    seasons = sorted({s for s, _ in info_.values() if s is not None})
    eps = sorted({e for _, e in info_.values() if e})

    def episodes(files, season):
        out = []
        for e in sorted({info_[f.relpath][1] for f in files if info_[f.relpath][1]}):
            out.append(Unit(t, title, "episode", [f for f in files if info_[f.relpath][1] == e], show, season, e))
        return out

    if len(seasons) > 1:
        whole = Unit(t, title, "whole", list(t.real_files), show, seasons=seasons)
        for s in seasons:
            files = [f for f in t.real_files if info_[f.relpath][0] == s]
            su = Unit(t, title, "season", files, show, s)
            su.children = episodes(files, s)
            whole.children.append(su)
        return whole
    if len(seasons) == 1 and len(eps) > 1:
        whole = Unit(t, title, "season", list(t.real_files), show, seasons[0])
        whole.children = episodes(list(t.real_files), seasons[0])
        return whole
    if len(eps) == 1:
        return Unit(t, title, "episode", list(t.real_files), show, seasons[0], eps[0])
    return Unit(t, title, "whole", list(t.real_files), show)


def _rank(pr: Prowlarr, t: Torrent, unit: Unit, rels: list[Release]) -> list[Release]:
    """Order candidates for a season/whole unit by looking inside their NZBs (enough bytes,
    then how many of the unit's file names appear); episode candidates by popularity."""
    rels = sorted(rels, key=lambda r: -(r.grabs or 0))
    if unit.level == "episode" or len(rels) < 2:
        return rels
    scored, rest = [], []
    for r in rels:
        if len(scored) >= MAX_PEEK:
            rest.append(r)
            continue
        try:
            sc = nzbinfo.score(nzbinfo.parse(pr.fetch(r)), t, unit.files)
        except (ApiError, ET.ParseError):
            rest.append(r)
            continue
        info(f"{r.indexer[:16]:<16} {gb(r.size):>9}  {sc.summary}")
        scored.append((sc.key, r.grabs or 0, r))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [r for _, _, r in scored] + rest


def plan_and_fetch(cfg: Config, opts: Options, pr: Prowlarr, sab: SABnzbd, t: Torrent, title: str,
                   picks: list[Release], pp: int, placed: set[str] = frozenset()):
    """Get every file of the torrent from Usenet, trying the closest kind of NZB first.

    Returns (download folders, SABnzbd job ids, all units, rejected folders). A unit is
    tried with NZBs of its own kind - the person's picks for it first, then everything
    Prowlarr finds from the same release group and resolution - and only split into its
    children (seasons, then episodes) once all of those have been tried. Parts of a download that fell short are still used for
    the children they cover."""
    root = build_units(t, title)
    units = list(root.walk())

    # the person's picks go to the unit whose kind they are
    for p in picks:
        target = next((u for u in sorted(units, key=lambda u: {"episode": 0, "season": 1, "whole": 2}[u.level])
                       if _fits(dataclasses.replace(u.need, min_size=0, group=None), p) is None), root)
        target.seeded.append(p)

    dirs: list[str] = []
    nzos: list[str] = []
    partials: list[tuple[str, str]] = []        # (folder, title) of downloads that fell short
    pending: dict[str, tuple[Unit, Release]] = {}
    attempted: set[str] = set()                 # posts tried at any level of this build

    def resolve(u: Unit, d: str | None, nzo: str | None):
        u.done = True
        for x in u.walk():
            x.done = True
        if d and d not in dirs:
            dirs.append(d)
        if nzo:
            nzos.append(nzo)

    def seeded_below(u: Unit) -> bool:
        return any(c.seeded for c in u.walk() if c is not u)

    def next_candidate(u: Unit) -> Release | None:
        if not u.searched:
            u.searched = True
            kind = {"whole": "whole-torrent", "season": "season", "episode": "episode"}[u.level]
            info(f"{u.need.label}: looking for {kind} NZBs"
                 + (f" from {u.need.group.upper()}" if u.need.group else "")
                 + (f", {u.need.res}" if u.need.res else "") + f", at least {gb(u.need.min_size)}")
            found = [r for r in usenet_results(cfg, pr, u.need, u.seeded)
                     if r not in u.seeded and _fits(u.need, r) is None]
            small = [r for r in u.seeded if r.size < u.need.min_size]
            for r in small:
                info(f"{u.need.label}: skipping your pick {r.title} ({gb(r.size)}) - smaller than "
                     f"the {gb(u.need.min_size)} it has to hold")
            u.queue = [r for r in u.seeded if r not in small] + _rank(pr, t, u, found)
            if not u.queue:
                info(f"{u.need.label}: none found")
        tried_guids = {r.guid for r in u.tried} | attempted
        tried_sizes = {r.size for r in u.tried if r.size}
        while u.queue:
            r = u.queue.pop(0)
            if r.guid not in tried_guids and r.size not in tried_sizes:
                return r
        return None

    def split(u: Unit):
        kinds = {c.level for c in u.children}
        info(f"{u.need.label}: going to {'season' if 'season' in kinds else 'episode'} NZBs")
        for c in u.children:
            d = next((d for d, _ in partials if c.satisfied_by(t, d)), None)
            if d:
                info(f"{c.need.label}: already in an earlier download ({os.path.basename(d)})")
                resolve(c, d, None)
            else:
                start(c)

    def start(u: Unit):
        if u.done:
            return
        mine = {f.relpath for f in u.files}
        if mine <= placed:
            info(f"{u.need.label}: already in place from an earlier run - not downloading it again")
            u.tried = used_before(cfg, u.need)
            resolve(u, None, None)
            return
        if u.children and not u.seeded and (mine & placed or seeded_below(u) or u.mixed):
            split(u)
            return
        advance(u)

    def advance(u: Unit):
        # every post of this kind from the right group and resolution is tried before splitting
        while True:
            rel = next_candidate(u)
            if rel is None:
                if u.children:
                    split(u)
                    return
                rel = ask_for_post(cfg, pr, u.need, u.tried, u.seeded)
                if rel is None:
                    raise Abort(f"no Usenet post could supply {u.need.label}")
            u.tried.append(rel)
            attempted.add(rel.guid)
            if failed_before(cfg, sab, rel):
                continue
            again = reuse(cfg, sab, rel, lambda d: u.satisfied_by(t, d))
            if again and again[1]:
                resolve(u, again[1], again[0])
                return
            try:
                nzo = again[0] if again else sab_submit(cfg, pr, sab, rel, None, pp)
            except ApiError as e:
                warn(f"{rel.indexer}: {e}")
                continue
            pending[nzo] = (u, rel)
            return

    step("Finding the NZBs, closest match first")
    start(root)
    while pending:
        batch = dict(pending)
        pending.clear()
        results = sab_wait(sab, {nzo: rel.title for nzo, (_, rel) in batch.items()})
        for nzo, (status, slot_info) in results.items():
            u, rel = batch[nzo]
            if status in SAB_DONE:
                d = job_dir(cfg, slot_info, rel.title)
                if u.satisfied_by(t, d):
                    resolve(u, d, nzo)
                    continue
                warn(f"{rel.title} finished but does not hold all of {u.need.label}; "
                     "what it does hold is used if needed")
                partials.append((d, rel.title))
            else:
                warn(f"SABnzbd could not complete {rel.title}")
            advance(u)
    rejected = [(d, title_) for d, title_ in partials if d not in dirs]
    return dirs, nzos, units, rejected


def preview_plan(cfg: Config, pr: Prowlarr, t: Torrent, title: str) -> Unit:
    """Show which NZBs each level would try, without downloading or opening any NZB."""
    root = build_units(t, title)
    step("Plan: closest match first")

    def show(u: Unit, depth: int):
        pad = "  " * depth
        found = [r for r in usenet_results(cfg, pr, u.need, []) if _fits(u.need, r) is None]
        what = {"whole": "whole torrent", "season": "season", "episode": "episode"}[u.level]
        info(f"{pad}{u.need.label} ({what}, {len(u.files)} file(s), {gb(u.need.min_size)}"
             + (f", {u.need.group.upper()}" if u.need.group else "") + (", mixed groups" if u.mixed else "")
             + f"): {len(found)} NZB(s)")
        for r in sorted(found, key=lambda r: -(r.grabs or 0))[:5]:
            info(f"{pad}    {r.title}  ({r.indexer}, {gb(r.size)}, {r.grabs} grabs)")
        for c in u.children:
            show(c, depth + 1)
    show(root, 0)
    return root


def earlier_dirs(cfg: Config, sab: SABnzbd, t: Torrent) -> list[str]:
    """Folders of every finished download nzb2seed made for this torrent on any attempt:
    once the torrent is complete, their leftovers are cleaned up with the rest."""
    out = []
    for j in ledger_for(cfg).all():
        if j.get("torrent") != t.infohash:
            continue
        try:
            status, slot = sab.status(j["nzo"])
            if status in SAB_DONE:
                d = job_dir(cfg, slot, j.get("title", ""))
                if os.path.isdir(d) and d not in out:
                    out.append(d)
        except (ApiError, Abort):
            pass
    return out


def usenet_single(cfg: Config, opts: Options, pr: Prowlarr, sab: SABnzbd, t: Torrent,
                  group: list[Release], pp: int, rejected: list | None = None,
                  slots: list | None = None) -> tuple[list[str], list[str]]:
    """Build from one release (e.g. a movie or an episode) - see plan_and_fetch."""
    dirs, nzos, units, rej = plan_and_fetch(cfg, opts, pr, sab, t, t.name, list(group), pp)
    if slots is not None:
        slots.extend(units)
    if rejected is not None:
        rejected.extend(rej)
    return dirs, nzos


def usenet_multi(cfg: Config, pr: Prowlarr, sab: SABnzbd, t: Torrent, groups: list[list[Release]],
                 pp: int, slots: list | None = None, placed: set[str] = frozenset()
                 ) -> tuple[list[str], list[str]]:
    """Build from several picked releases (e.g. episodes of a pack) - see plan_and_fetch."""
    picks = [r for g in groups for r in g]
    dirs, nzos, units, _ = plan_and_fetch(cfg, Options(), pr, sab, t, t.name, picks, pp, placed)
    if slots is not None:
        slots.extend(units)
    return dirs, nzos


def next_post(cfg: Config, pr: Prowlarr, slot) -> Release | None:
    """The next post to try for a unit when repairing: queued ones, then other posts of the
    same release group (searched once), then whatever the person picks."""
    while True:
        while slot.queue:
            r = slot.queue.pop(0)
            if r.guid not in {x.guid for x in slot.tried} and r.size not in {x.size for x in slot.tried if x.size}:
                return r
        if not slot.searched:
            slot.searched = True
            slot.queue = find_alternatives(cfg, pr, slot.need, slot.tried, slot.known)
            if slot.queue:
                continue
        return ask_for_post(cfg, pr, slot.need, slot.tried, slot.known)


def remove_rejected(rejected: list[tuple[str, str]]):
    """Delete downloads of posts that fell short, now that the torrent is complete. Only
    job folders named after the release are removed - never a shared folder."""
    step("Removing posts that were tried and not used")
    for d, title in rejected:
        if os.path.isdir(d) and matching.norm(os.path.basename(d)).startswith(matching.norm(title)):
            shutil.rmtree(d)
            info(f"removed {d}")
        elif os.path.isfile(d):
            os.remove(d)
            info(f"removed {d}")


# ---------------------------------------------------------------- earlier downloads for a torrent

def saved_torrent(cfg: Config, title: str) -> Torrent | None:
    """The .torrent an earlier build saved for this release, if there is one."""
    want = matching.norm(title)
    try:
        names = os.listdir(cfg.torrent_dir)
    except OSError:
        return None
    for n in names:
        if n.endswith(".torrent") and matching.norm(n[:-8]) == want:
            try:
                with open(os.path.join(cfg.torrent_dir, n), "rb") as fh:
                    return parse(fh.read())
            except (OSError, ValueError):
                return None
    return None


def previous_downloads(cfg: Config, sab: SABnzbd, title: str) -> list[dict]:
    """NZBs nzb2seed downloaded on earlier attempts at this torrent, and what became of them.

    Matched by the torrent they were downloaded for, or by show + episode and the release
    group of the torrent's own file for that episode (from the saved .torrent), or - with no
    saved .torrent - by show + episode/season."""
    t = saved_torrent(cfg, title)
    n = matching.norm(title)
    m = _EP.search(n) or re.search(r"(?<![a-z0-9])s\d{1,4}(?![0-9e])", n)
    show = n[:m.start()] if m else n.split("-")[0]
    wanted: dict[str, str | None] = {}           # episode token -> release group (None = any)
    if t:
        for f in t.real_files:
            e = _EP.search(matching.norm(f.name))
            if e:
                wanted[e.group(0)] = group_of(f.name)
    out = []
    for j in ledger_for(cfg).all():
        jt = j.get("title", "")
        jn = matching.norm(jt)
        mine = t is not None and j.get("torrent") == t.infohash
        if not mine:
            if not jn.startswith(show) or not show:
                continue
            e = _EP.search(jn)
            if wanted:
                if not e or e.group(0) not in wanted:
                    continue
                g = wanted[e.group(0)]
                if g and group_of(jt) != g:
                    continue
            elif m and m.group(0) not in jn:
                continue
        state, note = "unknown", ""
        try:
            status, slot = sab.status(j["nzo"])
        except ApiError:
            status, slot = "Unknown", {}
        if status in SAB_FAILED:
            state, note = "failed", "failed in SABnzbd - skipped automatically"
        elif status.startswith("Queued:") or status not in SAB_DONE | {"Unknown"}:
            state, note = "downloading", f"still downloading ({slot.get('percentage', '?')}%)"
        elif status in SAB_DONE:
            try:
                d = job_dir(cfg, slot, jt)
                has = any(os.path.getsize(os.path.join(r, x)) >= 1 << 20
                          for r, _, xs in os.walk(d) for x in xs)
            except (Abort, OSError):
                has = False
            state, note = ("finished", "finished - reused automatically if it holds what the torrent needs") \
                if has else ("used", "finished - its files were moved into a torrent or removed")
        else:
            state, note = "gone", "no longer in SABnzbd"
        out.append({"title": jt, "indexer": j.get("indexer", ""), "size": j.get("size") or 0,
                    "nzo": j["nzo"], "state": state, "note": note,
                    "submitted": j.get("submitted")})
    return out


# ---------------------------------------------------------------- fixing pieces that fail

@dataclass
class Retry:
    """What bad-piece repair needs to fetch other posts."""
    cfg: Config
    pr: Prowlarr
    sab: SABnzbd
    pp: int
    slots: list


def inner_pieces(t: Torrent, f) -> range:
    """Pieces lying entirely inside file ``f``."""
    first = -(-f.offset // t.piece_length)
    last = (f.offset + f.length) // t.piece_length - 1
    return range(first, last + 1) if last >= first else range(0)


def copy_verifies(t: Torrent, f, path: str, placed: dict) -> bool:
    """Do all pieces inside ``f`` verify when ``f`` is read from ``path``?"""
    rng = inner_pieces(t, f)
    with PieceVerifier(t, lambda ff: path if ff.relpath == f.relpath else placed.get(ff.relpath)) as pv:
        for n, i in enumerate(rng):
            if n % 32 == 0:
                check_cancel()
                progress(f"checking {os.path.basename(path)}: {n}/{len(rng)} pieces")
            if not pv.check(i):
                end_progress()
                return False
    end_progress()
    return True


def repair_bad_pieces(rt: Retry, t: Torrent, res, bad: list[int], owned, job_dirs: list[str]) -> bool:
    """Replace the files that failing pieces point to with copies from other posts of the
    same release until every piece verifies. Only files nzb2seed placed are replaced; a
    copy is only swapped in when it makes the failing piece(s) pass. True = all fixed."""
    file_slot = {}
    for sl in rt.slots:
        sl.searched = False               # allow a fresh search for alternatives
    # the finest unit holding a file claims it: an episode NZB is the cheapest replacement
    order = {"episode": 0, "season": 1, "whole": 2}
    for sl in sorted(rt.slots, key=lambda x: order.get(x.level, 3)):
        for f in t.real_files:
            if sl.covers(f):
                file_slot.setdefault(f.relpath, sl)
    pool: dict[str, list] = {}            # relpath -> [(path, release)] copies whose insides verify
    exhausted: set[str] = set()
    by_rel = {f.relpath: f for f in t.real_files}

    def piece_ok(i: int, choice: dict) -> bool:
        with PieceVerifier(t, lambda ff: choice.get(ff.relpath, res.placed.get(ff.relpath))) as pv:
            return pv.check(i)

    def suspects(i: int) -> list:
        start, end = t.piece_span(i)
        fs = [f for f in t.real_files if f.offset < end and f.offset + f.length > start]
        # a file starting inside the piece first (re-muxed headers), then the others
        return sorted(fs, key=lambda f: (not (start <= f.offset < end), f.offset))

    def swap(rel: str, path: str, used: Release):
        dst = res.placed[rel]
        os.remove(dst)
        owned.forget_file(dst)
        asm._move(path, dst, owned)
        owned.save()
        info(f"replaced {by_rel[rel].name} with the copy from {used.title} ({used.indexer})")

    def fetch_copy(f) -> tuple[str, Release] | None:
        slot = file_slot[f.relpath]
        while True:
            rel = next_post(rt.cfg, rt.pr, slot)
            if rel is None:
                return None
            slot.tried.append(rel)
            if failed_before(rt.cfg, rt.sab, rel):
                continue
            step(f"Trying another copy of {f.name}: {rel.title} ({rel.indexer})")
            again = reuse(rt.cfg, rt.sab, rel, lambda d: supplies(t, d, f.name))
            if again and again[1]:
                d = again[1]
            else:
                try:
                    nzo = again[0] if again else sab_submit(rt.cfg, rt.pr, rt.sab, rel, None, rt.pp)
                except ApiError as e:
                    warn(f"{rel.indexer}: {e}")
                    continue
                status, slot_info = sab_wait(rt.sab, {nzo: rel.title})[nzo]
                if status not in SAB_DONE:
                    continue
                d = job_dir(rt.cfg, slot_info, rel.title)
            if d not in job_dirs:
                job_dirs.append(d)        # ours: cleaned up with the rest
            src = asm.find_sources(t, [d]).get(f.relpath)
            if src is None and supplies(t, d, f.name):
                src = asm.find_sources(t, [d]).get(f.relpath)
            if src is None:
                warn(f"{rel.title} does not hold {f.name}")
                continue
            if not copy_verifies(t, f, src, res.placed):
                warn(f"the copy of {f.name} in {rel.title} differs inside the file too")
                continue
            info(f"the copy of {f.name} in {rel.title} verifies inside the file")
            return src, rel

    pending = sorted(set(bad))
    while pending:
        check_cancel()
        i = pending[0]
        fs = suspects(i)
        changeable = [f for f in fs if f.relpath in file_slot and owned.owns(res.placed[f.relpath])]
        options = [[None] + pool.get(f.relpath, []) if f in changeable else [None] for f in fs]
        found = None
        for combo in itertools.product(*options):
            choice = {f.relpath: c for f, c in zip(fs, combo) if c}
            if choice and piece_ok(i, {r: c[0] for r, c in choice.items()}):
                found = choice
                break
        if found:
            for rel, (path, used) in found.items():
                swap(rel, path, used)
                pool[rel] = [c for c in pool.get(rel, []) if c[0] != path]
            pending = [j for j in pending if not piece_ok(j, {})]
            info(f"piece {i} verifies now; {len(pending)} failing piece(s) left")
            continue
        todo = [f for f in changeable if f.relpath not in exhausted]
        if not todo:
            names = ", ".join(f.name for f in fs)
            warn(f"piece {i} still fails and there are no more copies of {names} to try")
            return False
        target = min(todo, key=lambda f: (len(pool.get(f.relpath, [])), changeable.index(f)))
        got = fetch_copy(target)
        if got is None:
            exhausted.add(target.relpath)
        else:
            pool.setdefault(target.relpath, []).append(got)
    return True


# ---------------------------------------------------------------- torrent side

def save_torrent(cfg: Config, data: bytes) -> tuple[Torrent, str]:
    t = parse(data)
    os.makedirs(cfg.torrent_dir, exist_ok=True)
    safe = re.sub(r'[<>:"/\\|?*]', "_", t.name)
    path = os.path.join(cfg.torrent_dir, f"{safe}.torrent")
    with open(path, "wb") as fh:
        fh.write(data)
    info(f"{t.name}: {len(t.real_files)} file(s), {gb(t.total_size)}, "
         f"piece {t.piece_length // 1024} KiB, infohash {t.infohash}"
         + (", private" if t.private else ""))
    info(f"saved {path}")
    return t, path


def show_layout(t: Torrent):
    dirs = sorted({"/".join(f.parts[1:-1]) for f in t.real_files if len(f.parts) > 2})
    if dirs:
        info("torrent subfolders: " + ", ".join(dirs))


def qbit_preflight(qb: QBittorrent, t: Torrent):
    qb.login()
    info(f"qBittorrent {qb.app_version()} (WebAPI {'.'.join(map(str, qb.api_version()))})")
    try:
        prefs = qb.preferences()
        if prefs.get("incomplete_files_ext"):
            warn("qBittorrent appends .!qB to incomplete files; the recheck still finds complete "
                 "files, but disable it if you see files renamed")
    except ApiError:
        pass
    existing = qb.info(t.infohash)
    if existing and existing.get("state") not in STOPPED:
        raise Abort(f"this torrent is already in qBittorrent and active ({existing.get('state')}); "
                    "stop it first so it cannot download anything")
    return existing


def _cancel_tick(_t):
    check_cancel()


def owned_record(cfg: Config, t: Torrent) -> asm.Owned:
    return asm.Owned(os.path.join(cfg.torrent_dir, f"{t.infohash}.owned.json"))


def finish(cfg: Config, opts: Options, t: Torrent, torrent_path: str, source_dirs: list[str],
           output_dir: str, qb: QBittorrent | None, existing: dict | None,
           sources_are_ours: bool = True, retry: Retry | None = None,
           earlier_dirs: list[str] = ()) -> dict:
    """``sources_are_ours``: the source folders are SABnzbd jobs this build submitted, so
    their leftovers may be deleted. False for folders the user pointed at (assemble)."""
    step(f"Laying out files for the torrent in {output_dir}")
    for d in source_dirs:
        info(f"from {d}")
    show_layout(t)
    last = [0.0]

    def copying(name, done, total):
        # SSD -> HDD copies; deliberately no cancel check here, a file is never left half-moved
        now = time.monotonic()
        if done == total:
            progress(f"copied {name} ({gb(total)}) to the other disk")
            end_progress()
        elif now - last[0] > 0.5:
            last[0] = now
            progress(f"copying {name}: {gb(done)} of {gb(total)}")
    owned = owned_record(cfg, t)
    try:
        res = asm.assemble(t, source_dirs, output_dir, dry_run=opts.dry_run, log=line,
                           progress=copying, owned=owned)
    finally:
        if not opts.dry_run:
            owned.save()
    for f, p in res.conflicts:
        warn(f"IN THE WAY  {p} already exists with a different size and was not created by "
             "nzb2seed; it is left untouched")
    if res.conflicts:
        raise Abort("files that nzb2seed did not create are where the torrent's files must go; "
                    "nothing was moved or deleted")
    for n in res.notes:
        warn(n)
    for f in res.missing:
        warn(f"MISSING  {f.relpath} ({gb(f.length)})")
    for f, size in res.wrong_size:
        warn(f"WRONG SIZE  {f.relpath}: have {size}, need {f.length}")
    if opts.dry_run:
        info("dry run: nothing moved")
        asm.cleanup(t, res, source_dirs if sources_are_ours else [], output_dir, owned,
                    dry_run=True, log=line)
        return {"result": "dry run"}
    if not res.complete:
        raise Abort("the NZB download(s) do not contain every file of the torrent; not adding it "
                    "(nothing deleted; try post-processing 'unpack' if the post was packed differently)")
    info(f"all {len(t.real_files)} file(s) present with the right size")

    retry_on = retry is not None and (cfg.retry_bad_pieces if opts.retry_bad is None else opts.retry_bad)
    if opts.local_verify or cfg.local_verify or retry_on:
        step("Hashing all pieces locally")

        def tick(i, n, bad_so_far):
            check_cancel()
            progress(f"{i}/{n} pieces")
            m = bytearray(b"#" * i + b"." * (n - i))
            for b in bad_so_far:
                m[b] = ord("x")
            pieces(m.decode())
        bad = asm.verify_all(t, res, tick)
        end_progress()
        if bad:
            for f in asm.files_for_pieces(t, bad):
                warn(f"bad pieces in {f.relpath}")
            if not retry_on:
                raise Abort(f"{len(bad)} of {len(t.pieces)} pieces fail locally; not adding the torrent "
                            "(turn on 'try other posts' to replace the files automatically)")
            step(f"Replacing files behind {len(bad)} failing piece(s) with other posts")
            if not repair_bad_pieces(retry, t, res, bad, owned, source_dirs):
                raise Abort(f"pieces still fail after trying the other posts; not adding the torrent")
            pieces("#" * len(t.pieces))
        info("100% of pieces verified locally")

    if cfg.cleanup and not opts.no_cleanup:
        step("Removing files that are not part of the torrent")
        ours = (list(source_dirs) + [d for d in earlier_dirs if d not in source_dirs]) if sources_are_ours else []
        removed = asm.cleanup(t, res, ours, output_dir, owned, log=line)
        owned.save()
        info(f"{len(removed)} file(s) removed" + ("" if sources_are_ours else
             " (your source folders are left as they are)"))

    if qb is None:
        info("qBittorrent step skipped")
        return {"result": "files ready", "progress": None}

    save_path = map_path(output_dir, cfg.local_to_qbit)
    step("Adding to qBittorrent (stopped)")
    if existing is None:
        with open(torrent_path, "rb") as fh:
            qb.add_stopped(fh.read(), os.path.basename(torrent_path), save_path,
                           cfg.qbit_category, ",".join(cfg.qbit_tags))
        tinfo = None
        for _ in range(20):
            tinfo = qb.info(t.infohash)
            if tinfo:
                break
            time.sleep(0.5)
        if not tinfo:
            raise Abort("qBittorrent accepted the torrent but it does not show up")
    else:
        info("already in qBittorrent (stopped); reusing it")
        tinfo = existing
    if tinfo.get("state") not in STOPPED and tinfo.get("state") not in ("checkingUP", "checkingDL",
                                                                         "checkingResumeData"):
        warn(f"torrent came up as {tinfo.get('state')}; stopping it")
        qb.stop(t.infohash)
    qb.wait_idle(t.infohash, on_tick=_cancel_tick)

    step("Pointing qBittorrent at the data")
    tinfo = qb.info(t.infohash)
    if tinfo.get("save_path", "").rstrip("/\\") != save_path.rstrip("/\\"):
        info(f"{tinfo.get('save_path')} -> {save_path}")
        qb.set_location(t.infohash, save_path)
        qb.wait_idle(t.infohash, on_tick=_cancel_tick)
    info(f"save path: {save_path}")

    step("Force recheck")
    qb.recheck(t.infohash)

    def piece_map(final=False):
        try:
            states = qb.piece_states(t.infohash)
        except ApiError:
            return
        pieces("".join("#" if p == 2 else ("x" if final else ".") for p in states))

    def tick(ti):
        check_cancel()
        progress(f"{ti.get('state')}: {ti.get('progress', 0) * 100:.1f}%")
        piece_map()
    tinfo = qb.wait_idle(t.infohash, on_tick=tick, min_wait=5)
    end_progress()
    piece_map(final=True)
    pct = tinfo.get("progress", 0) * 100
    if tinfo.get("progress", 0) < 1:
        for f in qb.files(t.infohash):
            if f.get("progress", 0) < 1:
                warn(f"{f.get('progress', 0) * 100:5.1f}%  {f.get('name')}")
        raise Abort(f"recheck finished at {pct:.1f}%; the torrent stays stopped (no download, no H&R)")
    info("recheck: 100.0% - complete, built entirely from Usenet")

    if opts.start or cfg.start_when_complete:
        qb.start(t.infohash)
        info("started seeding")
        state = "seeding"
    else:
        info("left stopped; start it in qBittorrent to seed")
        state = "stopped"
    return {"result": f"100.0% ({state})", "progress": 1.0, "infohash": t.infohash}


# ---------------------------------------------------------------- whole runs

def local_release(t: Torrent, path: str) -> Release:
    """A Release standing in for a .torrent file the user already has."""
    return Release(title=t.name, protocol="torrent", indexer=os.path.basename(path), indexer_id=0,
                   size=t.total_size, guid=f"file:{t.infohash}", download_url="", info_url="",
                   publish_date="", grabs=None, seeders=None, files=len(t.real_files))


def execute_run(cfg: Config, opts: Options, tor_rel: Release, groups: list[list[Release]],
                torrent_data: bytes | None = None) -> dict:
    """Everything after the releases have been chosen. ``torrent_data``: a .torrent the
    user supplied, used instead of downloading one."""
    pr = Prowlarr(cfg.prowlarr_url, cfg.prowlarr_key)
    sab = SABnzbd(cfg.sab_url, cfg.sab_key)
    qb = None if opts.no_qbit else QBittorrent(cfg.qbit_url, cfg.qbit_user, cfg.qbit_pass)
    info(f"SABnzbd {sab.version()}")

    if torrent_data is not None:
        step(f"Using .torrent file {tor_rel.indexer}")
        t, path = save_torrent(cfg, torrent_data)
    else:
        step(f"Downloading .torrent from {tor_rel.indexer}")
        t, path = save_torrent(cfg, pr.fetch(tor_rel))
    show_layout(t)
    global _current_torrent
    _current_torrent = t.infohash
    existing = qbit_preflight(qb, t) if qb else None

    pp = choose_pp(opts, cfg, t)
    mode = opts.pp or cfg.post_processing
    why = "as configured" if mode in ("repair", "unpack") else \
        ("the torrent holds RAR archives" if pp == PP_REPAIR else "the torrent holds unpacked files")
    info(f"SABnzbd post-processing: {'+Repair' if pp == PP_REPAIR else '+Repair/Unpack'} ({why}); never +Delete")
    sab_preflight(sab, pp)

    placed = placed_before(cfg, opts, t)
    picks = [r for g in groups for r in g]
    dirs, nzos, slots, rejected = plan_and_fetch(cfg, opts, pr, sab, t, t.name, picks, pp, placed)
    if not dirs and not placed:
        raise Abort("nothing was downloaded")

    output_dir = opts.output_dir or cfg.output_dir or os.path.dirname(os.path.abspath(dirs[0]))
    out = finish(cfg, opts, t, path, dirs, output_dir, qb, existing,
                 retry=Retry(cfg, pr, sab, pp, slots), earlier_dirs=earlier_dirs(cfg, sab, t))
    if rejected and cfg.cleanup and not opts.no_cleanup and not opts.dry_run:
        remove_rejected(rejected)
    if cfg.sab_delete_history:
        for nzo in nzos:
            sab.delete_history(nzo)
    return out


def execute_assemble(cfg: Config, opts: Options, torrent_file: str, sources: list[str]) -> dict:
    with open(torrent_file, "rb") as fh:
        data = fh.read()
    step(f"Reading {torrent_file}")
    t, path = save_torrent(cfg, data) if not opts.dry_run else (parse(data), torrent_file)
    dirs = [os.path.abspath(d) for d in sources]
    for d in dirs:
        if not os.path.exists(d):
            raise Abort(f"{d} does not exist")
    output_dir = opts.output_dir or cfg.output_dir or os.path.dirname(dirs[0])
    qb = existing = None
    if not opts.no_qbit and not opts.dry_run:
        qb = QBittorrent(cfg.qbit_url, cfg.qbit_user, cfg.qbit_pass)
        existing = qbit_preflight(qb, t)
    return finish(cfg, opts, t, path, dirs, output_dir, qb, existing, sources_are_ours=False)
