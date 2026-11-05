"""Stopped torrents finished from another torrent's good files (mend.py)."""
import os
from types import SimpleNamespace

from nzb2seed import mend

GOOD, BAD = b"good" * 1000, b"baad" * 1000


class FakeQb:
    """Two torrents named Rel: "short" (stopped, its file checked against GOOD) and "whole"."""

    def __init__(self, short_dir, whole_dir):
        self.dirs = {"short": short_dir, "whole": whole_dir}
        self.prog = {"short": 0.5, "whole": 1.0}
        self.started = []
        self.state = {"short": "stoppedDL", "whole": "stalledUP"}

    def torrents(self):
        return [{"hash": h, "name": "Rel", "progress": self.prog[h], "state": self.state[h],
                 "save_path": self.dirs[h]} for h in self.dirs]

    def info(self, h):
        return next(r for r in self.torrents() if r["hash"] == h)

    def files(self, h):
        return [{"name": "Rel/ep.mkv", "size": len(GOOD), "progress": self.prog[h], "priority": 1}]

    def recheck(self, h):
        with open(os.path.join(self.dirs[h], "Rel", "ep.mkv"), "rb") as fh:
            self.prog[h] = 1.0 if fh.read() == GOOD else 0.5

    def wait_idle(self, h, **kw):
        return self.info(h)

    def start(self, h):
        self.started.append(h)
        self.state[h] = "stalledUP"


def _setup(tmp_path, theirs=GOOD):
    lib_file = tmp_path / "tv" / "ep.mkv"
    lib_file.parent.mkdir()
    lib_file.write_bytes(BAD)
    short = tmp_path / "short" / "Rel"
    short.mkdir(parents=True)
    os.link(lib_file, short / "ep.mkv")                 # linked from the library
    whole = tmp_path / "whole" / "Rel"
    whole.mkdir(parents=True)
    (whole / "ep.mkv").write_bytes(theirs)
    qb = FakeQb(str(tmp_path / "short"), str(tmp_path / "whole"))
    m = object.__new__(mend.Mender)
    m.app = SimpleNamespace(cfg=SimpleNamespace(local_to_qbit=[]), _qbit=lambda: qb, jobs={})
    m.state = mend.State(str(tmp_path / "mend.json"))
    return m, qb, lib_file, short / "ep.mkv"


def test_a_stopped_torrent_is_finished_from_another_torrents_file(tmp_path):
    m, qb, lib_file, mine = _setup(tmp_path)
    m.round()
    assert qb.prog["short"] == 1.0 and qb.started == ["short"]
    assert mine.read_bytes() == GOOD
    assert lib_file.read_bytes() == BAD                 # the library's file is never written into
    assert not os.path.exists(str(mine) + mend.ORIG)
    assert "finished" in m.state.log[-1][1]


def test_a_copy_that_is_no_better_is_put_back_and_not_tried_again(tmp_path):
    m, qb, lib_file, mine = _setup(tmp_path, theirs=b"othr" * 1000)
    m.round()
    assert qb.prog["short"] == 0.5 and qb.started == []
    assert os.stat(mine).st_ino == os.stat(lib_file).st_ino   # the old file is back
    assert not os.path.exists(str(mine) + mend.ORIG)
    calls = []
    qb.recheck = lambda h: calls.append(h)
    m.round()
    assert calls == []                                  # that copy was tried already


def test_a_torrent_a_job_works_on_is_left_alone(tmp_path):
    m, qb, lib_file, mine = _setup(tmp_path)
    m.app.jobs = {1: SimpleNamespace(infohash="short", status="running")}
    m.round()
    assert qb.prog["short"] == 0.5 and mine.read_bytes() == BAD


def test_a_swap_cut_off_by_a_restart_is_undone(tmp_path):
    m, qb, lib_file, mine = _setup(tmp_path)
    mend.put_in(str(tmp_path / "whole" / "Rel" / "ep.mkv"), str(mine))
    m.state.pending = [[str(mine), str(mine) + mend.ORIG]]
    m._undo_pending()
    assert os.stat(mine).st_ino == os.stat(lib_file).st_ino and m.state.pending == []



def test_a_search_for_posts_goes_to_the_usenet_indexers_only():
    from nzb2seed import pipeline
    cfg = SimpleNamespace(indexer_ids=[])
    pr = SimpleNamespace(usenet_ids=lambda: [3, 4])
    assert pipeline.usenet_indexers(cfg, pr) == [3, 4]
    assert pipeline.usenet_indexers(SimpleNamespace(indexer_ids=[4, 9]), pr) == [4]
    assert pipeline.usenet_indexers(cfg, SimpleNamespace()) == []      # Prowlarr cannot say: as before


def test_old_downloads_nothing_uses_are_removed(tmp_path, monkeypatch):
    from nzb2seed import pipeline
    root = tmp_path / "complete" / "nzb2seed"

    def folder(name, age_days):
        d = root / name
        d.mkdir(parents=True)
        f = d / "a.7z.001"
        f.write_bytes(b"x" * 10)
        t = 1_000_000 - age_days * 86400
        os.utime(f, (t, t))
        os.utime(d, (t, t))
        return d
    old = folder("Dropped.Post", 5)
    new = folder("Fresh.Post", 1)
    queued = folder("Still.Queued", 5)
    seeded = folder("Seeded.Here", 5)
    building = folder("Busy.Build.S01E01", 5)
    sab = SimpleNamespace(
        config=lambda: {"misc": {"complete_dir": str(tmp_path / "complete")},
                        "categories": [{"name": "nzb2seed", "dir": "nzb2seed"}]},
        _call=lambda *a, **k: {"queue": {"slots": [{"filename": "Still.Queued"}]}})
    qb = SimpleNamespace(torrents=lambda: [{"content_path": str(seeded / "a.7z.001")}])
    cfg = SimpleNamespace(sab_category="nzb2seed", sab_to_local=[], local_to_qbit=[],
                          path=str(tmp_path / "nzb2seed.toml"))
    pipeline.ledger_for(cfg).record("nzo1", "Busy.Build.S01E01", torrent="abc")
    monkeypatch.setattr(pipeline, "_building", {"abc"})
    n, freed = pipeline.remove_old_downloads(cfg, sab, qb, 3, now=1_000_000, log=lambda t: None)
    assert (n, freed) == (1, 10)
    assert not old.exists() and new.exists() and queued.exists() and seeded.exists() and building.exists()
    folder("Another.Dropped", 5)            # qBittorrent not answering: nothing is removed
    assert pipeline.remove_old_downloads(cfg, sab, None, 3, now=1_000_000) == (0, 0)
