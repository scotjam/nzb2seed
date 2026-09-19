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
import subprocess

from . import archives, metadata, scenerar
from .assemble import TEXT_EXTS
from .report import end_progress, info, progress, warn

SCENE_DIR = "nzb2seed-scene"
_done: dict[tuple[str, str], bool] = {}     # (infohash, folder) -> filled anything
_have: dict[str, set[str]] = {}             # infohash -> file names already supplied in this build


def reset():
    """Forget what earlier builds did (called at the start of each build)."""
    _done.clear()
    _have.clear()


def release_name(t) -> str:
    return t.name[:-len(".torrent")] if t.name.lower().endswith(".torrent") else t.name


def _video_in(d: str, size: int, name: str) -> str | None:
    """The release's video in download folder ``d``: loose (any name, exact size), or
    unpacked from one of its RAR sets into the unpack folder."""
    for root, _, ns in os.walk(d):
        for n in ns:
            p = os.path.join(root, n)
            if os.path.getsize(p) == size and not scenerar.VOLUME.search(n):
                return p
    for first in archives.rar_sets(d):
        try:
            entries = archives.list_contents(first)
        except (RuntimeError, OSError, subprocess.SubprocessError):
            continue
        member = next((p for p, s in entries if s == size), None)
        if member is None:
            continue
        dest = os.path.join(d, archives.UNPACK_DIR)
        info(f"{os.path.basename(first)} holds a {size}-byte video, the size of {name} - unpacking it")
        try:
            archives.extract(first, [member], dest)
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            warn(f"unpacking {os.path.basename(first)} failed: {e}")
            continue
        p = os.path.join(dest, *member.replace("\\", "/").split("/"))
        if os.path.isfile(p):
            return p
    return None


def fill(t, d: str, missing: list, fetch=None) -> bool:
    """Put what srrDB can rebuild or provide for ``missing`` torrent files into
    ``d/nzb2seed-scene``. True when anything was added. Tried once per download folder."""
    key = (t.infohash, os.path.abspath(d))
    if key in _done:
        return False
    _done[key] = False
    release = release_name(t)
    try:
        det = metadata.srrdb_details(release)
    except Exception as e:                  # srrDB being unreachable must not stop a build
        warn(f"srrDB: {e}")
        return False
    if not det:
        return False
    out = os.path.join(d, SCENE_DIR)
    have = _have.setdefault(t.infohash, set())
    # what another download folder of this build already got is not fetched or rebuilt again
    want = {f.name.lower(): f for f in missing if f.name.lower() not in have}
    added = False

    # 1. the scene RAR volumes, rebuilt from the original video
    vols = {os.path.basename(v["name"]).lower(): v for v in scenerar.volumes(det)}
    need_vols = [f for n, f in want.items() if n in vols and vols[n]["size"] == f.length]
    videos = [a for a in det.get("archived-files", []) if a.get("size")]
    if need_vols and len(videos) == 1:
        v = videos[0]
        info(f"{release}: the download does not hold the scene RAR volumes; srrDB can rebuild them "
             f"from the original video ({os.path.basename(v['name'])})")
        video = _video_in(d, v["size"], os.path.basename(v["name"]))
        if video is None:
            info(f"{release}: no {v['size']}-byte video in this download to rebuild them from")
        else:
            progress(f"CRC check of {os.path.basename(video)}")
            crc = metadata.crc32(video)
            end_progress()
            if crc != v["crc"].upper():
                warn(f"{release}: the video in this download is not the original (CRC {crc}, srrDB has "
                     f"{v['crc'].upper()}) - a re-encode or re-mux; the scene RARs cannot be rebuilt from it")
            else:
                info(f"{release}: the video's CRC {crc} matches srrDB - rebuilding the {len(vols)} scene RAR volumes")
                progress(f"rebuilding the scene RARs of {release}")
                ok, why = scenerar.rebuild(release, det, video, out)
                end_progress()
                (info if ok else warn)(f"{release}: {why}")
                added |= ok
                if ok:
                    have.update(vols)
    elif need_vols:
        info(f"{release}: srrDB lists {len(videos)} files inside the RARs - not rebuilding them")

    # 2. the release's small files (.nfo, .sfv, Sample...): from srrDB, else from another post
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
