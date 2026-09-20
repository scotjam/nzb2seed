"""Lay out NZB-downloaded files exactly as a torrent expects them.

Matching order for every file in the torrent (padding files skipped):
  1. same file name (case-insensitive) and same size
  2. same size, when only one unused candidate has that size
  3. same size, disambiguated by hashing a piece that lies wholly inside the file
.nfo files are matched by name (or by being the only .nfo) and have their line
endings rewritten to whichever variant reproduces the torrent's byte size; if
several variants fit, the one whose overlapping pieces hash correctly wins.

Files end up at ``<output_dir>/<torrent path>``, which puts Sample/, Proof/,
Subs/ etc. into the same subfolders the torrent uses.
"""
from __future__ import annotations

import errno
import itertools
import json
import os
import shutil
from dataclasses import dataclass, field

from .torrent import PieceVerifier, TFile, Torrent

# Small text files whose line endings can differ between the Usenet post and the torrent.
TEXT_EXTS = {".nfo", ".sfv", ".txt", ".md5", ".srt"}
TEXT_MAX = 4 << 20
MAX_COMBOS = 20000


def is_text(f: TFile) -> bool:
    return f.ext in TEXT_EXTS and f.length < TEXT_MAX


def _key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


@dataclass
class Source:
    path: str
    size: int

    @property
    def name(self) -> str:
        return os.path.basename(self.path)


@dataclass
class Result:
    placed: dict[str, str] = field(default_factory=dict)       # tfile.relpath -> final path
    how: dict[str, str] = field(default_factory=dict)          # tfile.relpath -> how it was matched
    missing: list[TFile] = field(default_factory=list)
    wrong_size: list[tuple[TFile, int]] = field(default_factory=list)
    conflicts: list[tuple[TFile, str]] = field(default_factory=list)  # someone else's file in the way
    notes: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.missing and not self.wrong_size and not self.conflicts


class Owned:
    """What nzb2seed itself put on disk for one torrent (files and folders), kept in a
    small JSON file so re-runs and cleanups only ever touch nzb2seed's own work."""

    def __init__(self, path: str | None = None):
        self.path = path
        self.files: set[str] = set()
        self.dirs: set[str] = set()
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
            self.files = set(d.get("files", []))
            self.dirs = set(d.get("dirs", []))

    def owns(self, p: str) -> bool:
        return _key(p) in {_key(x) for x in self.files}

    def add_file(self, p: str):
        self.files.add(os.path.abspath(p))

    def forget_file(self, p: str):
        self.files = {x for x in self.files if _key(x) != _key(p)}

    def makedirs(self, d: str):
        """os.makedirs, remembering every folder it had to create."""
        d = os.path.abspath(d)
        todo = []
        while not os.path.isdir(d):
            todo.append(d)
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
        for x in reversed(todo):
            os.makedirs(x, exist_ok=True)
            self.dirs.add(x)

    def save(self):
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"files": sorted(self.files), "dirs": sorted(self.dirs)}, fh, indent=1)
        os.replace(tmp, self.path)


def target_path(output_dir: str, f: TFile) -> str:
    return os.path.join(output_dir, *f.parts)


def torrent_root(torrent: Torrent, output_dir: str) -> str:
    """The folder (multi-file) or file (single-file) qBittorrent will look at."""
    return os.path.join(output_dir, torrent.name)


def scan(dirs) -> list[Source]:
    seen, out = set(), []
    for d in dirs:
        if os.path.isfile(d):
            walk = [(os.path.dirname(d), [], [os.path.basename(d)])]
        else:
            walk = os.walk(d)
        for root, _, names in walk:
            for n in names:
                p = os.path.join(root, n)
                k = _key(p)
                if k in seen:
                    continue
                seen.add(k)
                try:
                    out.append(Source(p, os.path.getsize(p)))
                except OSError:
                    pass
    return out


# ---------------------------------------------------------------- nfo handling

def eol_variants(data: bytes) -> list[bytes]:
    """The original plus CRLF / LF / CR renderings, each with and without a trailing EOL."""
    lf = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    bodies = [lf, lf.rstrip(b"\n"), lf.rstrip(b"\n") + b"\n"]
    out = [data]
    for body in bodies:
        for eol in (b"\r\n", b"\n", b"\r"):
            v = body.replace(b"\n", eol)
            if v not in out:
                out.append(v)
    return out


def _read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def describe_eol(data: bytes) -> str:
    crlf = data.count(b"\r\n")
    lf = data.count(b"\n") - crlf
    cr = data.count(b"\r") - crlf
    kinds = [k for k, n in (("CRLF", crlf), ("LF", lf), ("CR", cr)) if n]
    return "+".join(kinds) or "no line breaks"


# ---------------------------------------------------------------- matching

def _match(torrent: Torrent, sources: list[Source], output_dir: str, res: Result):
    """Return {relpath: Source} for binary files and [(TFile, Source|None)] for text files."""
    used: set[str] = set()
    chosen: dict[str, Source] = {}
    files = [f for f in torrent.real_files if not is_text(f)]
    texts = [f for f in torrent.real_files if is_text(f)]

    def take(f, s, how):
        chosen[f.relpath] = s
        used.add(_key(s.path))
        res.how[f.relpath] = how

    def free(pred):
        return [s for s in sources if _key(s.path) not in used and pred(s)]

    # pass 0: already exactly where it belongs
    for f in files:
        tp = target_path(output_dir, f)
        for s in free(lambda s: _key(s.path) == _key(tp) and s.size == f.length):
            take(f, s, "already in place")

    # pass 1: name + size
    for f in files:
        if f.relpath in chosen:
            continue
        c = free(lambda s: s.size == f.length and s.name.casefold() == f.name.casefold())
        if len(c) == 1:
            take(f, c[0], "name+size")

    # pass 2/3: size, then piece hash
    with PieceVerifier(torrent, lambda _f: None) as pv:
        for f in files:
            if f.relpath in chosen:
                continue
            c = free(lambda s: s.size == f.length)
            if len(c) == 1:
                take(f, c[0], "size (unique)")
                continue
            if not c:
                continue
            piece = torrent.contained_piece(f)
            if piece is None:
                res.notes.append(f"{f.relpath}: {len(c)} same-size candidates and the file is too "
                                 "small to hash-verify on its own; left unmatched")
                continue
            ok = [s for s in c if pv.check_bytes_at(s.path, f, piece)]
            if len(ok) >= 1:
                take(f, ok[0], f"size+piece hash (1 of {len(c)} candidates)")
            else:
                res.notes.append(f"{f.relpath}: none of {len(c)} same-size candidates hash-verify")

    # text files: by name first, else a same-type file that can be made to fit
    text_pairs = []
    for f in texts:
        c = free(lambda s: s.name.casefold() == f.name.casefold())
        if not c:
            c = [s for s in free(lambda s: s.name.lower().endswith(f.ext) and s.size < TEXT_MAX)
                 if any(len(v) == f.length for v in eol_variants(_read(s.path)))]
            if len(c) > 1:
                res.notes.append(f"{f.relpath}: {len(c)} differently-named {f.ext} files could fit; "
                                 f"using {c[0].name}")
        s = c[0] if c else None
        if s:
            used.add(_key(s.path))
        text_pairs.append((f, s))
    return chosen, text_pairs


def find_sources(torrent: Torrent, dirs: list[str]) -> dict[str, str]:
    """{torrent relpath: file in ``dirs`` that would supply it} - matching only, nothing moves."""
    res = Result()
    probe = os.path.join(os.sep, "nonexistent-nzb2seed-probe")
    chosen, text_pairs = _match(torrent, scan(dirs), probe, res)
    out = {rel: s.path for rel, s in chosen.items()}
    out.update({f.relpath: s.path for f, s in text_pairs if s})
    return out


# ---------------------------------------------------------------- moving

PART_SUFFIX = ".nzb2seed-part"
COPY_CHUNK = 8 << 20


def _copy_across(src: str, dst: str, progress=None, remove_src: bool = True):
    """Move between filesystems without ever leaving a truncated file under the
    final name: copy to <dst>.part, fsync, rename into place, then delete src
    (unless ``remove_src`` is False: a copy)."""
    part = dst + PART_SUFFIX
    total = os.path.getsize(src)
    done = 0
    with open(src, "rb") as fi, open(part, "wb") as fo:
        while True:
            buf = fi.read(COPY_CHUNK)
            if not buf:
                break
            fo.write(buf)
            done += len(buf)
            if progress:
                progress(os.path.basename(dst), done, total)
        fo.flush()
        os.fsync(fo.fileno())
    if os.path.getsize(part) != total:
        os.remove(part)
        raise OSError(f"copy of {src} came out the wrong size")
    shutil.copystat(src, part)
    os.replace(part, dst)
    if remove_src:
        os.remove(src)


def _move(src: str, dst: str, owned: Owned, progress=None):
    if _key(src) == _key(dst):
        return
    owned.makedirs(os.path.dirname(dst))
    if os.path.lexists(dst):
        if not owned.owns(dst):
            raise RuntimeError(f"{dst} already exists and was not created by nzb2seed; not touching it")
        os.remove(dst)                    # our own stale copy from an earlier run
    try:
        os.replace(src, dst)              # same filesystem: instant
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        _copy_across(src, dst, progress)
    owned.forget_file(src)
    owned.add_file(dst)


def _place_copy(src: str, dst: str, owned: Owned, progress=None) -> str:
    """Put a copy of someone else's file at ``dst``, leaving ``src`` where it is: a hard link
    on the same filesystem (no extra space), a copy otherwise. Returns "link" or "copy"."""
    if _key(src) == _key(dst):
        return "keep"
    owned.makedirs(os.path.dirname(dst))
    if os.path.lexists(dst):
        if not owned.owns(dst):
            raise RuntimeError(f"{dst} already exists and was not created by nzb2seed; not touching it")
        os.remove(dst)
    how = "link"
    try:
        os.link(src, dst)
    except OSError:
        _copy_across(src, dst, progress, remove_src=False)
        how = "copy"
    owned.add_file(dst)
    return how


def _under(path: str, dirs) -> bool:
    k = _key(path)
    return any(k == _key(d) or k.startswith(_key(d).rstrip("/\\") + os.sep.lower()) or
               k.startswith(_key(d).rstrip("/\\") + "/") for d in dirs)


# ---------------------------------------------------------------- text files

def fix_text_files(torrent: Torrent, pairs, locate, res: Result) -> dict[str, tuple[bytes, bytes]]:
    """Decide, per text file, whether its line endings must change.

    A file is only ever rewritten when the pieces it touches FAIL as downloaded
    and a rewrite makes them PASS. A file that already verifies (e.g. an .nfo
    that is LF-only in the torrent too) is never touched, and when no variant
    verifies the download is kept as-is. Nothing is written here: candidates are
    hashed from memory. Text files that share a piece (.nfo next to .sfv) are
    solved together. Returns {relpath: (original, chosen)}.
    """
    items = {f.relpath: (f, _read(s.path)) for f, s in pairs}
    picks = {rel: (data, data) for rel, (_, data) in items.items()}
    if not items:
        return picks

    # cluster text files that share pieces
    parent = {rel: rel for rel in items}

    def root(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    owner: dict[int, str] = {}
    for rel, (f, _) in items.items():
        for p in torrent.pieces_overlapping(f.offset, f.length):
            if p in owner:
                parent[root(rel)] = root(owner[p])
            else:
                owner[p] = rel
    clusters: dict[str, list[str]] = {}
    for rel in items:
        clusters.setdefault(root(rel), []).append(rel)

    text_locate = lambda f: None if f.relpath in items else locate(f)  # noqa: E731
    with PieceVerifier(torrent, text_locate, cache=True) as pv:
        for rels in clusters.values():
            files = [items[r][0] for r in rels]
            pieces = sorted({p for f in files for p in torrent.pieces_overlapping(f.offset, f.length)})

            def passes(combo) -> bool:
                if any(len(c) != f.length for c, f in zip(combo, files)):
                    return False
                pv.overrides = dict(zip(rels, combo))
                return all(pv.check(p) for p in pieces)

            originals = tuple(items[r][1] for r in rels)
            if passes(originals):
                for r in rels:
                    res.how[r] = "pieces verify as downloaded - unchanged"
                continue
            options = [[items[r][1]] + [v for v in eol_variants(items[r][1])[1:]
                                        if len(v) == items[r][0].length] for r in rels]
            found = None
            for n, combo in enumerate(itertools.product(*options)):
                if n >= MAX_COMBOS:
                    break
                if combo != originals and passes(combo):
                    found = combo
                    break
            if found is None:
                for r in rels:
                    f, data = items[r]
                    res.how[r] = "left as downloaded"
                    res.notes.append(
                        f"{r}: pieces {pieces[0]}-{pieces[-1]} fail as downloaded ({len(data)} bytes, "
                        f"{describe_eol(data)}; torrent wants {f.length}) and no line-ending variant "
                        "makes them pass - left as downloaded (a neighbouring file may be missing or bad)")
                continue
            for r, pick in zip(rels, found):
                data = items[r][1]
                picks[r] = (data, pick)
                res.how[r] = ("pieces verify as downloaded - unchanged" if pick == data else
                              f"line endings {describe_eol(data)} -> {describe_eol(pick)}, "
                              "verified by piece hash")
    return picks


def assemble(torrent: Torrent, source_dirs: list[str], output_dir: str,
             dry_run: bool = False, log=print, progress=None, owned: Owned | None = None,
             keep: list[str] = ()) -> Result:
    """Put every torrent file at ``<output_dir>/<torrent path>``.

    Sources are the given folders plus files nzb2seed placed on an earlier run (``owned``).
    Files in ``keep`` folders (someone else's, e.g. a library) are never moved: they are
    hard-linked, or copied across disks; files anywhere else are nzb2seed's and moved.
    A file already at a target path that nzb2seed did not create is used as-is when it has
    the right size and is otherwise reported as a conflict - it is never moved, rewritten
    or deleted. ``progress(name, done, total)`` reports copies between disks."""
    owned = owned or Owned()
    res = Result()
    sources = scan(source_dirs)
    known = {_key(x.path) for x in sources}
    sources += [Source(p, os.path.getsize(p)) for p in sorted(owned.files)
                if os.path.isfile(p) and _key(p) not in known]
    source_keys = {_key(x.path) for x in sources}

    # someone else's files already sitting where the torrent wants its files
    foreign: set[str] = set()
    for f in torrent.real_files:
        tp = target_path(output_dir, f)
        if os.path.lexists(tp) and _key(tp) not in source_keys and not owned.owns(tp):
            size = os.path.getsize(tp) if os.path.isfile(tp) else -1
            if size == f.length:
                foreign.add(f.relpath)
                res.placed[f.relpath] = tp
                res.how[f.relpath] = "already there (not created by nzb2seed) - left untouched"
                log(f"    keep  {f.relpath}   [{res.how[f.relpath]}]")
            else:
                res.conflicts.append((f, tp))
    if res.conflicts:
        return res

    todo = Torrent(torrent.raw, torrent.name, torrent.infohash, torrent.piece_length, torrent.pieces,
                   [f for f in torrent.files if f.relpath not in foreign], torrent.multi_file,
                   torrent.private, torrent.trackers) if foreign else torrent
    chosen, text_pairs = _match(todo, sources, output_dir, res)
    by_rel = {f.relpath: f for f in torrent.real_files}

    for rel, s in chosen.items():
        dst = target_path(output_dir, by_rel[rel])
        theirs = _under(s.path, keep)
        if _key(s.path) != _key(dst):
            log(f"    {'link' if theirs else 'move'}  {s.name}  ->  {rel}   [{res.how[rel]}"
                + ("; the original stays where it is]" if theirs else "]"))
        if not dry_run:
            if theirs:
                _place_copy(s.path, dst, owned, progress)
            else:
                _move(s.path, dst, owned, progress)
        res.placed[rel] = dst

    # where each binary file is *now* (a dry run has not moved anything)
    current = {rel: (chosen[rel].path if dry_run and rel in chosen else p) for rel, p in res.placed.items()}
    picks = fix_text_files(torrent, [(f, s) for f, s in text_pairs if s],
                           lambda f: current.get(f.relpath), res)

    for f, s in text_pairs:
        if s is None:
            continue
        dst = target_path(output_dir, f)
        data, pick = picks[f.relpath]
        how = res.how[f.relpath]
        if pick != data or _key(s.path) != _key(dst):
            log(f"    {'fix ' if pick != data else 'move'}  {s.name}  ->  {f.relpath}   [{how}]")
        if not dry_run:
            place = _place_copy if _under(s.path, keep) else _move
            if pick == data:
                place(s.path, dst, owned, progress)
            else:
                place(s.path, dst, owned, progress)    # first take it over (or a copy of theirs)
                tmp = dst + ".nzb2seed-tmp"
                with open(tmp, "wb") as fh:
                    fh.write(pick)
                os.replace(tmp, dst)
        res.placed[f.relpath] = dst

    # completeness
    for f in torrent.real_files:
        p = res.placed.get(f.relpath)
        if p is None:
            res.missing.append(f)
            continue
        if dry_run:
            continue
        size = os.path.getsize(p) if os.path.exists(p) else -1
        if size != f.length:
            res.wrong_size.append((f, size))
    return res


# ---------------------------------------------------------------- verification

def verify_all(torrent: Torrent, res: Result, progress=None) -> list[int]:
    """Hash every piece locally; return the indices of pieces that fail."""
    bad = []
    locate = lambda f: res.placed.get(f.relpath)  # noqa: E731
    with PieceVerifier(torrent, locate) as pv:
        n = len(torrent.pieces)
        for i in range(n):
            if not pv.check(i):
                bad.append(i)
            if progress and (i % 64 == 0 or i == n - 1):
                progress(i + 1, n, bad)
    return bad


def files_for_pieces(torrent: Torrent, pieces) -> list[TFile]:
    hit = []
    for f in torrent.real_files:
        r = torrent.pieces_overlapping(f.offset, f.length)
        if any(p in r for p in pieces):
            hit.append(f)
    return hit


# ---------------------------------------------------------------- cleanup

def cleanup(torrent: Torrent, res: Result, job_dirs: list[str], output_dir: str,
            owned: Owned | None = None, dry_run: bool = False, log=print) -> list[str]:
    """Remove only what nzb2seed added and the torrent does not need:

    * leftovers inside ``job_dirs`` - folders of SABnzbd jobs this build submitted
      (.nzb, .par2, archives, poster extras), then those folders once empty;
    * files and folders nzb2seed created on an earlier run that are no longer needed
      (from ``owned``), including interrupted-copy .part files.
    Nothing else is ever deleted - not in the output folder, not anywhere."""
    owned = owned or Owned()
    keep = {_key(p) for p in res.placed.values()}
    out_key = _key(output_dir)
    removed = []

    def rm(p, shown):
        log(f"    delete {shown}")
        removed.append(p)
        if not dry_run:
            os.remove(p)
            owned.forget_file(p)

    for d in job_dirs:
        k = _key(d)
        if not os.path.isdir(d):
            continue
        if out_key == k or out_key.startswith(k + os.sep) or os.path.dirname(k) == k:
            log(f"    skip cleanup of {d}: it contains the output folder")
            continue
        for dirpath, _, names in os.walk(d, topdown=False):
            for n in names:
                p = os.path.join(dirpath, n)
                if _key(p) not in keep:
                    rm(p, os.path.relpath(p, d))
            if not dry_run:
                try:
                    os.rmdir(dirpath)             # only succeeds once empty
                    log(f"    rmdir  {dirpath}")
                except OSError:
                    pass

    stale = [p for p in owned.files if _key(p) not in keep and os.path.isfile(p)]
    stale += [p + PART_SUFFIX for p in res.placed.values() if os.path.isfile(p + PART_SUFFIX)]
    for p in stale:
        rm(p, p)
    for d in sorted(owned.dirs, key=len, reverse=True):
        if not dry_run and os.path.isdir(d):
            try:
                os.rmdir(d)                       # only if empty: never removes anything but the folder
                owned.dirs.discard(d)
                log(f"    rmdir  {d}")
            except OSError:
                pass
    return removed
