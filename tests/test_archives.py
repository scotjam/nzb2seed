"""A download that finished as RAR archives is looked into before it is written off."""
import os

from nzb2seed import archives, pipeline
from nzb2seed.torrent import parse

from helpers import make_torrent, rnd

SLT = """
7-Zip [64] 26.02
Listing archive: x.part01.rar
--
Path = x.part01.rar
Type = Rar5
----------
Path = Movie.2020.1080p.BluRay.x264-GRP.mkv
Folder = -
Size = 123456
Packed Size = 50000000

Path = Sample
Folder = +
Size = 0

Path = Sample/movie-sample.mkv
Folder = -
Size = 999
"""

UNRAR_LT = """
UNRAR 6.21 freeware
Archive: x.part01.rar
Details: RAR 5, volume

        Name: Movie.2020.1080p.BluRay.x264-GRP.mkv
        Type: File
        Size: 123456
 Packed size: 50000000

        Name: Sample
        Type: Directory
        Size: 0
"""


def test_parsers():
    assert archives.parse_7z_slt(SLT.split("----------", 1)[1]) == [
        ("Movie.2020.1080p.BluRay.x264-GRP.mkv", 123456), ("Sample/movie-sample.mkv", 999)]
    assert archives.parse_unrar_lt(UNRAR_LT) == [("Movie.2020.1080p.BluRay.x264-GRP.mkv", 123456)]


def test_rar_sets_finds_first_volumes(tmp_path):
    for n in ["a.part01.rar", "a.part02.rar", "b.rar", "b.r00", "c.mkv"]:
        (tmp_path / n).write_bytes(b"x")
    assert sorted(os.path.basename(p) for p in archives.rar_sets(str(tmp_path))) == ["a.part01.rar", "b.rar"]


def test_episode_inside_rars_is_unpacked_and_counts(tmp_path, monkeypatch):
    video = rnd(3 << 20, 5)                       # big enough that size alone identifies it
    t = parse(make_torrent("Show.S01.720p.HDTV.x264-GRP",
                           {"show.s01e01.720p.hdtv.x264-grp.mkv": video}))
    job = tmp_path / "job"
    job.mkdir()
    for i in range(1, 4):
        (job / f"1085a8f6.part0{i}.rar").write_bytes(b"rar")     # obfuscated RAR set
    monkeypatch.setattr(archives, "list_contents", lambda first: [("1085a8f6.mkv", len(video))])
    extracted = []

    def fake_extract(first, members, dest):
        extracted.append((os.path.basename(first), members))
        os.makedirs(dest, exist_ok=True)
        with open(os.path.join(dest, members[0]), "wb") as fh:
            fh.write(video)
    monkeypatch.setattr(archives, "extract", fake_extract)

    assert pipeline.supplies(t, str(job), "show.s01e01.720p.hdtv.x264-grp.mkv")
    assert extracted == [("1085a8f6.part01.rar", ["1085a8f6.mkv"])]


def test_rars_without_the_file_are_still_a_miss(tmp_path, monkeypatch):
    t = parse(make_torrent("Show.S01.720p.HDTV.x264-GRP",
                           {"show.s01e01.720p.hdtv.x264-grp.mkv": rnd(3 << 20, 6)}))
    job = tmp_path / "job"
    job.mkdir()
    (job / "x.rar").write_bytes(b"rar")
    monkeypatch.setattr(archives, "list_contents", lambda first: [("other.mkv", 12345)])
    monkeypatch.setattr(archives, "extract", lambda *a: (_ for _ in ()).throw(AssertionError("no")))
    assert not pipeline.supplies(t, str(job), "show.s01e01.720p.hdtv.x264-grp.mkv")


class Encrypted:
    """7-Zip on an archive that opens only with its password: a wrong one half-writes a file."""

    def __init__(self, dest=None):
        self.tried, self.dest = [], dest

    def __call__(self, cmd, **kw):
        pw = next(a[2:] for a in cmd if a.startswith("-p"))
        self.tried.append(pw)
        ok = pw == "s3cret"
        if cmd[1] == "x" and self.dest and not ok:
            with open(os.path.join(self.dest, f"junk-{len(self.tried)}.mkv"), "wb") as fh:
                fh.write(b"half")
        out = SLT if ok and cmd[1] == "l" else ""
        return type("R", (), {"returncode": 0 if ok else 2, "stdout": out,
                              "stderr": "" if ok else "ERROR: x.part01.rar : Cannot open encrypted archive. Wrong password?"})()


def test_an_encrypted_repack_is_opened_with_the_password_its_nzb_carried(tmp_path, monkeypatch):
    monkeypatch.setattr(archives, "tool", lambda: ("7z", "7z"))
    monkeypatch.setattr(archives, "_passwords", [])
    monkeypatch.setattr(archives, "_works", {})
    run = Encrypted()
    monkeypatch.setattr(archives.subprocess, "run", run)
    first = str(tmp_path / "x.part01.rar")
    try:
        archives.list_contents(first)
        raise AssertionError("opened without its password")
    except RuntimeError as e:
        assert "encrypted" in str(e)
    archives.remember_password("other")
    archives.remember_password("s3cret")
    assert archives.list_contents(first)[0][0].startswith("Movie.2020")
    run.tried.clear()
    archives.list_contents(first)
    assert run.tried == ["s3cret"]                       # the one that worked goes first


def test_a_wrong_password_leaves_nothing_behind(tmp_path, monkeypatch):
    monkeypatch.setattr(archives, "tool", lambda: ("7z", "7z"))
    monkeypatch.setattr(archives, "_passwords", ["s3cret"])
    monkeypatch.setattr(archives, "_works", {})
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "mine.txt").write_text("kept")               # already there before: never removed
    monkeypatch.setattr(archives.subprocess, "run", Encrypted(str(dest)))
    archives.extract(str(tmp_path / "x.part01.rar"), ["Movie.mkv"], str(dest))
    assert sorted(os.listdir(dest)) == ["mine.txt"]      # the "-" attempt's half-file is gone
