"""A RAR'd scene torrent built from a re-packed post: the original volumes are rebuilt
from srrDB (stubbed here), the .nfo comes from srrDB, and the unit is then satisfied."""
import os
import zlib

from nzb2seed import metadata, pipeline, scenefill, scenerar
from nzb2seed.torrent import parse

from helpers import make_torrent

REL = "Show.2016.S01E01.1080p.WEB.h264-GRPA"
VIDEO = os.urandom(300_000)
VOLS = {"show.2016.s01e01.1080p.web.h264-grpa.rar": os.urandom(150_000),
        "show.2016.s01e01.1080p.web.h264-grpa.r00": os.urandom(150_123)}
NFO = b"GRPA presents\r\n"


def crc(b):
    return f"{zlib.crc32(b) & 0xFFFFFFFF:08X}"


def details():
    files = [{"name": n, "size": len(b), "crc": crc(b)} for n, b in VOLS.items()]
    files.append({"name": "show.2016.s01e01.1080p.web.h264-grpa.nfo", "size": len(NFO), "crc": crc(NFO)})
    return {"files": files, "archived-files": [{"name": REL + ".mkv", "size": len(VIDEO), "crc": crc(VIDEO)}]}


def test_repack_is_turned_into_the_scene_release(tmp_path, monkeypatch):
    t = parse(make_torrent(REL, {**VOLS, "show.2016.s01e01.1080p.web.h264-grpa.nfo": NFO}))
    d = tmp_path / "job"
    d.mkdir()
    (d / "a8f3e1.mkv").write_bytes(VIDEO)            # the video, under an obfuscated name
    (d / "a8f3e1.par2").write_bytes(b"par")
    monkeypatch.setattr(metadata, "srrdb_details", lambda r: details() if r == REL else None)
    monkeypatch.setattr(metadata, "srrdb_file", lambda r, p: NFO if p.endswith(".nfo") else None)
    seen = {}

    def rebuild(release, det, video, out):
        seen["video"] = open(video, "rb").read()
        os.makedirs(out, exist_ok=True)
        for n, b in VOLS.items():
            with open(os.path.join(out, n), "wb") as fh:
                fh.write(b)
        return True, "rebuilt 2 scene RAR volume(s) from srrDB's .srr; every CRC matches"
    monkeypatch.setattr(scenerar, "rebuild", rebuild)
    scenefill._done.clear()
    unit = pipeline.build_units(t, t.name)
    fill = lambda folder, missing: scenefill.fill(t, folder, missing)  # noqa: E731
    assert unit.satisfied_by(t, str(d), fill)
    assert seen["video"] == VIDEO
    assert (d / scenefill.SCENE_DIR / "show.2016.s01e01.1080p.web.h264-grpa.nfo").read_bytes() == NFO


def test_a_reencoded_video_is_not_used(tmp_path, monkeypatch):
    t = parse(make_torrent(REL, dict(VOLS)))
    d = tmp_path / "job"
    d.mkdir()
    (d / "video.mkv").write_bytes(os.urandom(len(VIDEO)))   # right size, other bytes
    monkeypatch.setattr(metadata, "srrdb_details", lambda r: details())
    monkeypatch.setattr(scenerar, "rebuild", lambda *a: (_ for _ in ()).throw(AssertionError("rebuilt")))
    scenefill._done.clear()
    assert not scenefill.fill(t, str(d), list(t.real_files))
