"""A build that stops a few percent short: keep it, and let the person decide.

Many trackers let you download a few percent of a torrent without it counting towards a
hit-and-run, so a 97% build is worth finishing in the torrent client rather than throwing
away. Nothing of it is deleted until the person says so.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest  # noqa: E402

from nzb2seed import gui  # noqa: E402
from nzb2seed.pipeline import Incomplete  # noqa: E402
from test_gui_login import call, server  # noqa: E402, F401  (fixture)

AUTH = ("admin", "nzb2seed")
H = "a" * 40


def wait(pred, timeout=5):
    import time
    end = time.time() + timeout
    while time.time() < end and not pred():
        time.sleep(0.05)
    assert pred()


def failed_part_way(app, have=0.97, in_client=False, tracker="Some Tracker (API)"):
    def run(cfg):
        raise Incomplete(f"stopped at {have * 100:.1f}%", have, H, "/t/x.torrent", "/data",
                         in_client=in_client, tracker=tracker)
    job = app.start_job("Show.S01.1080p-GRP", "build", run)
    wait(lambda: job.status == "failed")
    return job


# ---------------------------------------------------------------- how far it got

def test_a_build_that_stops_short_says_how_far_it_got(server):  # noqa: F811
    app, url = server
    job = failed_part_way(app, have=0.973)
    assert job.extra["have"] == 0.973 and job.extra["infohash"] == H
    assert job.extra["tracker"] == "Some Tracker (API)"
    row = [r for r in call(url + "/api/jobs", *AUTH)[1] if r["id"] == job.id][0]
    assert row["extra"]["have"] == 0.973                    # the page can offer to finish it


def test_what_it_got_survives_a_restart(tmp_path):
    cfgfile = tmp_path / "nzb2seed.toml"
    cfgfile.write_text("")
    app = gui.App(str(cfgfile))
    job = failed_part_way(app)
    app.store.flush()
    again = gui.App(str(cfgfile))
    assert again.jobs[job.id].extra["have"] == 0.97


def test_an_ordinary_failure_has_nothing_to_finish(server):  # noqa: F811
    from nzb2seed.pipeline import Abort
    app, url = server

    def run(cfg):
        raise Abort("no Usenet post could supply it")
    job = app.start_job("X", "build", run)
    wait(lambda: job.status == "failed")
    assert job.extra is None
    assert call(url + "/api/jobs/add_to_client", *AUTH, body={"id": job.id})[0] == 404
    assert call(url + "/api/jobs/abandon", *AUTH, body={"id": job.id})[0] == 404


# ---------------------------------------------------------------- abandoning it

class FakeQbit:
    def __init__(self, state=None):
        self.state, self.removed = state, []

    def info(self, h):
        return {"state": self.state, "progress": 0.97} if self.state else None

    def remove(self, h, delete_files=False):
        assert delete_files is False                          # its files go from the record
        self.removed.append(h)


def placed(app, tmp_path):
    """Files the build placed, recorded as nzb2seed's own."""
    from nzb2seed import pipeline
    d = tmp_path / "out" / "Show.S01"
    d.mkdir(parents=True)
    f = d / "e01.mkv"
    f.write_bytes(b"x" * 10)
    rec = pipeline.owned_record(app.cfg, type("T", (), {"infohash": H})())
    rec.files.add(str(f)); rec.dirs.add(str(d)); rec.save()
    return f, d


def test_abandoning_deletes_what_the_build_placed(server, tmp_path, monkeypatch):  # noqa: F811
    app, url = server
    f, d = placed(app, tmp_path)
    monkeypatch.setattr(app, "_qbit", lambda: FakeQbit(state=None))
    monkeypatch.setattr(app, "_sab", lambda: None)
    job = failed_part_way(app)
    status, r = call(url + "/api/jobs/abandon", *AUTH, body={"id": job.id})
    assert status == 200 and not f.exists() and not d.exists()
    assert job.extra["abandoned"] is True and "abandoned" in job.result


def test_abandoning_removes_a_stopped_torrent_nzb2seed_added(server, tmp_path, monkeypatch):  # noqa: F811
    app, url = server
    placed(app, tmp_path)
    qb = FakeQbit(state="stoppedDL")
    monkeypatch.setattr(app, "_qbit", lambda: qb)
    monkeypatch.setattr(app, "_sab", lambda: None)
    job = failed_part_way(app, in_client=True)
    assert call(url + "/api/jobs/abandon", *AUTH, body={"id": job.id})[0] == 200
    assert qb.removed == [H]


def test_a_torrent_that_is_running_is_never_abandoned(server, tmp_path, monkeypatch):  # noqa: F811
    """If it is downloading the rest, abandoning would throw that download away."""
    app, url = server
    f, _ = placed(app, tmp_path)
    qb = FakeQbit(state="downloading")
    monkeypatch.setattr(app, "_qbit", lambda: qb)
    job = failed_part_way(app, in_client=True)
    assert call(url + "/api/jobs/abandon", *AUTH, body={"id": job.id})[0] == 400
    assert f.exists() and qb.removed == []                   # nothing touched


def test_nothing_is_deleted_until_the_person_abandons_it(server, tmp_path):  # noqa: F811
    """A build that stops a few percent short keeps everything on its own."""
    app, _ = server
    f, _ = placed(app, tmp_path)
    failed_part_way(app, have=0.99)
    assert f.exists()


def test_its_usenet_downloads_are_kept_while_other_builds_run(server, tmp_path, monkeypatch):  # noqa: F811
    """Another build may be reusing one of them."""
    import threading
    app, url = server
    placed(app, tmp_path)
    monkeypatch.setattr(app, "_qbit", lambda: FakeQbit(state=None))
    gate = threading.Event()
    other = app.start_job("Another.Build", "build", lambda cfg: gate.wait(5) and {"result": "done"})
    job = failed_part_way(app)
    try:
        status, r = call(url + "/api/jobs/abandon", *AUTH, body={"id": job.id})
        assert status == 200 and "other builds are running" in job.result
    finally:
        gate.set()
        wait(lambda: other.status == "done")


# ---------------------------------------------------------------- always, for a tracker

def test_a_tracker_you_chose_is_added_without_asking(server, monkeypatch):  # noqa: F811
    import dataclasses
    app, url = server
    app.cfg = dataclasses.replace(app.cfg, nearly_auto_trackers=["sometracker"])
    started = []
    monkeypatch.setattr(app, "finish_in_client", lambda job, why="", **kw: started.append((job.id, why)))
    job = failed_part_way(app, have=0.97, tracker="sometracker (API)")
    assert [j for j, _ in started] == [job.id]               # "(API)" is the same tracker
    assert job.extra["auto"] is True and "sometracker" in started[0][1]


def test_other_trackers_still_ask(server, monkeypatch):  # noqa: F811
    import dataclasses
    app, _ = server
    app.cfg = dataclasses.replace(app.cfg, nearly_auto_trackers=["sometracker"])
    started = []
    monkeypatch.setattr(app, "finish_in_client", lambda job, why="", **kw: started.append(job.id))
    failed_part_way(app, have=0.97, tracker="Another Tracker")
    assert started == []


def test_too_far_short_is_never_added_even_for_a_chosen_tracker(server, monkeypatch):  # noqa: F811
    """"Always" is only ever about builds this close: 80% is not a few percent."""
    import dataclasses
    app, _ = server
    app.cfg = dataclasses.replace(app.cfg, nearly_auto_trackers=["sometracker"])
    started = []
    monkeypatch.setattr(app, "finish_in_client", lambda job, why="", **kw: started.append(job.id))
    failed_part_way(app, have=0.80, tracker="sometracker (API)")
    assert started == []


def test_always_remembers_the_tracker(server, monkeypatch, tmp_path):  # noqa: F811
    app, url = server
    monkeypatch.setattr(app, "finish_in_client", lambda job, why="", **kw: type("J", (), {"id": 0})())
    job = failed_part_way(app, have=0.97, tracker="sometracker (API)")
    assert call(url + "/api/jobs/add_to_client", *AUTH, body={"id": job.id, "always": True})[0] == 200
    assert app.always_adds("Sometracker")                          # the same tracker, however written
    assert "sometracker (API)" in app.cfg.nearly_auto_trackers


def test_once_added_it_is_not_added_twice(server, monkeypatch):  # noqa: F811
    app, url = server
    monkeypatch.setattr(app, "finish_in_client", lambda job, why="", **kw: type("J", (), {"id": 0})())
    job = failed_part_way(app)
    job.extra = {**job.extra, "added": True}
    assert call(url + "/api/jobs/add_to_client", *AUTH, body={"id": job.id})[0] == 400


# ---------------------------------------------------------------- never round a gap away

def test_a_torrent_short_by_a_few_bytes_is_not_shown_as_complete():
    """30 bytes short of 25 GB is 99.9999999% - rounding that up would hide the gap."""
    from nzb2seed.pipeline import exact_pct
    total = 25 * 1024 ** 3
    assert exact_pct(1 - 30 / total) == "99.9999%"
    assert exact_pct(1.0) == "100.0000%"                 # only a whole torrent says 100
    assert exact_pct(0.973) == "97.3000%"


def test_a_small_shortfall_is_never_shown_as_nothing():
    from nzb2seed.pipeline import exact_gb
    assert exact_gb(30 * 1024) == "0.0293 MB"            # 30 KB
    assert exact_gb(30) == "0.0001 MB"                   # 30 bytes still shows as something
    assert exact_gb(0) == "0.0000 MB"
    assert exact_gb(3 * 1024 ** 3) == "3.0000 GB"


def test_a_few_kb_short_is_not_saved_as_complete():
    """Two tiny extras missing from a 20 GB season must not be stored - or shown - as 100%."""
    from nzb2seed.pipeline import Incomplete, exact_pct
    have = 1 - 40_000 / (20 * 1024 ** 3)
    d = Incomplete("x", have, "h", "t", "s", in_client=False).details()
    assert d["have"] < 1 and exact_pct(d["have"]) == "99.9998%"


# ---------------------------------------------------------------- the recheck decides

class RecheckQbit:
    """qBittorrent whose recheck finds ``found`` of the torrent."""
    def __init__(self, found):
        self.found, self.added, self.started = found, False, []

    def info(self, h):
        return {"state": "stoppedDL", "progress": self.found} if self.added else None

    def add_stopped(self, *a):
        self.added = True

    def wait_idle(self, h, min_wait=0):
        pass

    def recheck(self, h):
        pass

    def start(self, h):
        self.started.append(h)


def finish(app, monkeypatch, tmp_path, found):
    t = tmp_path / "x.torrent"
    t.write_bytes(b"d4:infod4:name1:xee")
    qb = RecheckQbit(found)
    monkeypatch.setattr(app, "_qbit", lambda: qb)
    job = failed_part_way(app, have=0.99)
    job.extra["torrent_path"] = str(t)
    done = app.finish_in_client(job)
    wait(lambda: done.status in ("done", "failed"))
    return job, done, qb


def test_it_starts_when_the_recheck_agrees(server, monkeypatch, tmp_path):  # noqa: F811
    app, _ = server
    job, done, qb = finish(app, monkeypatch, tmp_path, found=0.99)
    assert done.status == "done" and qb.started == [H] and job.extra["added"]


def test_it_stays_stopped_when_the_recheck_finds_too_little(server, monkeypatch, tmp_path):  # noqa: F811
    """The build said 99%, but only 90% checks out: 10% over BitTorrent would count
    towards a hit-and-run, so it is never started - and the job now says 90%."""
    app, _ = server
    job, done, qb = finish(app, monkeypatch, tmp_path, found=0.90)
    assert done.status == "failed" and qb.started == []
    assert "90.0000%" in done.result and "stays stopped" in done.result
    assert job.extra["have"] == 0.90 and job.extra["in_client"] and not job.extra.get("added")
    assert not app.nearly_enough(job.extra)                      # its Add button goes


def test_always_also_adds_the_ones_already_waiting(server, monkeypatch, tmp_path):  # noqa: F811
    """Choosing "always" for a tracker hands over its builds that are already waiting too -
    but not another tracker's, not one too far short, and not one already added."""
    app, url = server
    started = []
    monkeypatch.setattr(app, "finish_in_client",
                        lambda job, why="", **kw: started.append(job.id) or type("J", (), {"id": 0})())
    job = failed_part_way(app, have=0.999, tracker="SomeTracker")
    waiting = failed_part_way(app, have=0.998, tracker="SomeTracker (API)")
    other = failed_part_way(app, have=0.998, tracker="ElseTracker")
    far = failed_part_way(app, have=0.80, tracker="SomeTracker")
    done = failed_part_way(app, have=0.998, tracker="SomeTracker")
    done.extra = {**done.extra, "added": True}
    status, r = call(url + "/api/jobs/add_to_client", *AUTH, body={"id": job.id, "always": True})
    assert status == 200 and started == [job.id, waiting.id] and len(r["also"]) == 1
    assert other.id not in started and far.id not in started and done.id not in started


def test_without_always_only_that_one_is_added(server, monkeypatch):  # noqa: F811
    app, url = server
    started = []
    monkeypatch.setattr(app, "finish_in_client",
                        lambda job, why="", **kw: started.append(job.id) or type("J", (), {"id": 0})())
    job = failed_part_way(app, have=0.999, tracker="SomeTracker")
    failed_part_way(app, have=0.998, tracker="SomeTracker")
    assert call(url + "/api/jobs/add_to_client", *AUTH, body={"id": job.id})[0] == 200
    assert started == [job.id]


# ---------------------------------------------------------------- nobody to finish it

def test_with_no_seeders_it_is_never_offered_to_the_torrent_client(server, monkeypatch):  # noqa: F811
    """Prowlarr said nobody seeds it: the missing part could never come over BitTorrent,
    so neither the button, nor "always add" for its tracker, nor the endpoint hands it on."""
    app, url = server
    started = []
    monkeypatch.setattr(app, "finish_in_client", lambda job, why="", **kw: started.append(job.id))

    def run(cfg):
        raise Incomplete("short", 0.999, H, "/t/x.torrent", "/data", in_client=False,
                         tracker="SomeTracker", seeders=0)
    app.cfg = __import__("dataclasses").replace(app.cfg, nearly_auto_trackers=["SomeTracker"])
    job = app.start_job("Film.2022.1080p-GRP", "build", run)
    wait(lambda: job.status == "failed")
    assert job.extra["seeders"] == 0 and not app.nearly_enough(job.extra)
    assert started == []                                            # no automatic add either
    assert call(url + "/api/jobs/add_to_client", *AUTH, body={"id": job.id})[0] == 400


def test_unknown_seeders_do_not_hold_it_back(server):  # noqa: F811
    app, _ = server
    job = failed_part_way(app, have=0.999)
    assert job.extra.get("seeders") is None and app.nearly_enough(job.extra)


# ---------------------------------------------------------------- other trackers

def test_other_trackers_with_the_same_release_are_offered(server, monkeypatch, tmp_path):  # noqa: F811
    """The same release, on trackers other than the build's own - pre-approved ones first,
    and whether each is the same size as the torrent that was built."""
    import dataclasses
    from nzb2seed.clients import Release
    from helpers import make_torrent
    app, url = server
    data = make_torrent("Show.S01.1080p-GRP", {"a.mkv": b"x" * 5000})
    path = tmp_path / "t.torrent"
    path.write_bytes(data)
    from nzb2seed.torrent import parse
    size = parse(data).total_size
    rel = lambda title, idx, sz, seeds: Release(title, "torrent", idx, 1, sz, title + idx, "", "", "", 0, seeds, 1)
    monkeypatch.setattr(gui, "search", lambda cfg, q: ([
        rel("Show.S01.1080p-GRP", "Some Tracker (API)", size, 9),      # its own tracker: left out
        rel("Show S01 1080p-GRP", "Other", size + 1, 50),
        rel("Show.S01.1080p-GRP", "Approved One", size, 2),
        rel("Show.S01.720p-GRP", "Other", size, 5),                      # another release: left out
    ], []))
    app.cfg = dataclasses.replace(app.cfg, nearly_auto_trackers=["approved one"])
    job = failed_part_way(app, have=0.999, tracker="Some Tracker (API)")
    job.extra["torrent_path"] = str(path)
    status, r = call(url + "/api/jobs/other_trackers", *AUTH, body={"id": job.id})
    assert status == 200
    assert [(x["indexer"], x["approved"], x["same_size"]) for x in r["releases"]] == [
        ("Approved One", True, True), ("Other", False, False)]


# ---------------------------------------------------------------- 5% or 200 MB

def test_nearly_complete_is_under_both_the_percentage_and_the_megabytes(server):  # noqa: F811
    """5% of a 50 GB torrent is 2.5 GB - far too much to download on most trackers' grace,
    so the default is 5% or 200 MB, whichever is less."""
    app, _ = server
    mb = 1024 ** 2
    assert app.nearly_enough({"have": 0.99, "short": 50 * mb})            # 1% of 5 GB: 50 MB
    assert not app.nearly_enough({"have": 0.99, "short": 500 * mb})       # 1% of 50 GB: 500 MB
    assert not app.nearly_enough({"have": 0.94, "short": 10 * mb})        # 6%: over the percentage
    assert app.nearly_enough({"have": 0.99})                              # size not known: the percentage
    assert app.nearly_limit_text() == "5% or 200 MB"


def test_a_tracker_set_to_always_add_is_not_sent_a_build_over_the_megabytes(server, monkeypatch):  # noqa: F811
    import dataclasses
    app, _ = server
    started = []
    monkeypatch.setattr(app, "finish_in_client", lambda job, why="", **kw: started.append(job.id))
    app.cfg = dataclasses.replace(app.cfg, nearly_auto_trackers=["SomeTracker"])

    def run(cfg):
        raise Incomplete("short", 0.99, H, "/t/x.torrent", "/data", in_client=False,
                         tracker="SomeTracker", short=500 * 1024 ** 2)
    job = app.start_job("Film.2020.2160p-GRP", "build", run)
    wait(lambda: job.status == "failed")
    assert started == []


def test_outside_the_limit_only_an_override_adds_it(server, monkeypatch):  # noqa: F811
    app, url = server
    started = []
    monkeypatch.setattr(app, "finish_in_client", lambda job, why="", **kw: started.append(kw.get("override"))
                        or type("J", (), {"id": 0})())
    job = failed_part_way(app, have=0.80)                                  # 20% short: over the limit
    assert call(url + "/api/jobs/add_to_client", *AUTH, body={"id": job.id})[0] == 400
    assert call(url + "/api/jobs/add_to_client", *AUTH, body={"id": job.id, "override": True, "always": True})[0] == 400
    assert started == []
    assert call(url + "/api/jobs/add_to_client", *AUTH, body={"id": job.id, "override": True})[0] == 200
    assert started == [True]


def test_an_override_is_started_whatever_the_recheck_finds(server, monkeypatch, tmp_path):  # noqa: F811
    app, _ = server
    t = tmp_path / "x.torrent"
    t.write_bytes(b"d4:infod4:name1:xee")
    qb = RecheckQbit(0.80)
    monkeypatch.setattr(app, "_qbit", lambda: qb)
    job = failed_part_way(app, have=0.80)
    job.extra["torrent_path"] = str(t)
    done = app.finish_in_client(job, override=True)
    wait(lambda: done.status in ("done", "failed"))
    assert done.status == "done" and qb.started == [H]
