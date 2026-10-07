"""A .torrent uploaded on the Build tab is paired and built like a search result."""
import base64
import threading

from nzb2seed import gui
from nzb2seed.clients import Release

from helpers import make_torrent
from test_gui_login import call, server  # noqa: F401  (fixture)

AUTH = ("admin", "nzb2seed")


def test_upload_becomes_a_buildable_result(server, monkeypatch):  # noqa: F811
    app, url = server
    data = make_torrent("Show.2016.S01E01.720p.HDTV.x264-GRPA",
                        {"a.mkv": b"x" * 40000, "a.nfo": b"hello\r\n"})
    status, r = call(url + "/api/upload_torrent", *AUTH,
                     body={"torrent_b64": base64.b64encode(data).decode(), "torrent_name": "mine.torrent"})
    assert status == 200
    t = r["torrent"]
    assert t["title"] == "Show.2016.S01E01.720p.HDTV.x264-GRPA" and t["size"] == 40007
    assert t["guid"].startswith("upload:") and t["indexer"] == "mine.torrent" and t["files"] == 2

    seen = {}
    gate = threading.Event()

    def fake_run(cfg, opts, tor_rel, groups, torrent_data=None):
        seen.update(rel=tor_rel, data=torrent_data)
        gate.set()
        return {"result": "ok"}
    monkeypatch.setattr(gui, "execute_run", fake_run)
    status, r = call(url + "/api/build", *AUTH, body={"torrent": t, "nzbs": [], "options": {}})
    assert status == 200 and gate.wait(5)
    assert seen["data"] == data and seen["rel"].title == t["title"]


def test_junk_and_forged_uploads_are_refused(server):  # noqa: F811
    app, url = server
    status, _ = call(url + "/api/upload_torrent", *AUTH,
                     body={"torrent_b64": base64.b64encode(b"not a torrent").decode(), "torrent_name": "x.torrent"})
    assert status == 400
    forged = Release("X", "torrent", "x", 0, 1, "upload:../../etc/passwd", "", "", "", None, None, None)
    try:
        gui.uploaded_data(app.cfg, forged)
        raise AssertionError("accepted a forged path")
    except ValueError:
        pass


def test_a_build_from_another_tracker_does_not_wait_for_a_build_slot(server, monkeypatch):  # noqa: F811
    """Look on other trackers: the Usenet downloads are already made, so building it again
    from another tracker only places files - it is not queued behind builds that download."""
    import dataclasses
    app, url = server
    full = threading.Semaphore(0)                       # every build slot taken
    monkeypatch.setattr(app, "build_slot", lambda: full)
    started = []
    monkeypatch.setattr(gui, "execute_run", lambda cfg, opts, t, g, torrent_data=None:
                        started.append(t.indexer) or {"result": "ok"})
    monkeypatch.setattr(gui, "uploaded_data", lambda cfg, t: None)

    def torrent(tracker, guid):
        return dataclasses.asdict(Release(f"Film.2020.1080p.BluRay-{guid}", "torrent", tracker, 1, 10, guid,
                                          "http://prowlarr.local/1/download", "", "", 0, 3, None))
    call(url + "/api/build", *AUTH, body={"torrent": torrent("TrackerOne", "a"), "nzbs": [], "options": {}})
    status, r = call(url + "/api/build", *AUTH, body={"torrent": torrent("TrackerThree", "b"), "nzbs": [],
                                                       "options": {}, "other_tracker": 3})
    assert status == 200
    deadline = __import__("time").time() + 5
    while "TrackerThree" not in started and __import__("time").time() < deadline:
        __import__("time").sleep(0.05)
    assert started == ["TrackerThree"]                  # the ordinary build still waits its turn
    job = app.jobs[r["id"]]
    assert any("not waiting for a build slot" in l["t"] for l in job.lines)
    for j in app.jobs.values():
        j.cancel.set()                                  # let the waiting one go


def test_trying_again_a_release_that_stopped_nearly_complete_does_not_wait_either(server, monkeypatch):  # noqa: F811
    """However it is started again - Try again, or a request from before the button marked
    it - a release already built nearly complete, downloads kept, does not queue."""
    import dataclasses
    app, url = server
    monkeypatch.setattr(app, "build_slot", lambda: threading.Semaphore(0))
    started = []
    monkeypatch.setattr(gui, "execute_run", lambda cfg, opts, t, g, torrent_data=None:
                        started.append(t.title) or {"result": "ok"})
    monkeypatch.setattr(gui, "uploaded_data", lambda cfg, t: None)
    before = app.start_job("Show Name S01 1080p WEB-DL DDP5 1 H 264-GRPA", "build", lambda cfg: None)
    before.extra = {"have": 0.99999, "tracker": "TrackerOne"}
    gone = app.start_job("Film.2021.1080p.BluRay-GRPA", "build", lambda cfg: None)
    gone.extra = {"have": 0.99999, "abandoned": True}                 # its downloads were deleted
    t = dataclasses.asdict(Release("Show.Name.S01.1080p.WEB-DL.DDP5.1.H.264-GRPA", "torrent", "TrackerTwo", 1, 10,
                                   "g", "http://prowlarr.local/1/download", "", "", 0, 3, None))
    assert call(url + "/api/build", *AUTH, body={"torrent": t, "nzbs": [], "options": {}})[0] == 200
    t2 = dict(t, title="Film.2021.1080p.BluRay-GRPA", guid="g2")
    assert call(url + "/api/build", *AUTH, body={"torrent": t2, "nzbs": [], "options": {}})[0] == 200
    import time
    end = time.time() + 5
    while not started and time.time() < end:
        time.sleep(0.05)
    time.sleep(0.2)
    assert started == ["Show.Name.S01.1080p.WEB-DL.DDP5.1.H.264-GRPA"]   # the abandoned one queues
    for j in app.jobs.values():
        j.cancel.set()


def test_a_build_from_another_tracker_knows_the_first_torrent(server, monkeypatch):  # noqa: F811
    """Its downloads are looked for on disk before anything is searched for."""
    import dataclasses
    app, url = server
    first = app.start_job("Film.2020.1080p.BluRay-GRPA", "auto", lambda cfg: None)
    first.extra = {"have": 0.9999, "infohash": "f" * 40}
    seen = threading.Event()
    got = {}
    monkeypatch.setattr(gui, "execute_run", lambda cfg, opts, t, g, torrent_data=None:
                        got.update(related=opts.related) or seen.set() or {"result": "ok"})
    monkeypatch.setattr(gui, "uploaded_data", lambda cfg, t: None)
    t = dataclasses.asdict(Release("Film.2020.1080p.BluRay-GRPA", "torrent", "TrackerTwo", 1, 10, "g",
                                   "http://prowlarr.local/1/download", "", "", 0, 3, None))
    status, _ = call(url + "/api/build", *AUTH, body={"torrent": t, "nzbs": [], "options": {},
                                                      "other_tracker": first.id})
    assert status == 200 and seen.wait(5) and got["related"] == ("f" * 40,)


def test_a_build_carrying_on_an_automatic_one_stays_automatic(server, monkeypatch):  # noqa: F811
    """Built from another tracker for an automatic job: labelled automatic, never stops to
    ask, and its files count as automatic ones."""
    import dataclasses
    app, url = server
    first = app.start_job("auto: Film.2020.1080p.BluRay-GRPA", "auto", lambda cfg: None)
    first.extra = {"have": 0.9999, "infohash": "f" * 40}
    seen = threading.Event()
    got = {}
    monkeypatch.setattr(gui, "execute_run", lambda cfg, opts, t, g, torrent_data=None:
                        got.update(unattended=opts.unattended) or seen.set() or {"result": "ok"})
    monkeypatch.setattr(gui, "uploaded_data", lambda cfg, t: None)
    t = dataclasses.asdict(Release("Film.2020.1080p.BluRay-GRPA", "torrent", "TrackerTwo", 1, 10, "g",
                                   "http://prowlarr.local/1/download", "", "", 0, 3, None))
    status, r = call(url + "/api/build", *AUTH, body={"torrent": t, "nzbs": [], "options": {},
                                                      "other_tracker": first.id})
    assert status == 200 and seen.wait(5) and got["unattended"] is True
    assert app.jobs[r["id"]].title == "auto: Film.2020.1080p.BluRay-GRPA"
    assert first.status == "cancelled" and first.retried_as == r["id"]       # replaced by it


def test_a_build_over_90_percent_looks_on_the_other_trackers_by_itself(server, monkeypatch):  # noqa: F811
    """Stopped nearly complete where nothing may be downloaded: the release is found on a
    pre-approved, seeded tracker and built from it - without anyone pressing anything."""
    import dataclasses
    from nzb2seed.pipeline import Incomplete
    app, url = server
    app.cfg.nearly_auto_trackers = ["TrackerTwo"]
    other = Release("Film.2020.1080p.BluRay-GRPA", "torrent", "TrackerTwo", 2, 10, "g2",
                    "http://prowlarr.local/2/download", "", "", 0, 4, None)
    monkeypatch.setattr(gui, "search", lambda cfg, name: ([other], []))
    started = threading.Event()
    got = {}
    monkeypatch.setattr(gui, "execute_run", lambda cfg, opts, t, g, torrent_data=None:
                        got.update(t=t, related=opts.related) or started.set() or {"result": "ok"})
    monkeypatch.setattr(gui, "uploaded_data", lambda cfg, t: None)

    def stops(cfg):
        raise Incomplete("not every file", 0.999999, "f" * 40, "", "/x", False, tracker="TrackerOne", short=1200)
    first = app.start_job("auto: Film.2020.1080p.BluRay-GRPA", "auto", stops)
    assert started.wait(5)
    assert got["t"].indexer == "TrackerTwo" and got["related"] == ("f" * 40,)
    assert first.extra["built_from_other"] == "TrackerTwo"
    new = max(app.jobs.values(), key=lambda j: j.id)
    assert new.title.startswith("auto: ") and new.repeat["body"]["other_tracker"] == first.id
    import time
    end = time.time() + 5
    while time.time() < end and first.status != "cancelled":
        time.sleep(0.05)
    # the old job is done with: cancelled, pointing at the build that replaced it
    assert first.status == "cancelled" and first.retried_as == new.id
    assert first.result == f"replaced by job {new.id}: built from TrackerTwo"
    for j in app.jobs.values():
        j.cancel.set()
