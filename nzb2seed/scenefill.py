"""Supply a RAR'd scene torrent's files from a download that does not hold them as posted.

Posts of scene releases are often re-packed: a different RAR set around the same video,
without the .nfo, .sfv or Sample. The torrent needs the original volumes byte for byte.
srrDB has what it takes: the CRC of the video that was inside the RARs, a .srr that
rebuilds the original volumes from that video (vendored pyReScene), and the release's
small files. The Sample, which srrDB does not store, is fetched on its own from any post
whose NZB lists it. Everything goes into a subfolder of the download folder; the normal
matching and piece check then decide whether it is right.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess

from . import archives, metadata, scenerar
from .assemble import TEXT_EXTS
from .report import end_progress, info, progress, warn

SCENE_DIR = "nzb2seed-scene"
_done: dict[tuple[str, str, str], bool] = {}  # (infohash, folder, release) -> filled anything
_have: dict[str, set[str]] = {}             # infohash -> file names already supplied in this build


def reset():
    """Forget what earlier builds did (called at the start of each build)."""
    _done.clear()
    _have.clear()
    _details.clear()
    _listings.clear()


def release_name(t) -> str:
    return t.name[:-len(".torrent")] if t.name.lower().endswith(".torrent") else t.name


_SPLIT = re.compile(r"^(?P<stem>.+)\.(?P<n>\d{3})$")
_ARCHIVE_NAME = re.compile(r"\.(rar|r\d{2,3}|7z|zip|\d{3})$", re.I)
_listings: dict[str, list[tuple[str, int]] | None] = {}   # first volume -> what it holds (per build)


def _base(p: str) -> str:
    return os.path.basename(p.replace("\\", "/"))


def _listing(first: str) -> list[tuple[str, int]] | None:
    if first not in _listings:
        try:
            _listings[first] = archives.list_contents(first)
        except (RuntimeError, OSError, subprocess.SubprocessError):
            _listings[first] = None     # not an archive after all (a plain split file), or unreadable
    return _listings[first]


def _extract(first: str, members: list[str], dest: str) -> bool:
    try:
        archives.extract(first, members, dest)
        return True
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        warn(f"unpacking {os.path.basename(first)} failed: {e}")
        return False


def _joined_split(d: str, size: int) -> str | None:
    """A file posted as plain numbered pieces (name.001, name.002, ...): joined, when the
    pieces add up to ``size`` and are not an archive."""
    sets: dict[str, list[str]] = {}
    for root, _, ns in os.walk(d):
        if archives.UNPACK_DIR in root.split(os.sep):
            continue
        for n in ns:
            m = _SPLIT.match(n)
            if m:
                sets.setdefault(os.path.join(root, m["stem"]), []).append(os.path.join(root, n))
    for stem, parts in sets.items():
        parts.sort()
        nums = [int(_SPLIT.match(os.path.basename(p))["n"]) for p in parts]
        if nums != list(range(nums[0], nums[0] + len(nums))) or nums[0] not in (0, 1):
            continue
        if sum(os.path.getsize(p) for p in parts) != size or _listing(parts[0]):
            continue
        dest = os.path.join(d, archives.UNPACK_DIR, os.path.basename(stem))
        if not (os.path.isfile(dest) and os.path.getsize(dest) == size):
            info(f"{os.path.basename(stem)}: {len(parts)} numbered pieces add up to {size} bytes - joining them")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest + ".part", "wb") as out:
                for p in parts:
                    with open(p, "rb") as fh:
                        shutil.copyfileobj(fh, out, 8 << 20)
            os.replace(dest + ".part", dest)
        return dest
    return None


def _nested_sets(d: str) -> list[str]:
    """Archives inside archives (a zip or 7z around a RAR set, a RAR around another):
    the inner sets are unpacked - one level deep - and their first volumes returned."""
    out = []
    for i, first in enumerate(archives.archive_sets(d)):
        inner = [p for p, _ in (_listing(first) or []) if _ARCHIVE_NAME.search(p)]
        if not inner:
            continue
        dest = os.path.join(d, archives.UNPACK_DIR, f"nested-{i}")
        if not os.path.isdir(dest):
            info(f"{os.path.basename(first)} holds archives of its own - unpacking them to look inside")
            if not _extract(first, inner, dest):
                continue
        out += archives.archive_sets(dest)
    return out


def _find(d: str, size: int, name: str) -> str | None:
    """A file of exactly ``size`` bytes in download folder ``d``, however the post carried
    it: loose under any name; inside an archive (RAR in either volume style, 7z, zip,
    numbered set - encrypted ones with the password their NZB carried); as plain numbered
    pieces; or inside an archive inside an archive. Unpacked into the unpack folder."""
    for root, _, ns in os.walk(d):
        for n in ns:
            p = os.path.join(root, n)
            if os.path.getsize(p) == size and not scenerar.VOLUME.search(n):
                return p
    for sets in (lambda: archives.archive_sets(d), lambda: _nested_sets(d)):
        for first in sets():
            member = next((p for p, s in (_listing(first) or []) if s == size), None)
            if member is None:
                continue
            dest = os.path.join(d, archives.UNPACK_DIR)
            info(f"{os.path.basename(first)} holds a {size}-byte file, the size of {name} - unpacking it")
            if _extract(first, [member], dest):
                p = os.path.join(dest, *member.replace("\\", "/").split("/"))
                if os.path.isfile(p):
                    return p
    return _joined_split(d, size)


def _take_packed(d: str, want: dict, out: str) -> set[str]:
    """Torrent files packed whole inside a re-pack (the scene volumes, .nfo, Sample... in
    a RAR, 7z or zip around them): unpacked as they are - nothing to rebuild."""
    got: set[str] = set()
    for sets in (lambda: archives.archive_sets(d), lambda: _nested_sets(d)):
        for first in sets():
            hits = []
            for p, size in _listing(first) or []:
                f = want.get(_base(p).lower())
                if f is not None and f.length == size and _base(p).lower() not in got:
                    hits.append(p)
            if hits and _extract(first, hits, out):
                info(f"{os.path.basename(first)} holds {len(hits)} of the torrent's files as they are - "
                     "unpacked them")
                got |= {_base(p).lower() for p in hits}
        if got:
            break
    return got


_SUBDIRS = {"sample", "samples", "proof", "proofs", "subs", "sub", "extras"}
_details: dict[str, dict | None] = {}       # name -> srrDB's details (asked once per build)
_STEM = re.compile(r"(\.part\d+)?\.(rar|r\d{2,3}|\d{3}|sfv|nfo)$", re.I)
_POSTER = re.compile(r"([-.](xpost|repost|obfuscated|postbot|scrambled)|\s*\[[^]]*\])+$", re.I)


def release_of(t, f) -> str:
    """The scene release a torrent file belongs to: its nearest folder that is not a
    Sample/Proof/Subs folder - in a season pack of scene episodes, each episode is its
    own release on srrDB, and the pack's name is not one. The torrent's name otherwise."""
    for part in reversed(f.parts[:-1]):
        if part.lower() not in _SUBDIRS:
            return part
    return release_name(t)


def _names(t, release: str, files: list) -> list[str]:
    """Names srrDB may know the release by, most likely first: its folder, the torrent,
    the scene volumes' / .sfv / .nfo names (scene releases name them after themselves),
    and each without a poster's suffix such as -xpost."""
    names = [release, release_name(t)]
    names += [_STEM.sub("", f.name) for f in files if _STEM.search(f.name)]
    names += [_POSTER.sub("", n) for n in names]
    return [n for n in dict.fromkeys(n.strip() for n in names) if n][:6]


def details(release: str) -> dict | None:
    if release not in _details:
        try:
            _details[release] = metadata.srrdb_details(release)
        except Exception as e:              # srrDB being unreachable must not stop a build
            warn(f"srrDB: {e}")
            return None
    return _details[release]


def details_for(t, release: str, files: list) -> tuple[str, dict | None]:
    """(the name srrDB knows the release by, its details) - None when no name finds it."""
    for n in _names(t, release, files):
        det = details(n)
        if det:
            return n, det
    return release, None


def inner_sizes(t, files: list) -> tuple[int, ...]:
    """Sizes of what the scene RARs of ``files``' releases hold (their videos), from srrDB."""
    groups: dict[str, list] = {}
    for f in files:
        groups.setdefault(release_of(t, f), []).append(f)
    out = []
    for release, fs in groups.items():
        det = details_for(t, release, fs)[1] or {}
        out += [a["size"] for a in det.get("archived-files", []) if a.get("size")]
    return tuple(out)


def fill(t, d: str, missing: list, fetch=None) -> bool:
    """Put what srrDB can rebuild or provide for ``missing`` torrent files into
    ``d/nzb2seed-scene``, release by release. True when anything was added. Each release is
    tried once per download folder."""
    by_release: dict[str, list] = {}
    for f in missing:
        by_release.setdefault(release_of(t, f), []).append(f)
    added = False
    for release, files in by_release.items():
        added |= _fill_release(t, release, d, files, fetch)
    return added


def _fill_release(t, release: str, d: str, missing: list, fetch=None) -> bool:
    key = (t.infohash, os.path.abspath(d), release)
    if key in _done:
        return False
    _done[key] = False
    release, det = details_for(t, release, missing)
    if not det:
        return False
    out = os.path.join(d, SCENE_DIR)
    have = _have.setdefault(t.infohash, set())
    # what another download folder of this build already got is not fetched or rebuilt again
    want = {f.name.lower(): f for f in missing if f.name.lower() not in have}
    added = False

    # 1. the torrent's files packed whole inside a re-pack: taken as they are
    got = _take_packed(d, want, out)
    if got:
        added = True
        have.update(got)
        want = {n: f for n, f in want.items() if n not in got}

    # 2. the scene RAR volumes, rebuilt from what they held (one video, or several files)
    vols = {os.path.basename(v["name"]).lower(): v for v in scenerar.volumes(det)}
    need_vols = [f for n, f in want.items() if n in vols and vols[n]["size"] == f.length]
    inner = [a for a in det.get("archived-files", []) if a.get("size")]
    if need_vols and inner:
        what = os.path.basename(inner[0]["name"]) if len(inner) == 1 else f"the {len(inner)} files they held"
        info(f"{release}: the download does not hold the scene RAR volumes; srrDB can rebuild them "
             f"from {what}")
        found, bad = {}, None
        for a in inner:
            p = _find(d, a["size"], os.path.basename(a["name"]))
            if p is None:
                info(f"{release}: no {a['size']}-byte {os.path.basename(a['name'])} in this download "
                     "to rebuild them from")
                break
            progress(f"CRC check of {os.path.basename(p)}")
            crc = metadata.crc32(p)
            end_progress()
            if a.get("crc") and crc != a["crc"].upper():
                bad = (p, crc, a)
                break
            found[a["name"]] = p
        if bad:
            p, crc, a = bad
            warn(f"{release}: {os.path.basename(p)} is not the original (CRC {crc}, srrDB has "
                 f"{a['crc'].upper()}) - a re-encode or re-mux; the scene RARs cannot be rebuilt from it")
        elif len(found) == len(inner):
            info(f"{release}: every CRC matches srrDB - rebuilding the {len(vols)} scene RAR volumes")
            progress(f"rebuilding the scene RARs of {release}")
            ok, why = scenerar.rebuild(release, det, found if len(found) > 1 else next(iter(found.values())), out)
            end_progress()
            (info if ok else warn)(f"{release}: {why}")
            added |= ok
            if ok:
                have.update(vols)

    # 3. the release's small files (.nfo, .sfv, Sample...): from srrDB, else from another post
    for f in metadata.release_files(det):
        rel = f["name"].replace("\\", "/")
        tf = want.get(os.path.basename(rel).lower())
        # a text file may differ only in its line endings: the layout step converts it when
        # that makes its pieces verify
        if tf is None or (tf.length != f["size"] and os.path.splitext(rel)[1].lower() not in TEXT_EXTS):
            continue
        dst = os.path.join(out, *rel.split("/"))
        data = None
        try:
            data = metadata.srrdb_file(release, rel)
        except Exception:
            pass
        if data and len(data) == f["size"]:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as fh:
                fh.write(data)
            info(f"{release}: got {rel} from srrDB"
                 + (f" ({f['size']} bytes; the torrent's is {tf.length} - line endings are matched when "
                    "the files are laid out)" if tf.length != f["size"] else ""))
            added = True
            have.add(tf.name.lower())
        elif fetch and f.get("crc") and fetch(release, rel, f["size"], f["crc"], dst):
            info(f"{release}: got {rel} from another post of the release (size and CRC match)")
            added = True
            have.add(tf.name.lower())
        else:
            warn(f"{release}: {rel} is not in this download, srrDB does not store it"
                 + (" and no other post had it" if fetch else ""))
    _done[key] = added
    return added
