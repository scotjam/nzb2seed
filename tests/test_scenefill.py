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
    scenefill.reset()
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
    scenefill.reset()
    assert not scenefill.fill(t, str(d), list(t.real_files))


def test_a_season_pack_is_looked_up_episode_by_episode(tmp_path, monkeypatch):
    """A season pack of scene episodes: each episode folder is a release of its own on
    srrDB, and the pack's name is none - so the episode's name is what is looked up."""
    pack = "Show.2016.S01.1080p.WEB.h264-GRPA"
    files = {f"{REL}/{n}": b for n, b in VOLS.items()}
    files[f"{REL}/Sample/show.2016.s01e01.1080p.web.h264-grpa-sample.mkv"] = b"s" * 900
    t = parse(make_torrent(pack, files))
    asked = []
    monkeypatch.setattr(metadata, "srrdb_details", lambda r: asked.append(r) or (details() if r == REL else None))
    monkeypatch.setattr(metadata, "srrdb_file", lambda r, p: None)
    d = tmp_path / "job"
    d.mkdir()
    (d / "x.mkv").write_bytes(VIDEO)
    monkeypatch.setattr(scenerar, "rebuild", lambda release, det, video, out: (True, f"rebuilt {release}"))
    scenefill.reset()
    sample = next(f for f in t.real_files if "Sample" in f.relpath)
    assert scenefill.release_of(t, sample) == REL                 # a Sample folder is not a release
    assert scenefill.fill(t, str(d), list(t.real_files))
    assert asked == [REL]                                         # never the pack's name
    assert scenefill.inner_sizes(t, list(t.real_files)) == (len(VIDEO),)


# ------------------------------------------------ the many shapes a post can come in

def srr(monkeypatch, det=None, known=(REL,)):
    asked = []
    monkeypatch.setattr(metadata, "srrdb_details",
                        lambda r: asked.append(r) or ((det or details()) if r in known else None))
    monkeypatch.setattr(metadata, "srrdb_file", lambda r, p: None)
    scenefill.reset()
    return asked


def rebuilt(monkeypatch):
    seen = {}

    def rebuild(release, det, video, out):
        seen["release"], seen["video"] = release, video
        return True, "rebuilt"
    monkeypatch.setattr(scenerar, "rebuild", rebuild)
    return seen


class FakeArchives:
    """7-Zip as scenefill sees it: each archive lists members and unpacks them from bytes."""

    def __init__(self, monkeypatch, contents):
        self.contents = contents                          # archive file name -> {member: bytes}
        monkeypatch.setattr(scenefill.archives, "list_contents", self.list)
        monkeypatch.setattr(scenefill.archives, "extract", self.extract)

    def list(self, first):
        c = self.contents.get(os.path.basename(first))
        if c is None:
            raise RuntimeError("Can not open the file as archive")
        return [(n, len(b)) for n, b in c.items()]

    def extract(self, first, members, dest):
        for m in members:
            p = os.path.join(dest, *m.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as fh:
                fh.write(self.contents[os.path.basename(first)][m])


def job(tmp_path, files):
    d = tmp_path / "job"
    d.mkdir()
    for n, b in files.items():
        (d / n).write_bytes(b)
    return str(d)


def test_every_kind_of_archive_set_is_found(tmp_path):
    names = ["a.part01.rar", "a.part02.rar", "b.rar", "b.r00", "c.7z", "d.7z.001", "d.7z.002",
             "e.zip", "f.001", "f.002", "g.part1.rar", "g.part2.rar", "h.part10.rar"]
    for n in names:
        (tmp_path / n).write_bytes(b"x")
    got = sorted(os.path.basename(p) for p in scenefill.archives.archive_sets(str(tmp_path)))
    assert got == ["a.part01.rar", "b.rar", "c.7z", "d.7z.001", "e.zip", "f.001", "g.part1.rar"]


def test_the_video_inside_a_7z_or_zip_repack(tmp_path, monkeypatch):
    t = parse(make_torrent(REL, dict(VOLS)))
    srr(monkeypatch)
    seen = rebuilt(monkeypatch)
    FakeArchives(monkeypatch, {"x.7z": {"a8f3e1.mkv": VIDEO}})
    d = job(tmp_path, {"x.7z": b"7z"})
    assert scenefill.fill(t, d, list(t.real_files))
    assert open(seen["video"], "rb").read() == VIDEO


def test_a_video_posted_as_plain_numbered_pieces_is_joined(tmp_path, monkeypatch):
    t = parse(make_torrent(REL, dict(VOLS)))
    srr(monkeypatch)
    seen = rebuilt(monkeypatch)
    FakeArchives(monkeypatch, {})                         # the pieces are no archive
    half = len(VIDEO) // 2
    d = job(tmp_path, {"v.mkv.001": VIDEO[:half], "v.mkv.002": VIDEO[half:]})
    assert scenefill.fill(t, d, list(t.real_files))
    assert open(seen["video"], "rb").read() == VIDEO


def test_an_archive_inside_an_archive(tmp_path, monkeypatch):
    t = parse(make_torrent(REL, dict(VOLS)))
    srr(monkeypatch)
    seen = rebuilt(monkeypatch)
    FakeArchives(monkeypatch, {"outer.zip": {"inner.rar": b"rar"}, "inner.rar": {"f.mkv": VIDEO}})
    d = job(tmp_path, {"outer.zip": b"zip"})
    assert scenefill.fill(t, d, list(t.real_files))
    assert open(seen["video"], "rb").read() == VIDEO


def test_the_scene_volumes_packed_whole_inside_a_repack_are_taken_as_they_are(tmp_path, monkeypatch):
    t = parse(make_torrent(REL, dict(VOLS)))
    srr(monkeypatch)
    monkeypatch.setattr(scenerar, "rebuild", lambda *a: (_ for _ in ()).throw(AssertionError("rebuilt")))
    FakeArchives(monkeypatch, {"repack.part01.rar": dict(VOLS)})
    d = job(tmp_path, {"repack.part01.rar": b"rar"})
    assert scenefill.fill(t, d, list(t.real_files))
    for n, b in VOLS.items():
        assert open(os.path.join(d, scenefill.SCENE_DIR, n), "rb").read() == b


def test_a_release_whose_rars_held_several_files(tmp_path, monkeypatch):
    t = parse(make_torrent(REL, dict(VOLS)))
    extra = os.urandom(5_000)
    det = details()
    det["archived-files"].append({"name": REL + ".idx", "size": len(extra), "crc": crc(extra)})
    srr(monkeypatch, det)
    seen = rebuilt(monkeypatch)
    d = job(tmp_path, {"v.mkv": VIDEO, "v.idx": extra})
    assert scenefill.fill(t, d, list(t.real_files))
    assert sorted(seen["video"]) == sorted([REL + ".mkv", REL + ".idx"])     # every file, by its inner name


def test_srrdb_is_asked_by_the_names_the_release_goes_by(tmp_path, monkeypatch):
    """Posted as "-xpost" in a folder of another name: the scene volumes' own name finds it."""
    t = parse(make_torrent("Some Folder Name", {f"{REL}-xpost/{n}": b for n, b in VOLS.items()}))
    asked = srr(monkeypatch, known=(REL.lower(),))
    seen = rebuilt(monkeypatch)
    d = job(tmp_path, {"v.mkv": VIDEO})
    assert scenefill.fill(t, d, list(t.real_files))
    assert asked[:2] == [REL + "-xpost", "Some Folder Name"] and seen["release"] == REL.lower()
