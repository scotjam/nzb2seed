"""Look inside RAR sets a download finished with, and unpack only what a torrent needs.

A post can complete as a RAR set (unpacking off, failed, or an oddly named archive)
while the torrent wants the video inside it. Listing uses 7-Zip (7z/7zz/7za) or RARLAB
unrar; extraction never overwrites anything and goes into a folder inside the download
job, which belongs to nzb2seed like the rest of that job.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess

UNPACK_DIR = "nzb2seed-unpacked"
_PART = re.compile(r"\.part(\d+)\.rar$", re.I)
_RAR = re.compile(r"\.rar$", re.I)


def tool() -> tuple[str, str] | None:
    for name in ("7z", "7zz", "7za"):
        p = shutil.which(name)
        if p:
            return "7z", p
    p = shutil.which("unrar")
    if p:
        return "unrar", p
    return None


def rar_sets(d: str) -> list[str]:
    """First volume of every RAR set under ``d`` (x.part01.rar, or x.rar of x.rar/x.r00)."""
    out = []
    for root, _, names in os.walk(d):
        if os.path.basename(root) == UNPACK_DIR:
            continue
        for n in sorted(names):
            if not _RAR.search(n):
                continue
            m = _PART.search(n)
            if m and int(m.group(1)) != 1:
                continue
            out.append(os.path.join(root, n))
    return out


def parse_7z_slt(text: str) -> list[tuple[str, int]]:
    """Entries (path, size) from ``7z l -slt`` output; folders are skipped."""
    out, cur = [], {}
    for line in text.splitlines() + [""]:
        if not line.strip():
            if cur.get("Path") and cur.get("Folder", "-") != "+" and "Size" in cur:
                out.append((cur["Path"], int(cur["Size"] or 0)))
            cur = {}
            continue
        k, sep, v = line.partition(" = ")
        if sep:
            cur[k.strip()] = v.strip()
    return out


def parse_unrar_lt(text: str) -> list[tuple[str, int]]:
    out, name, size, kind = [], None, None, None
    for line in text.splitlines() + [""]:
        s = line.strip()
        if s.startswith("Name: "):
            name = s[6:]
        elif s.startswith("Type: "):
            kind = s[6:]
        elif s.startswith("Size: "):
            size = int(s[6:].split()[0] or 0)
        elif not s and name:
            if kind != "Directory" and size is not None:
                out.append((name, size))
            name, size, kind = None, None, None
    return out


def list_contents(first_volume: str) -> list[tuple[str, int]]:
    t = tool()
    if t is None:
        raise RuntimeError("no 7-Zip or unrar found to look inside RAR archives")
    kind, exe = t
    if kind == "7z":
        r = subprocess.run([exe, "l", "-slt", "-p-", first_volume], capture_output=True, text=True,
                           errors="replace", timeout=300)
        entries = parse_7z_slt(r.stdout.split("----------", 1)[-1])
    else:
        r = subprocess.run([exe, "lt", "-p-", first_volume], capture_output=True, text=True,
                           errors="replace", timeout=300)
        entries = parse_unrar_lt(r.stdout)
    if r.returncode != 0 and not entries:
        raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1:] or ["cannot read archive"])
    return entries


def extract(first_volume: str, members: list[str], dest: str):
    """Extract ``members`` into ``dest``, never overwriting existing files."""
    t = tool()
    if t is None:
        raise RuntimeError("no 7-Zip or unrar found to unpack RAR archives")
    kind, exe = t
    os.makedirs(dest, exist_ok=True)
    if kind == "7z":
        cmd = [exe, "x", "-aos", "-p-", "-y", f"-o{dest}", first_volume, *members]
    else:
        cmd = [exe, "x", "-o-", "-p-", "-y", first_volume, *members, dest + os.sep]
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=6 * 3600)
    if r.returncode != 0:
        raise RuntimeError(((r.stderr or r.stdout).strip().splitlines() or ["extraction failed"])[-1])
