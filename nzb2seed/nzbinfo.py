"""Look inside an NZB before downloading it, to rank candidate posts.

An NZB lists every posted file (usually with its real name in the subject) and
the byte count of every segment. That is enough to tell a post that carries the
torrent's .nfo / sample / RAR set apart from a bare re-upload of the video.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from .torrent import Torrent

_QUOTED = re.compile(r'"([^"]+)"')
_PAR2 = re.compile(r"\.(par2|vol\d+\+\d+\.par2)$", re.I)
_RAR = re.compile(r"\.(rar|r\d\d|\d\d\d)$", re.I)
YENC_OVERHEAD = (0.995, 1.06)   # posted bytes / payload bytes


@dataclass
class NzbFile:
    name: str
    posted_bytes: int


@dataclass
class Score:
    name_hits: int
    names_wanted: int
    ratio: float            # posted non-par2 bytes / torrent size
    plausible: bool
    archives: bool          # the post is a RAR set
    summary: str

    @property
    def key(self):
        # enough bytes first (a short post is missing something), then how many names match
        return (self.plausible, self.name_hits / max(self.names_wanted, 1))


def parse(data: bytes) -> list[NzbFile]:
    root = ET.fromstring(data)
    out = []
    for f in root.iter():
        if not f.tag.endswith("file"):
            continue
        subject = f.get("subject", "")
        m = _QUOTED.search(subject)
        name = (m.group(1) if m else subject).strip()
        total = sum(int(s.get("bytes", 0) or 0) for s in f.iter() if s.tag.endswith("segment"))
        out.append(NzbFile(name, total))
    return out


def score(files: list[NzbFile], torrent: Torrent, part: list | None = None) -> Score:
    """How likely this post holds ``part`` of the torrent (default: all of it)."""
    part = part if part is not None else torrent.real_files
    total = sum(f.length for f in part)
    wanted = {f.name.lower() for f in part}
    posted = {f.name.lower() for f in files}
    hits = len(wanted & posted)
    payload = sum(f.posted_bytes for f in files if not _PAR2.search(f.name))
    ratio = payload / total if total else 0
    archives = any(_RAR.search(f.name) for f in files)
    loose = [f for f in files if not _PAR2.search(f.name)]
    # without archives every torrent file needs a posted file of its own
    too_few = not archives and len(loose) < len(part)
    plausible = YENC_OVERHEAD[0] <= ratio <= YENC_OVERHEAD[1] and not too_few
    why = ""
    if too_few:
        why = f" (only {len(loose)} loose files for {len(part)} torrent files)"
    elif not plausible:
        why = " (implausible)"
    summary = (f"{hits}/{len(wanted)} torrent file names, {len(files)} posted files"
               f"{' (RAR set)' if archives else ''}, size x{ratio:.3f}{why}")
    return Score(hits, len(wanted), ratio, plausible, archives, summary)
