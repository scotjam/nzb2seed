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

    def piece_states(self, h):
        return [2 if self.prog[h] >= 1 else 0]


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



def test_a_torrent_named_after_its_file_is_finished_from_one_named_after_a_folder(tmp_path):
    m, qb, lib_file, mine = _setup(tmp_path)
    whole = tmp_path / "whole"
    os.replace(whole / "Rel" / "ep.mkv", whole / "ep.mkv")       # a single-file torrent
    names = {"short": "Rel", "whole": "Rel.mkv"}
    paths = {"short": "Rel/ep.mkv", "whole": "ep.mkv"}
    rows = qb.torrents
    qb.torrents = lambda: [dict(r, name=names[r["hash"]]) for r in rows()]
    qb.files = lambda h: [{"name": paths[h], "size": len(GOOD), "progress": qb.prog[h], "priority": 1}]
    m.round()
    assert qb.prog["short"] == 1.0 and mine.read_bytes() == GOOD


def test_an_unfinished_torrents_files_are_found_in_its_download_folder(tmp_path):
    m, qb, lib_file, mine = _setup(tmp_path)
    rows = qb.torrents
    qb.torrents = lambda: [dict(r, save_path=str(tmp_path / "later"), download_path=r["save_path"],
                                content_path=os.path.join(r["save_path"], "Rel"))
                           if r["hash"] == "short" else r for r in rows()]
    m.round()
    assert qb.prog["short"] == 1.0 and mine.read_bytes() == GOOD


def _alone(tmp_path, monkeypatch, ticked=("TrackerX",), percent=10, progress=0.99):
    """Only the short torrent, its file linked from the library: nothing here to try."""
    from nzb2seed import originals
    monkeypatch.setattr(originals, "_drive_root", lambda p: str(tmp_path))
    m, qb, lib_file, mine = _setup(tmp_path)
    del qb.dirs["whole"]
    qb.prog["short"] = progress
    rows = qb.torrents
    qb.torrents = lambda: [dict(r, tracker="https://tracker.example/announce", total_size=1000,
                                amount_left=round(1000 * (1 - qb.prog[r["hash"]])), added_on=1)
                           for r in rows()]
    m.app.cfg = SimpleNamespace(local_to_qbit=[], path=str(tmp_path / "nzb2seed.toml"), keep_originals_days=3,
                                nearly_auto_trackers=list(ticked),
                                nearly_limits=[{"tracker": "TrackerX", "percent": percent, "mb": None}])
    m.tracker = lambda r: "TrackerX"
    return m, qb, lib_file, mine


def test_nothing_here_to_try_downloads_the_rest_from_a_ticked_tracker_within_its_limit(tmp_path, monkeypatch):
    from nzb2seed import btledger
    m, qb, lib_file, mine = _alone(tmp_path, monkeypatch)
    m.round()
    assert qb.started == ["short"]
    assert os.stat(mine).st_ino != os.stat(lib_file).st_ino     # its own bytes to write into
    with open(mine, "r+b") as fh:                               # what qBittorrent's download does
        fh.write(GOOD)
    assert lib_file.read_bytes() == BAD                         # the library's file is untouched
    assert btledger.taken(m.app.cfg, "short", None) == 10       # counted against the tracker
    assert "downloading the last" in m.state.log[-1][1]
    qb.state["short"] = "stoppedDL"                             # stopped again by you: left alone
    from nzb2seed.pathmap import map_path
    assert m.download_rest(qb, qb.info("short"), [], map_path) is False


def test_a_tracker_not_ticked_is_not_downloaded_from(tmp_path, monkeypatch):
    m, qb, lib_file, mine = _alone(tmp_path, monkeypatch, ticked=())
    m.round()
    assert qb.started == [] and os.stat(mine).st_ino == os.stat(lib_file).st_ino


def test_more_missing_than_the_tracker_allows_is_not_downloaded(tmp_path, monkeypatch):
    m, qb, lib_file, mine = _alone(tmp_path, monkeypatch, progress=0.905)    # 9.5%: over 90% of 10%
    m.round()
    assert qb.started == [] and os.stat(mine).st_ino == os.stat(lib_file).st_ino


def test_nothing_is_downloaded_while_a_copy_here_is_left_to_try(tmp_path, monkeypatch):
    m, qb, lib_file, mine = _alone(tmp_path, monkeypatch)
    qb.dirs["whole"] = str(tmp_path / "whole")
    qb.prog["whole"], qb.state["whole"] = 1.0, "stalledUP"
    m.round()
    assert qb.started == ["short"] and qb.prog["short"] == 1.0  # finished from the copy, not downloaded
    assert "finished from" in m.state.log[-1][1]


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


def test_a_whole_file_sharing_a_missing_piece_gets_its_own_copy_too(tmp_path, monkeypatch):
    """A piece is written whole: the .nfo sharing the missing piece with the video would be
    written into as well - it gets bytes of its own; the .srt, in pieces already here, not."""
    m, qb, lib_file, mine = _alone(tmp_path, monkeypatch)
    linked = {}
    for name in ("ep.nfo", "ep.srt"):
        lib = tmp_path / "tv" / name
        lib.write_bytes(name.encode() * 10)
        os.link(lib, tmp_path / "short" / "Rel" / name)
        linked[name] = lib
    qb.files = lambda h: [
        {"name": "Rel/ep.mkv", "size": len(GOOD), "progress": qb.prog[h], "priority": 1, "piece_range": [0, 1]},
        {"name": "Rel/ep.nfo", "size": 60, "progress": 1, "priority": 1, "piece_range": [1, 1]},
        {"name": "Rel/ep.srt", "size": 60, "progress": 1, "priority": 1, "piece_range": [2, 2]}]
    qb.piece_states = lambda h: [2, 0, 2]
    m.round()
    assert qb.started == ["short"]
    here = tmp_path / "short" / "Rel"
    assert os.stat(here / "ep.nfo").st_ino != os.stat(linked["ep.nfo"]).st_ino
    assert os.stat(here / "ep.srt").st_ino == os.stat(linked["ep.srt"]).st_ino


def _cross_eyed(m, monkeypatch, jobs=None, dry=False, down=False, verdict="not_shared", asked=None):
    """A fake cross-eyed: its open jobs, and its download-check verdict for every torrent."""
    from nzb2seed import library
    from nzb2seed.clients import ApiError
    asked = asked if asked is not None else []

    class CE:
        def __init__(self, *a):
            pass

        def open_jobs(self):
            if down:
                raise ApiError("cross-eyed cannot be reached")
            return dict(jobs or {}), dry

        def download_check(self, h):
            if down:
                raise ApiError("cross-eyed cannot be reached")
            asked.append(("check", h))
            return {"verdict": verdict, "summary": f"says {verdict}", "job_id": 12}

        def repair(self, h):
            asked.append(("repair", h))
            return {"ok": True, "job_id": 31, "dry_run": dry}
    monkeypatch.setattr(library, "CrossEyed", CE)
    m.app.cfg.library_cross_eyed_url = "http://ce"
    m.app.cfg.library_cross_eyed_prefix = "/host"


def test_a_torrent_cross_eyed_has_a_job_on_is_left_alone_and_said_so_once(tmp_path, monkeypatch):
    m, qb, lib_file, mine = _alone(tmp_path, monkeypatch)
    _cross_eyed(m, monkeypatch, {"short": {"id": 7, "state": "failed", "job_type": "relocate"}})
    m.round()
    m.round()
    assert qb.started == [] and os.stat(mine).st_ino == os.stat(lib_file).st_ino
    said = [t for _, t in m.state.log if "cross-eyed job 7" in t]
    assert len(said) == 1 and "failed" in said[0] and "safety copy" in said[0]


def test_in_dry_run_mend_says_the_job_waits_for_you(tmp_path, monkeypatch):
    m, qb, lib_file, mine = _alone(tmp_path, monkeypatch)
    _cross_eyed(m, monkeypatch, {"short": {"id": 8, "state": "pending", "job_type": "move"}}, dry=True)
    m.round()
    assert qb.started == [] and "dry-run" in m.state.log[-1][1]


def test_cross_eyed_not_answering_means_never_writing_into_a_shared_file(tmp_path, monkeypatch):
    """No answer: no files are swapped in, and a download only goes into files of its own."""
    m, qb, lib_file, mine = _alone(tmp_path, monkeypatch)
    _cross_eyed(m, monkeypatch, down=True)
    m.round()
    assert any("did not answer" in t for _, t in m.state.log)
    assert qb.started == ["short"] and os.stat(mine).st_ino != os.stat(lib_file).st_ino


def test_repairable_is_repaired_by_cross_eyed_not_downloaded(tmp_path, monkeypatch):
    m, qb, lib_file, mine = _alone(tmp_path, monkeypatch, ticked=())      # no tracker needed for it
    asked = []
    _cross_eyed(m, monkeypatch, verdict="repairable", asked=asked, dry=True)
    m.round()
    m.round()
    assert qb.started == [] and asked.count(("repair", "short")) == 1
    said = [t for _, t in m.state.log if "repairing it" in t]
    assert len(said) == 1 and "job 31" in said[0] and "dry-run" in said[0]


def test_a_download_that_would_break_other_torrents_is_not_made(tmp_path, monkeypatch):
    m, qb, lib_file, mine = _alone(tmp_path, monkeypatch)
    _cross_eyed(m, monkeypatch, verdict="shared_breaks_others")
    m.round()
    m.round()
    assert qb.started == [] and os.stat(mine).st_ino == os.stat(lib_file).st_ino
    assert len([t for _, t in m.state.log if "says shared_breaks_others" in t]) == 1


def test_shared_with_torrents_needing_the_same_bytes_is_downloaded_in_place(tmp_path, monkeypatch):
    m, qb, lib_file, mine = _alone(tmp_path, monkeypatch)
    _cross_eyed(m, monkeypatch, verdict="shared_same_version")
    m.round()
    assert qb.started == ["short"] and os.stat(mine).st_ino == os.stat(lib_file).st_ino   # fixes them all
    assert "need the same bytes" in m.state.log[-1][1]


def test_files_a_library_also_holds_get_a_copy_of_their_own_first(tmp_path, monkeypatch):
    m, qb, lib_file, mine = _alone(tmp_path, monkeypatch)
    _cross_eyed(m, monkeypatch, verdict="shared_other_names")
    m.round()
    assert qb.started == ["short"] and os.stat(mine).st_ino != os.stat(lib_file).st_ino


def test_a_job_cross_eyed_reports_on_the_torrent_leaves_it_alone(tmp_path, monkeypatch):
    m, qb, lib_file, mine = _alone(tmp_path, monkeypatch)
    _cross_eyed(m, monkeypatch, verdict="in_job")
    m.round()
    assert qb.started == [] and "left alone - says in_job" in m.state.log[-1][1]


def test_cross_eyeds_answer_is_kept_while_nothing_changes(tmp_path, monkeypatch):
    m, qb, lib_file, mine = _alone(tmp_path, monkeypatch)
    asked = []
    _cross_eyed(m, monkeypatch, verdict="shared_breaks_others", asked=asked)
    m.round()
    m.round()
    assert asked.count(("check", "short")) == 1


def test_a_copy_cross_eyed_is_moving_is_not_used_to_finish_another_and_mend_says_when_it_can(tmp_path, monkeypatch):
    m, qb, lib_file, mine = _setup(tmp_path)
    m.app.cfg.nearly_auto_trackers = []
    jobs = {"whole": {"id": 9, "state": "repointed", "job_type": "move"}}
    _cross_eyed(m, monkeypatch, jobs)
    m.round()
    assert qb.prog["short"] == 0.5 and qb.started == []
    assert "complete copy is not used" in m.state.log[-1][1] and "job 9" in m.state.log[-1][1]
    jobs.clear()                                                    # the job is over
    m.round()
    said = [t for _, t in m.state.log]
    assert any("mend can use its files again" in t for t in said)
    assert qb.prog["short"] == 1.0                                  # and it is used


def test_mend_says_when_a_torrent_it_left_alone_is_picked_up_again_after_a_restart(tmp_path, monkeypatch):
    m, qb, lib_file, mine = _alone(tmp_path, monkeypatch)
    jobs = {"short": {"id": 7, "state": "failed", "job_type": "relocate"}}
    _cross_eyed(m, monkeypatch, jobs)
    m.round()
    m.state = mend.State(str(tmp_path / "mend.json"))               # a restart: read back from mend.json
    jobs.clear()
    m.round()
    said = [t for _, t in m.state.log]
    assert any("cross-eyed's job on it is over - mend looks at it again" in t for t in said)
    assert qb.started == ["short"]
