import hashlib
import os
import random

from nzb2seed import bencode

NAME = "Show.S01E01.1080p.BluRay.x264-GRP"
BASE = NAME.lower()
NFO_CRLF = (b"   GRP presents\r\n\r\n  Show S01E01\r\n  1080p BluRay\r\n" * 5)


def rnd(n: int, seed: int) -> bytes:
    return random.Random(seed).randbytes(n)


def scene_layout() -> dict[str, bytes]:
    """A typical scene release as it appears in a torrent (relative to the torrent root)."""
    files = {
        f"{BASE}.rar": rnd(50_000, 1),
        f"{BASE}.r00": rnd(50_000, 2),
        f"{BASE}.r01": rnd(50_000, 3),
        f"{BASE}.r02": rnd(12_345, 4),
        f"{BASE}.nfo": NFO_CRLF,
        f"{BASE}.sfv": b"; sfv\r\n" + b"x" * 90,
        f"Sample/{BASE}-sample.mkv": rnd(40_000, 5),
        f"Proof/{BASE}-proof.jpg": rnd(3_000, 6),
        f"Subs/{BASE}.subs.rar": rnd(9_000, 7),
        f"Subs/{BASE}.subs.sfv": b"; subs sfv\n" + b"y" * 40,
    }
    return files


def make_torrent(name: str, files: dict[str, bytes], piece_length: int = 16384) -> bytes:
    rels = list(files)
    blob = b"".join(files[r] for r in rels)
    pieces = b"".join(hashlib.sha1(blob[i:i + piece_length]).digest()
                      for i in range(0, len(blob), piece_length))
    info = {
        "name": name,
        "piece length": piece_length,
        "pieces": pieces,
        "private": 1,
        "files": [{"length": len(files[r]), "path": r.split("/")} for r in rels],
    }
    return bencode.encode({"announce": "https://tracker.example/announce/PASSKEY", "info": info})


def write_flat(job_dir: str, files: dict[str, bytes], rename: dict[str, str] | None = None):
    """Write files the way an NZB download delivers them: all in one folder."""
    os.makedirs(job_dir, exist_ok=True)
    rename = rename or {}
    for rel, data in files.items():
        base = rename.get(rel, rel.split("/")[-1])
        with open(os.path.join(job_dir, base), "wb") as fh:
            fh.write(data)


def nzb_job(job_dir: str, obfuscate=()):
    """Scene layout, flattened, nfo with LF endings, plus par2/nzb junk."""
    files = scene_layout()
    files[f"{BASE}.nfo"] = NFO_CRLF.replace(b"\r\n", b"\n")
    rename = {rel: f"a{i}b9f3c1d2e" for i, rel in enumerate(obfuscate)}
    write_flat(job_dir, files, rename)
    for n, data in {f"{BASE}.par2": b"PAR2" * 10, f"{BASE}.vol0+1.par2": b"PAR2" * 99,
                    f"{NAME}.nzb": b"<?xml?><nzb></nzb>", "readme_from_poster.txt": b"hi"}.items():
        with open(os.path.join(job_dir, n), "wb") as fh:
            fh.write(data)
