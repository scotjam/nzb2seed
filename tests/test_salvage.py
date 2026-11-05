"""When no post is complete: build from every download - complete files first, the
incomplete posts (missing articles, or failed by SABnzbd) last, closest first - and count
the gaps they leave before deciding anything."""
import os

import pytest

from nzb2seed import archives
from nzb2seed import assemble as asm
from nzb2seed import pipeline
from nzb2seed.config import Config
from nzb2seed.pipeline import Options, plan_and_fetch
from nzb2seed.torrent import parse

from helpers import make_torrent, rnd
from test_planner import PR, SAB, ep_name, rel

NAME = "Show.2020.S01.720p.HDTV.x264-GRP"


def season():
    return {f"Season 1/{ep_name(1, e)}": rnd(40_000 + e * 1000, e).replace(b"\0", b"\1") for e in (1, 2, 3)}


class HoledSAB(SAB):
    """``holes``: {guid: {file name: (start, end)}} zeroed like missing articles;
    ``salvaged``: guids SABnzbd fails but whose folder holds what came down."""

    def __init__(self, *a, holes=None, salvaged=(), **k):
        super().__init__(*a, **k)
        self.holes, self.salvaged = holes or {}, set(salvaged)

    def add_nzb(self, nzb, name, cat, pp, prio):
        nzo = super().add_nzb(nzb, name, cat, pp, prio)
        guid = self.jobs[nzo]
        for base, (s, e) in self.holes.get(guid, {}).items():
            p = os.path.join(self.root, nzo, base)
            with open(p, "r+b") as fh:
                fh.seek(s)
                fh.write(b"\0" * (e - s))
        return nzo

    def status(self, nzo):
        if self.jobs[nzo] in self.salvaged:
            return "Failed", {"storage": f"/dl/{nzo}", "fail_message": "Repair failed, not enough repair blocks"}
        return super().status(nzo)

    def history_slot(self, nzo):
        return {"stage_log": []}               # never checked against repair data


@pytest.fixture(autouse=True)
def small_gaps(monkeypatch):
    monkeypatch.setattr(pipeline, "GAP_MIN", 1024)
    monkeypatch.setattr(pipeline, "GAP_FILE_MIN", 0)


def world(tmp_path):
    files = season()
    t = parse(make_torrent(NAME, files))
    cfg = Config(path=str(tmp_path / "c.toml"), sab_to_local=[["/dl", str(tmp_path / "dl")]])
    size = sum(len(v) for v in files.values())
    return t, cfg, files, size


def test_complete_files_come_first_and_the_incomplete_post_fills_the_rest(tmp_path):
    t, cfg, files, size = world(tmp_path)
    a, b = rel(NAME, size, "A"), rel(NAME + "-xpost", size, "B")
    sab = HoledSAB(str(tmp_path / "dl"), t, files, lacks={"A": {ep_name(1, 3)}},
                   holes={"B": {ep_name(1, 1): (8000, 20000), ep_name(1, 3): (2000, 6000)}})
    dirs, _, _, rejected = plan_and_fetch(cfg, Options(), PR([a, b]), sab, t, t.name, [], 2)
    da, db = (os.path.join(str(tmp_path / "dl"), n) for n in ("nzo1", "nzo2"))
    assert da in dirs and db in dirs and dirs.index(da) < dirs.index(db)
    assert all(d != da for d, _ in rejected)          # the complete post is used, not thrown away
    src = asm.find_sources(t, dirs)
    by_name = {os.path.basename(r): p for r, p in src.items()}
    assert by_name[ep_name(1, 1)].startswith(da)      # sound copy, not the holed one
    assert by_name[ep_name(1, 3)].startswith(db)      # only the incomplete post has it


def test_the_closest_incomplete_post_is_the_one_built_from(tmp_path):
    t, cfg, files, size = world(tmp_path)
    b, c = rel(NAME + "-a", size, "B"), rel(NAME + "-b", size, "C")
    sab = HoledSAB(str(tmp_path / "dl"), t, files,
                   holes={"B": {ep_name(1, 1): (1000, 30000)}, "C": {ep_name(1, 2): (1000, 3000)}})
    dirs, _, _, _ = plan_and_fetch(cfg, Options(), PR([b, c]), sab, t, t.name, [], 2)
    db, dc = (os.path.join(str(tmp_path / "dl"), n) for n in ("nzo1", "nzo2"))
    assert dirs.index(dc) < dirs.index(db)            # fewer bytes missing: first


def test_a_post_sabnzbd_failed_is_kept_as_a_last_resort(tmp_path):
    t, cfg, files, size = world(tmp_path)
    a, f = rel(NAME, size, "A"), rel(NAME + "-xpost", size, "F")
    sab = HoledSAB(str(tmp_path / "dl"), t, files, lacks={"A": {ep_name(1, 3)}},
                   holes={"F": {ep_name(1, 3): (4000, 9000)}}, salvaged={"F"})
    dirs, _, _, _ = plan_and_fetch(cfg, Options(), PR([a, f]), sab, t, t.name, [], 2)
    df = os.path.join(str(tmp_path / "dl"), "nzo2")
    assert df in dirs
    assert asm.find_sources(t, dirs)[next(r.relpath for r in t.real_files
                                          if r.name == ep_name(1, 3))].startswith(df)


def test_zero_runs_across_read_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "ZERO_CHUNK", 1000)
    data = bytearray(rnd(5000, 7).replace(b"\0", b"\1"))
    data[900:2600] = b"\0" * 1700                     # across two block boundaries
    data[3000:3100] = b"\0" * 100                     # too short to be an article
    p = tmp_path / "f.bin"
    p.write_bytes(bytes(data))
    assert pipeline.zero_runs(str(p), 1024) == [(900, 2600)]


def test_gaps_are_counted_as_the_pieces_they_touch(tmp_path):
    files = {"a.mkv": rnd(40_000, 1).replace(b"\0", b"\1"), "b.mkv": rnd(30_000, 2)}
    t = parse(make_torrent("X.2020.1080p.WEB-GRP", files))
    out = tmp_path / "a.mkv"
    data = bytearray(files["a.mkv"])
    data[20_000:22_000] = b"\0" * 2000
    out.write_bytes(bytes(data))
    rel_a = next(f.relpath for f in t.real_files if f.name == "a.mkv")
    damaged, pieces = pipeline.holed_damage(t, {rel_a: str(out)}, {rel_a})
    assert [f.name for f, _ in damaged] == ["a.mkv"] and damaged[0][1] == 2000
    assert pieces == {1}                              # 16 KiB pieces: bytes 20000-22000


def test_a_cut_off_file_from_a_broken_rar_set_is_padded_to_its_size(tmp_path, monkeypatch):
    d = tmp_path / "job"
    d.mkdir()
    first = str(d / "x.part01.rar")
    monkeypatch.setattr(archives, "archive_sets", lambda _d: [first])
    monkeypatch.setattr(pipeline, "_contents", lambda _f: ([("Show/ep.mkv", 50_000)], True))

    def fake_salvage(first_volume, members, dest):
        os.makedirs(os.path.join(dest, "Show"), exist_ok=True)
        p = os.path.join(dest, "Show", "ep.mkv")
        with open(p, "wb") as fh:
            fh.write(b"\1" * 30_000)                  # the set broke off here
        return [p]
    monkeypatch.setattr(archives, "salvage", fake_salvage)

    class F:
        name, length = "ep.mkv", 50_000
    assert pipeline.salvage_archives(str(d), [F()])
    out = d / archives.UNPACK_DIR / "Show" / "ep.mkv"
    assert out.stat().st_size == 50_000
    assert pipeline.zero_runs(str(out), 1024) == [(30_000, 50_000)]
