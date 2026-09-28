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


_FIRST = re.compile(r"(\.part0*1\.rar|\.7z|\.7z\.0*1|\.zip|\.0*1)$", re.I)
_LATER = re.compile(r"(\.part0*[2-9]\d*\.rar|\.part0*1\d+\.rar|\.7z\.0*[2-9]\d*|\.0*[2-9]\d*|\.0*1\d+)$", re.I)


def archive_sets(d: str) -> list[str]:
    """First volume of every archive set under ``d`` that 7-Zip or unrar can open: RAR in
    both volume styles, 7z (single or split), zip, and numbered sets (.001) - a numbered
    set can also be a plain split file, which listing then tells apart."""
    out = []
    for root, _, names in os.walk(d):
        if UNPACK_DIR in os.path.relpath(root, d).split(os.sep):
            continue                            # what was unpacked here: not looked at again
        for n in sorted(names):
            if _PART.search(n):
                if int(_PART.search(n).group(1)) == 1:
                    out.append(os.path.join(root, n))
            elif _RAR.search(n) or (_FIRST.search(n) and not _LATER.search(n)):
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


# Passwords carried by the NZBs nzb2seed has read (<meta type="password">) - an encrypted
# re-pack is opened with these, as SABnzbd would. Tried only after no password at all.
_passwords: list[str] = []
_works: dict[str, str] = {}             # first volume -> the password that opened it


def remember_password(password: str | None):
    if password and password not in _passwords:
        _passwords.append(password)
        del _passwords[:-200]           # the recent ones are the ones that matter


def _candidates(first_volume: str) -> list[str]:
    known = _works.get(first_volume)
    rest = [p for p in reversed(_passwords) if p != known]
    return ([known] if known else []) + ["-"] + rest


def list_contents(first_volume: str) -> list[tuple[str, int]]:
    t = tool()
    if t is None:
        raise RuntimeError("no 7-Zip or unrar found to look inside RAR archives")
    kind, exe = t
    first_error = None
    for pw in _candidates(first_volume):
        if kind == "7z":
            r = subprocess.run([exe, "l", "-slt", f"-p{pw}", first_volume], capture_output=True, text=True,
                               errors="replace", timeout=300)
            entries = parse_7z_slt(r.stdout.split("----------", 1)[-1])
        else:
            r = subprocess.run([exe, "lt", f"-p{pw}", first_volume], capture_output=True, text=True,
                               errors="replace", timeout=300)
            entries = parse_unrar_lt(r.stdout)
        if r.returncode == 0 or entries:
            if pw != "-":
                _works[first_volume] = pw
            return entries
        first_error = first_error or ((r.stderr or r.stdout).strip().splitlines()[-1:] or ["cannot read archive"])
    raise RuntimeError(first_error)


def extract(first_volume: str, members: list[str], dest: str):
    """Extract ``members`` into ``dest``, never overwriting existing files."""
    t = tool()
    if t is None:
        raise RuntimeError("no 7-Zip or unrar found to unpack RAR archives")
    kind, exe = t
    os.makedirs(dest, exist_ok=True)
    first_error = None
    before = _files_in(dest)
    for pw in _candidates(first_volume):
        if kind == "7z":
            cmd = [exe, "x", "-aos", f"-p{pw}", "-y", f"-o{dest}", first_volume, *members]
        else:
            cmd = [exe, "x", "-o-", f"-p{pw}", "-y", first_volume, *members, dest + os.sep]
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=6 * 3600)
        if r.returncode == 0:
            if pw != "-":
                _works[first_volume] = pw
            return
        first_error = first_error or ((r.stderr or r.stdout).strip().splitlines() or ["extraction failed"])[-1]
        for f in _files_in(dest) - before:      # what a wrong password half-wrote: never kept
            try:
                os.remove(f)
            except OSError:
                pass
    raise RuntimeError(first_error)


def _files_in(d: str) -> set[str]:
    return {os.path.join(root, n) for root, _, ns in os.walk(d) for n in ns}
