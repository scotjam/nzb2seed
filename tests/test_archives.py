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
