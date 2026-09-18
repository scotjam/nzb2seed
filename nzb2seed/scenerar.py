"""Keep - or rebuild - the original scene RAR set of a release.

srrDB lists every RAR volume of a scene release with its size and CRC, and keeps a
``.srr`` file holding everything about the RAR set except the packed video. With the
unpacked video, pyReScene (vendored in ``_vendor/rescene``, MIT) rebuilds the original
volumes byte for byte - so an obfuscated, re-packed or encrypted post can still end up as
the scene RAR set. Rebuilt volumes are only kept when every one matches srrDB's CRC.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import warnings

from . import metadata

VOLUME = re.compile(r"\.(rar|r\d{2,3}|\d{3})$", re.I)


def _rescene():
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_vendor")
    if here not in sys.path:
        sys.path.insert(0, here)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")        # 2015-era code: invalid-escape warnings on new Pythons
        import rescene.main as main            # noqa: E402
    return main


def volumes(details: dict) -> list[dict]:
    """The release's RAR volumes as srrDB lists them (empty: the release was not RAR'd)."""
    return [f for f in details.get("files", []) if VOLUME.search(f["name"])]


def _local(folder: str) -> dict[str, str]:
    return {n.lower(): os.path.join(r, n) for r, _, ns in os.walk(folder) for n in ns}


def check_volumes(folder: str, details: dict) -> tuple[bool, list[str]]:
    """Are the scene RAR volumes in ``folder`` exactly the originals? (ok, problems)"""
    have = _local(folder)
    problems = []
    for v in volumes(details):
        p = have.get(os.path.basename(v["name"]).lower())
        if p is None:
            problems.append(f"{v['name']} missing")
        elif os.path.getsize(p) != v["size"]:
            problems.append(f"{v['name']} has the wrong size")
        elif metadata.crc32(p) != v["crc"].upper():
            problems.append(f"{v['name']} has the wrong CRC")
    return not problems, problems


def srr_file(release: str) -> bytes | None:
    return metadata._get(f"{metadata.SRRDB_DL}/srr/{release}", timeout=60)


def rebuild(release: str, details: dict, video: str, out_folder: str) -> tuple[bool, str]:
    """Rebuild the scene RAR volumes of ``release`` from srrDB's .srr and the unpacked
    ``video`` into ``out_folder``. (ok, what happened). Nothing is left behind on failure."""
    vols = volumes(details)
    if not vols:
        return False, "the release was not RAR'd"
    archived = [f for f in details.get("archived-files", [])]
    if not archived:
        return False, "srrDB does not say what the RARs contained"
    srr = srr_file(release)
    if not srr:
        return False, "srrDB has no .srr for this release"
    work = tempfile.mkdtemp(prefix="nzb2seed-rescene-", dir=os.path.dirname(os.path.abspath(out_folder)))
    try:
        srr_path = os.path.join(work, release + ".srr")
        with open(srr_path, "wb") as fh:
            fh.write(srr)
        src = os.path.join(work, "in")
        out = os.path.join(work, "out")
        os.makedirs(src)
        os.makedirs(out)
        # the video under the name it had inside the RARs (a hard link: no copy, same disk)
        inner = archived[0]["name"].replace("\\", "/")
        dst = os.path.join(src, *inner.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            os.link(video, dst)
        except OSError:
            shutil.copy2(video, dst)
        main = _rescene()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            main.reconstruct(srr_path, src, out, extract_paths=True, extract_files=False)
        ok, problems = check_volumes(out, details)
        if not ok:
            return False, "rebuilt volumes do not match srrDB: " + "; ".join(problems[:3])
        os.makedirs(out_folder, exist_ok=True)
        have = _local(out)
        for v in vols:
            p = have[os.path.basename(v["name"]).lower()]
            os.replace(p, os.path.join(out_folder, os.path.basename(v["name"])))
        return True, f"rebuilt {len(vols)} scene RAR volume(s) from srrDB's .srr; every CRC matches"
    except Exception as e:              # pyReScene raises its own error types
        return False, f"rebuilding failed: {e}"
    finally:
        shutil.rmtree(work, ignore_errors=True)
