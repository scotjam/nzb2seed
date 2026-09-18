"""Parse .torrent metainfo and verify piece hashes against files on disk."""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field

from . import bencode


class TorrentError(ValueError):
    pass


@dataclass
class TFile:
    parts: tuple[str, ...]   # path relative to the save path, including the root folder for multi-file torrents
    length: int
    offset: int              # byte offset of this file inside the torrent's concatenated data
    pad: bool = False        # BEP 47 padding file: virtual zeros, never on disk

    @property
    def relpath(self) -> str:
        return "/".join(self.parts)

    @property
    def name(self) -> str:
        return self.parts[-1]

    @property
    def ext(self) -> str:
        return os.path.splitext(self.name)[1].lower()


@dataclass
class Torrent:
    raw: bytes
    name: str
    infohash: str
    piece_length: int
    pieces: list[bytes]
    files: list[TFile]
    multi_file: bool
    private: bool
    trackers: list[str] = field(default_factory=list)

    @property
    def total_size(self) -> int:
        return sum(f.length for f in self.files)

    @property
    def real_files(self) -> list[TFile]:
        return [f for f in self.files if not f.pad]

    def pieces_overlapping(self, offset: int, length: int) -> range:
        if length <= 0:
            return range(0)
        first = offset // self.piece_length
        last = (offset + length - 1) // self.piece_length
        return range(first, last + 1)

    def contained_piece(self, f: TFile) -> int | None:
        """Index of the first piece lying entirely inside ``f``, if any."""
        first = -(-f.offset // self.piece_length)  # ceil
        if (first + 1) * self.piece_length <= f.offset + f.length:
            return first
        return None

    def piece_span(self, index: int) -> tuple[int, int]:
        start = index * self.piece_length
        return start, min(start + self.piece_length, self.total_size)


def _text(d: dict, key: bytes) -> str:
    raw = d.get(key + b".utf-8", d.get(key))
    if raw is None:
        raise TorrentError(f"missing {key.decode()}")
    return raw.decode("utf-8", errors="replace")


def _safe_part(part: str) -> str:
    if part in ("", ".", "..") or "/" in part or "\\" in part or ":" in part:
        raise TorrentError(f"refusing unsafe path component {part!r} in torrent")
    return part


def parse(data: bytes) -> Torrent:
    meta = bencode.decode(data)
    if not isinstance(meta, dict) or not isinstance(meta.get(b"info"), dict):
        raise TorrentError("not a torrent file (no info dictionary)")
    info = meta[b"info"]
    if b"pieces" not in info:
        raise TorrentError("pure BitTorrent v2 torrents are not supported (no v1 piece hashes)")

    start, end = bencode.info_span(data)
    infohash = hashlib.sha1(data[start:end]).hexdigest()

    name = _safe_part(_text(info, b"name"))
    plen = info[b"piece length"]
    blob = info[b"pieces"]
    if len(blob) % 20:
        raise TorrentError("pieces field is not a multiple of 20 bytes")
    pieces = [blob[i:i + 20] for i in range(0, len(blob), 20)]

    files: list[TFile] = []
    offset = 0
    if b"files" in info:
        multi = True
        for entry in info[b"files"]:
            raw_parts = entry.get(b"path.utf-8", entry.get(b"path"))
            parts = tuple(_safe_part(p.decode("utf-8", errors="replace")) for p in raw_parts)
            pad = b"p" in entry.get(b"attr", b"")
            files.append(TFile((name, *parts), entry[b"length"], offset, pad))
            offset += entry[b"length"]
    else:
        multi = False
        files.append(TFile((name,), info[b"length"], 0))
        offset = info[b"length"]

    expected = -(-offset // plen)
    if expected != len(pieces):
        raise TorrentError(f"torrent has {len(pieces)} piece hashes but data needs {expected}")

    trackers = []
    if b"announce" in meta:
        trackers.append(meta[b"announce"].decode(errors="replace"))
    for tier in meta.get(b"announce-list", []):
        for t in tier:
            t = t.decode(errors="replace")
            if t not in trackers:
                trackers.append(t)

    return Torrent(data, name, infohash, plen, pieces, files, multi,
                   bool(info.get(b"private")), trackers)


class PieceVerifier:
    """Hash pieces by reading the torrent's files from ``locate(tfile) -> path``.

    Files that are missing or too short simply make their pieces fail.
    """

    def __init__(self, torrent: Torrent, locate, overrides: dict[str, bytes] | None = None,
                 cache: bool = False):
        self.t = torrent
        self.locate = locate
        self.overrides = overrides if overrides is not None else {}  # relpath -> candidate content
        self._handles: dict[str, object] = {}
        self._cache: dict | None = {} if cache else None

    def close(self):
        for fh in self._handles.values():
            fh.close()
        self._handles.clear()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _read(self, f: TFile, pos: int, n: int) -> bytes:
        if f.pad:
            return bytes(n)
        if f.relpath in self.overrides:
            return self.overrides[f.relpath][pos:pos + n]
        path = self.locate(f)
        if path is None:
            return b""
        key = (path, pos, n)
        if self._cache is not None and key in self._cache:
            return self._cache[key]
        fh = self._handles.get(path)
        if fh is None:
            try:
                fh = open(path, "rb")
            except OSError:
                return b""
            self._handles[path] = fh
        fh.seek(pos)
        data = fh.read(n)
        if self._cache is not None:
            self._cache[key] = data
        return data

    def read_range(self, offset: int, length: int) -> bytes | None:
        out = bytearray()
        end = offset + length
        for f in self.t.files:
            f_end = f.offset + f.length
            if f_end <= offset or f.offset >= end:
                continue
            lo = max(offset, f.offset)
            hi = min(end, f_end)
            chunk = self._read(f, lo - f.offset, hi - lo)
            if len(chunk) != hi - lo:
                return None
            out += chunk
        return bytes(out)

    def check(self, index: int) -> bool:
        start, end = self.t.piece_span(index)
        data = self.read_range(start, end - start)
        return data is not None and hashlib.sha1(data).digest() == self.t.pieces[index]

    def check_bytes_at(self, path: str, f: TFile, index: int) -> bool:
        """Does ``path`` hold the right bytes for piece ``index`` (which must lie wholly inside ``f``)?"""
        start, end = self.t.piece_span(index)
        try:
            with open(path, "rb") as fh:
                fh.seek(start - f.offset)
                data = fh.read(end - start)
        except OSError:
            return False
        return hashlib.sha1(data).digest() == self.t.pieces[index]
