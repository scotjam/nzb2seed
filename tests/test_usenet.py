"""Choosing and retrying Usenet posts, post-processing choice, and odd layouts."""
import hashlib
import os

import pytest

from nzb2seed import assemble as asm
from nzb2seed import bencode, nzbinfo, pipeline
from nzb2seed.clients import PP_REPAIR, PP_UNPACK, Release
from nzb2seed.config import Config
from nzb2seed.pipeline import Abort, Options
from nzb2seed.torrent import parse

from helpers import BASE, NAME, make_torrent, nzb_job, rnd, scene_layout

MKV = "Movie.2020.1080p.BluRay.x264-GRP"


def unpacked_layout():
    return {
        f"{MKV.lower()}.mkv": rnd(90_000, 21),
        f"{MKV.lower()}.nfo": b"GRP\r\nMovie\r\n",
        f"Sample/{MKV.lower()}-sample.mkv": rnd(20_000, 22),
        f"Subs/{MKV.lower()}.subs.rar": rnd(4_000, 23),   # small rar in an unpacked release
    }


def single_file_torrent(data: bytes, name: str) -> bytes:
    pieces = b"".join(hashlib.sha1(data[i:i + 16384]).digest() for i in range(0, len(data), 16384))
    return bencode.encode({"info": {"name": name, "length": len(data), "piece length": 16384,
                                    "pieces": pieces}})


# ---------------------------------------------------------------- post-processing choice

def test_rar_release_is_repair_only():
    t = parse(make_torrent(NAME, scene_layout()))
    assert pipeline.packed_release(t)
    assert pipeline.choose_pp(Options(), Config(path="x"), t) == PP_REPAIR


def test_unpacked_release_with_subs_rar_may_unpack():
    t = parse(make_torrent(MKV, unpacked_layout()))
    assert not pipeline.packed_release(t)
    assert pipeline.choose_pp(Options(), Config(path="x"), t) == PP_UNPACK


def test_single_raw_file_may_unpack_and_config_overrides():
    t = parse(single_file_torrent(rnd(50_000, 1), "movie.mkv"))
    assert pipeline.choose_pp(Options(), Config(path="x"), t) == PP_UNPACK
    assert pipeline.choose_pp(Options(pp="repair"), Config(path="x"), t) == PP_REPAIR
    assert pipeline.choose_pp(Options(), Config(path="x", post_processing="repair"), t) == PP_REPAIR


# ---------------------------------------------------------------- layouts

def test_single_raw_file_from_nested_unpacked_job(tmp_path):
    data = rnd(70_000, 30)
    t = parse(single_file_torrent(data, f"{MKV}.mkv"))
    job = tmp_path / "ssd" / "Totally Different Job Name"
    (job / "abc123" / "inner").mkdir(parents=True)
    (job / "abc123" / "inner" / "e9f1c0d7.mkv").write_bytes(data)          # obfuscated + nested
    for n in ("x.rar", "x.r00", "x.par2", "x.nfo"):
        (job / n).write_bytes(b"junk" * 10)
    out = tmp_path / "hdd" / "complete"
    res = asm.assemble(t, [str(job)], str(out), log=lambda *_: None)
    assert res.complete and asm.verify_all(t, res) == []
    asm.cleanup(t, res, [str(job)], str(out), log=lambda *_: None)
    assert [p.name for p in out.iterdir()] == [f"{MKV}.mkv"]
    assert not job.exists()


def test_torrent_folder_differs_from_job_folder_and_nesting(tmp_path):
    files = unpacked_layout()
    t = parse(make_torrent(MKV, files))
    job = tmp_path / "ssd" / "movie 2020 (usenet post)"
    (job / "deeper" / "Subs").mkdir(parents=True)
    for rel, data in files.items():
        name = rel.split("/")[-1]
        target = job / "deeper" / "Subs" / name if rel.startswith("Subs/") else job / "deeper" / name
        target.write_bytes(data)
    out = tmp_path / "hdd"
    res = asm.assemble(t, [str(job)], str(out), log=lambda *_: None)
    assert res.complete and asm.verify_all(t, res) == []
    assert (out / MKV / "Sample" / f"{MKV.lower()}-sample.mkv").exists()


def test_job_dir_uses_folder_when_sab_reports_a_file(tmp_path):
    job = tmp_path / "downloads" / f"{MKV}"
    job.mkdir(parents=True)
    (job / "a.mkv").write_bytes(b"x")
    cfg = Config(path="x", sab_to_local=[["/downloads", str(tmp_path / "downloads")]])
    assert pipeline.job_dir(cfg, {"storage": f"/downloads/{MKV}/a.mkv"}, MKV) == str(job)
    # a file directly in the shared complete folder is never widened to that folder
    (tmp_path / "downloads" / "loose.mkv").write_bytes(b"x")
    assert pipeline.job_dir(cfg, {"storage": "/downloads/loose.mkv"}, MKV).endswith("loose.mkv")


# ---------------------------------------------------------------- NZB peeking and retries

def nzb_xml(names_sizes) -> bytes:
    files = "".join(
        f'<file subject="[1/9] - &quot;{n}&quot; yEnc (1/1)"><segments>'
        f'<segment bytes="{b}" number="1">x@y</segment></segments></file>' for n, b in names_sizes)
    return f'<?xml version="1.0"?><nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">{files}</nzb>'.encode()


def test_nzb_score_prefers_post_with_torrent_names():
    t = parse(make_torrent(NAME, scene_layout()))
    total = t.total_size
    good = nzbinfo.score(nzbinfo.parse(nzb_xml(
        [(f.name, int(f.length * 1.02)) for f in t.real_files] + [("x.par2", 5000)])), t)
    bare = nzbinfo.score(nzbinfo.parse(nzb_xml([("abcdef.mkv", int(total * 1.02))])), t)
    assert good.name_hits == len(t.real_files) and good.plausible and good.archives
    assert bare.name_hits == 0 and good.key > bare.key


class FakeProwlarr:
    def __init__(self, nzbs):
        self.nzbs, self.fetched = nzbs, []

    def fetch(self, r):
        self.fetched.append(r.guid)
        return self.nzbs[r.guid]


def rel(guid, size, grabs=0):
    return Release(NAME, "usenet", guid, 1, size, guid, "", "", "", grabs, None, None)


def test_rank_posts_drops_smaller_and_same_size_posts():
    t = parse(make_torrent(NAME, scene_layout()))
    T = t.total_size
    good = nzb_xml([(f.name, int(f.length * 1.02)) for f in t.real_files])
    bare = nzb_xml([("abcdef.mkv", int(T * 1.02))])
    pr = FakeProwlarr({"small": bare, "bare": bare, "bare2": bare, "good": good})
    group = [rel("small", T - 1, grabs=9999), rel("bare", T + 10, grabs=500),
             rel("bare2", T + 10, grabs=1), rel("good", T + 20, grabs=3)]
    ranked = pipeline.rank_posts(pr, t, group)
    assert [r.guid for r, _, _ in ranked] == ["good", "bare"]
    assert "small" not in pr.fetched and "bare2" not in pr.fetched


class RetrySAB:
    """First post lacks the nfo and sample; the second is complete."""

    def __init__(self, root):
        self.root, self.n = root, 0
        self.added = []

    def add_nzb(self, nzb, name, cat, pp, prio):
        self.n += 1
        d = os.path.join(self.root, f"{name}.{self.n}")
        nzb_job(d)
        if self.n == 1:
            os.remove(os.path.join(d, f"{BASE}.nfo"))
            os.remove(os.path.join(d, f"{BASE}-sample.mkv"))
        self.added.append((name, pp))
        return f"nzo{self.n}"

    def ensure_pp(self, nzo, pp):
        return "+Repair"

    def status(self, nzo):
        return "Completed", {"storage": f"/dl/{NAME}.{nzo[3:]}"}


def test_usenet_single_moves_on_to_the_next_post(tmp_path):
    t = parse(make_torrent(NAME, scene_layout()))
    T = t.total_size
    names = [(f.name, int(f.length * 1.02)) for f in t.real_files]
    pr = FakeProwlarr({"a": nzb_xml(names), "b": nzb_xml(names[:-1])})
    sab = RetrySAB(str(tmp_path))
    cfg = Config(path="x", sab_to_local=[["/dl", str(tmp_path)]])
    dirs, nzos = pipeline.usenet_single(cfg, Options(), pr, sab, t,
                                        [rel("a", T + 50, 10), rel("b", T + 60, 5)], PP_REPAIR)
    assert nzos == ["nzo2"] and dirs[0].endswith(".2")
    assert len(sab.added) == 2


def test_usenet_single_refuses_when_every_nzb_is_too_small(tmp_path):
    t = parse(make_torrent(NAME, scene_layout()))
    with pytest.raises(Abort):
        pipeline.usenet_single(Config(path="x"), Options(), FakeProwlarr({}), None, t,
                               [rel("a", t.total_size - 1)], PP_REPAIR)


def test_complete_bytes_beat_a_name_hit():
    """A post naming the .nfo but short by the sample's size must lose to an anonymous
    RAR set with exactly the right number of bytes."""
    t = parse(make_torrent(NAME, scene_layout()))
    nfo = next(f for f in t.real_files if f.ext == ".nfo")
    short = nzbinfo.score(nzbinfo.parse(nzb_xml(
        [(nfo.name, nfo.length), ("a.rar", int(t.total_size * 0.95))])), t)
    full = nzbinfo.score(nzbinfo.parse(nzb_xml([("b.rar", int(t.total_size * 1.0))])), t)
    assert full.key > short.key
