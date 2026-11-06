"""A built file's own bytes are kept before anything changes them (originals.py), and a
torrent that needs that version is finished from it (mend.py)."""
import os
import time
from types import SimpleNamespace

import pytest

from nzb2seed import mend, originals

BUILT, FIXED, OTHER = b"usnt" * 1000, b"trkr" * 1000, b"othr" * 1000
REL = "Rel/ep.mkv"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(originals, "_drive_root", lambda p: str(tmp_path / "drive"))
    (tmp_path / "drive").mkdir()
    return SimpleNamespace(path=str(tmp_path / "nzb2seed.toml"), keep_originals_days=30,
                           local_to_qbit=[])


def _file(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_the_built_version_survives_qbittorrent_writing_into_the_file(tmp_path, cfg):
    built = _file(tmp_path / "drive" / "torrents" / "Rel" / "ep.mkv", BUILT)
    assert originals.keep_before_writes(cfg, str(built), REL, "h" * 40)
    with open(built, "r+b") as fh:                      # what qBittorrent's download does
        fh.write(FIXED)
    [kept] = originals.candidates(cfg, REL, len(BUILT))
    assert open(kept, "rb").read() == BUILT
    assert built.read_bytes() == FIXED
    assert ".nzb2seed-originals" in kept


def test_only_the_first_version_is_kept(tmp_path, cfg):
    built = _file(tmp_path / "drive" / "t" / "Rel" / "ep.mkv", BUILT)
    first = originals.keep(cfg, str(built), REL, "h" * 40)
    os.remove(built)
    _file(built, OTHER)                                  # replaced by another post's copy
    assert originals.keep(cfg, str(built), REL, "h" * 40) == first
    assert open(first, "rb").read() == BUILT


def test_nothing_is_kept_when_switched_off(tmp_path, cfg):
    cfg.keep_originals_days = 0
    built = _file(tmp_path / "drive" / "t" / "Rel" / "ep.mkv", BUILT)
    assert originals.keep(cfg, str(built), REL, "h" * 40) is None
    assert originals.candidates(cfg, REL, len(BUILT)) == []


def test_an_old_original_is_let_go_but_a_torrent_using_it_keeps_the_bytes(tmp_path, cfg):
    built = _file(tmp_path / "drive" / "t" / "Rel" / "ep.mkv", BUILT)
    kept = originals.keep(cfg, str(built), REL, "h" * 40)
    user = tmp_path / "drive" / "cross-seed" / "Rel" / "ep.mkv"
    user.parent.mkdir(parents=True)
    os.link(kept, user)                                  # a torrent mend finished from it
    assert originals.prune(cfg, now=time.time() + 31 * 86400) == 1
    assert not os.path.exists(kept)
    assert not os.path.exists(tmp_path / "drive" / originals.DIR)   # empty folders tidied
    assert user.read_bytes() == BUILT
    assert originals.candidates(cfg, REL, len(BUILT)) == []


class FakeQb:
    """One torrent named Rel, stopped short: its file verifies only with the built bytes."""

    def __init__(self, save_dir):
        self.save_dir = save_dir
        self.prog = 0.5
        self.started = []

    def torrents(self):
        return [{"hash": "short", "name": "Rel", "progress": self.prog,
                 "state": "stalledUP" if self.started else "stoppedDL", "save_path": self.save_dir}]

    def info(self, h):
        return self.torrents()[0]

    def files(self, h):
        return [{"name": REL, "size": len(BUILT), "progress": self.prog, "priority": 1}]

    def recheck(self, h):
        with open(os.path.join(self.save_dir, *REL.split("/")), "rb") as fh:
            self.prog = 1.0 if fh.read() == BUILT else 0.5

    def wait_idle(self, h, **kw):
        return self.info(h)

    def start(self, h):
        self.started.append(h)


def test_a_cross_seed_wanting_the_built_version_is_finished_from_the_kept_original(tmp_path, cfg):
    # the build: kept, then changed by qBittorrent into the tracker's version
    built = _file(tmp_path / "drive" / "torrents" / "Rel" / "ep.mkv", BUILT)
    originals.keep_before_writes(cfg, str(built), REL, "z" * 40)
    with open(built, "r+b") as fh:
        fh.write(FIXED)
    # a cross-seed of another tracker, linked to the changed file: short
    xs = tmp_path / "drive" / "cross-seed" / "T2" / "Rel" / "ep.mkv"
    xs.parent.mkdir(parents=True)
    os.link(built, xs)
    qb = FakeQb(str(tmp_path / "drive" / "cross-seed" / "T2"))

    m = object.__new__(mend.Mender)
    m.app = SimpleNamespace(cfg=cfg, _qbit=lambda: qb, jobs={})
    m.state = mend.State(str(tmp_path / "mend.json"))
    m.round()

    assert qb.prog == 1.0 and qb.started == ["short"]
    assert xs.read_bytes() == BUILT
    assert built.read_bytes() == FIXED                   # the other torrent's file is untouched
    assert "kept" in m.state.log[-1][1]


def test_short_files_are_kept_before_a_build_is_handed_to_qbittorrent(tmp_path, cfg):
    from nzb2seed import gui
    good = _file(tmp_path / "drive" / "out" / "Rel" / "a.mkv", FIXED)
    short = _file(tmp_path / "drive" / "out" / "Rel" / "ep.mkv", BUILT)
    qb = SimpleNamespace(files=lambda h: [
        {"name": "Rel/a.mkv", "size": len(FIXED), "progress": 1.0, "priority": 1},
        {"name": REL, "size": len(BUILT), "progress": 0.99, "priority": 1}])
    app = object.__new__(gui.App)
    app.keep_originals(cfg, qb, "h" * 40, str(tmp_path / "drive" / "out"))
    [kept] = originals.candidates(cfg, REL, len(BUILT))
    assert os.stat(kept).st_ino != os.stat(short).st_ino      # the torrent writes into its own copy
    assert originals.candidates(cfg, "Rel/a.mkv", len(FIXED)) == []
    assert os.stat(good).st_nlink == 1


def test_a_kept_original_is_tried_before_another_torrents_copy(tmp_path, cfg):
    """Stuck a little short, the torrent was linked from the files already here - the
    whole torrent's version - so the kept original goes first and one recheck does it."""
    built = _file(tmp_path / "drive" / "torrents" / "Rel" / "ep.mkv", BUILT)
    originals.keep_before_writes(cfg, str(built), REL, "z" * 40)
    with open(built, "r+b") as fh:
        fh.write(FIXED)
    other = _file(tmp_path / "drive" / "elsewhere" / "Rel" / "ep.mkv", FIXED)   # another copy, same version
    xs = tmp_path / "drive" / "cross-seed" / "T2" / "Rel" / "ep.mkv"
    xs.parent.mkdir(parents=True)
    os.link(built, xs)
    qb = FakeQb(str(tmp_path / "drive" / "cross-seed" / "T2"))
    rechecks = []
    real = qb.recheck
    qb.recheck = lambda h: (rechecks.append(h), real(h))
    whole = {"hash": "whole", "name": "Rel", "save_path": str(tmp_path / "drive" / "elsewhere")}
    qb.files_of = {"whole": [{"name": REL, "size": len(FIXED), "progress": 1.0, "priority": 1}]}
    files = qb.files
    qb.files = lambda h: qb.files_of[h] if h in qb.files_of else files(h)

    m = object.__new__(mend.Mender)
    m.app = SimpleNamespace(cfg=cfg, _qbit=lambda: qb, jobs={})
    m.state = mend.State(str(tmp_path / "mend.json"))
    from nzb2seed.pathmap import map_path
    assert m.mend(qb, qb.torrents()[0], [whole], [], map_path)
    assert rechecks == ["short"] and xs.read_bytes() == BUILT
    assert other.read_bytes() == FIXED
