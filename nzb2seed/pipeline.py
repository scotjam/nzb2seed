"""The build pipeline, shared by the CLI and the GUI.

Output goes through ``report`` so the same code can print to a console or
feed a GUI job log.
"""
from __future__ import annotations

import os
import re
import shutil
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from . import assemble as asm
from . import matching, nzbinfo
from .clients import (PP_REPAIR, PP_UNPACK, SAB_DONE, SAB_FAILED, ApiError,
                      Prowlarr, QBittorrent, Release, SABnzbd)
from .config import Config
from .pathmap import map_path
from .report import check_cancel, end_progress, info, line, pieces, progress, step, warn
from .torrent import Torrent, parse

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
    eps = matching.episode_matches(torrent, nzbs)
    q = matching.episode_query(torrent.title)
    if not eps and q:
        info(f"no exact usenet match; searching for episodes: {q}")
        more = [r for r in pr.search(q, cfg.indexer_ids, cfg.categories) if r.protocol == "usenet"]
        known = {n.guid for n in nzbs}
        nzbs = nzbs + [m for m in more if m.guid not in known]
        eps = matching.episode_matches(torrent, nzbs)
    if eps:
        return [[e] for e in eps], nzbs, f"season pack from {len(eps)} episode NZB(s)"
    return [], nzbs, "no usenet release matches the torrent name"


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
MAX_ATTEMPTS = 3    # different posts downloaded before giving up
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


def sab_submit(cfg: Config, pr: Prowlarr, sab: SABnzbd, rel: Release, data: bytes | None, pp: int) -> str:
    nzb = data if data is not None else pr.fetch(rel)
    if b"<nzb" not in nzb[:4096].lower():
        raise ApiError(f"{rel.indexer} did not return an NZB for {rel.title}")
    nzo = sab.add_nzb(nzb, rel.title, cfg.sab_category, pp, cfg.sab_priority)
    mode = sab.ensure_pp(nzo, pp)
    info(f"{nzo}  {mode:<16} {rel.title}  ({rel.indexer}, {gb(rel.size)})")
    return nzo


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


def usenet_single(cfg: Config, opts: Options, pr: Prowlarr, sab: SABnzbd, t: Torrent,
                  group: list[Release], pp: int, rejected: list | None = None
                  ) -> tuple[list[str], list[str]]:
    """One release (possibly posted several times): try the best posts in turn until one
    holds every file of the torrent. Folders of posts that fell short go into ``rejected``."""
    step("Choosing the Usenet post")
    ranked = rank_posts(pr, t, group)
    if not ranked:
        raise Abort(f"no NZB is at least as large as the torrent ({gb(t.total_size)})")
    attempts = min(MAX_ATTEMPTS, len(ranked))
    tried = 0
    for rel, data, _ in ranked[:attempts]:
        tried += 1
        step(f"Downloading from Usenet (post {tried} of up to {attempts})")
        try:
            nzo = sab_submit(cfg, pr, sab, rel, data, pp)
        except ApiError as e:
            warn(f"{rel.indexer}: {e}")
            continue
        status, slot = sab_wait(sab, {nzo: rel.title})[nzo]
        if status not in SAB_DONE:
            continue
        d = job_dir(cfg, slot, rel.title)
        info(f"{nzo}: {d}")
        out = opts.output_dir or cfg.output_dir or os.path.dirname(os.path.abspath(d))
        missing = missing_from(t, d, out)
        if not missing:
            return [d], [nzo]
        warn(f"this post lacks {len(missing)} of the torrent's files: "
             + ", ".join(f.name for f in missing[:4]) + ("..." if len(missing) > 4 else ""))
        warn(f"left as downloaded in {d}")
        if rejected is not None:
            rejected.append((d, rel.title))
    raise Abort(f"none of the {tried} post(s) tried contains every file of the torrent")


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


def usenet_multi(cfg: Config, pr: Prowlarr, sab: SABnzbd, groups: list[list[Release]],
                 pp: int) -> tuple[list[str], list[str]]:
    """Several releases (e.g. the episodes of a season pack), all needed."""
    step(f"Downloading {len(groups)} NZBs from Usenet")
    jobs = {}   # nzo -> [group, index]
    for g in groups:
        for i, rel in enumerate(g):
            try:
                jobs[sab_submit(cfg, pr, sab, rel, None, pp)] = [g, i]
                break
            except ApiError as e:
                warn(f"{rel.indexer}: {e}")
        else:
            raise Abort(f"could not queue any NZB for {g[0].title}")
    dirs, done = [], []
    pending = {nzo: g[i].title for nzo, (g, i) in jobs.items()}
    while pending:
        results = sab_wait(sab, pending)
        pending = {}
        for nzo, (status, slot) in results.items():
            g, i = jobs[nzo]
            if status in SAB_DONE:
                dirs.append(job_dir(cfg, slot, g[i].title))
                done.append(nzo)
                continue
            if i + 1 >= len(g):
                raise Abort(f"SABnzbd could not complete {g[i].title}")
            info(f"trying {g[i + 1].indexer} for {g[i + 1].title}")
            new = sab_submit(cfg, pr, sab, g[i + 1], None, pp)
            jobs[new] = [g, i + 1]
            pending[new] = g[i + 1].title
    return dirs, done


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
           sources_are_ours: bool = True) -> dict:
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

    if opts.local_verify or cfg.local_verify:
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
            raise Abort(f"{len(bad)} of {len(t.pieces)} pieces fail locally; not adding the torrent")
        info("100% of pieces verified locally")

    if cfg.cleanup and not opts.no_cleanup:
        step("Removing files that are not part of the torrent")
        removed = asm.cleanup(t, res, source_dirs if sources_are_ours else [], output_dir, owned, log=line)
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

def execute_run(cfg: Config, opts: Options, tor_rel: Release, groups: list[list[Release]]) -> dict:
    """Everything after the releases have been chosen."""
    pr = Prowlarr(cfg.prowlarr_url, cfg.prowlarr_key)
    sab = SABnzbd(cfg.sab_url, cfg.sab_key)
    qb = None if opts.no_qbit else QBittorrent(cfg.qbit_url, cfg.qbit_user, cfg.qbit_pass)
    info(f"SABnzbd {sab.version()}")

    step(f"Downloading .torrent from {tor_rel.indexer}")
    t, path = save_torrent(cfg, pr.fetch(tor_rel))
    show_layout(t)
    existing = qbit_preflight(qb, t) if qb else None

    pp = choose_pp(opts, cfg, t)
    mode = opts.pp or cfg.post_processing
    why = "as configured" if mode in ("repair", "unpack") else \
        ("the torrent holds RAR archives" if pp == PP_REPAIR else "the torrent holds unpacked files")
    info(f"SABnzbd post-processing: {'+Repair' if pp == PP_REPAIR else '+Repair/Unpack'} ({why}); never +Delete")
    sab_preflight(sab, pp)

    rejected: list = []
    if len(groups) == 1:
        dirs, nzos = usenet_single(cfg, opts, pr, sab, t, groups[0], pp, rejected)
    else:
        size_warning(tor_rel, groups)
        dirs, nzos = usenet_multi(cfg, pr, sab, groups, pp)

    output_dir = opts.output_dir or cfg.output_dir or os.path.dirname(os.path.abspath(dirs[0]))
    out = finish(cfg, opts, t, path, dirs, output_dir, qb, existing)
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
