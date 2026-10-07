"""Running a job again from the jobs list.

A job keeps the request that started it, so "try again" is that same call made again -
no guessing at what a build was for, and nothing to re-enter.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from test_gui_login import call, server  # noqa: E402, F401  (fixture)

AUTH = ("admin", "nzb2seed")


def test_the_request_that_made_a_job_is_replayed(server):  # noqa: F811
    """The retry makes the same call again - here a settings save, so it is easy to see."""
    app, url = server
    job = app.start_job("something that failed", "build", lambda cfg: {"result": "done"})
    job.status = "failed"
    settings = call(url + "/api/settings", *AUTH)[1]["settings"]
    settings["behaviour"]["cleanup"] = False
    job.repeat = {"path": "/api/settings", "body": {"settings": settings}}

    status, _ = call(url + "/api/jobs/retry", *AUTH, body={"id": job.id})
    assert status == 200
    assert call(url + "/api/settings", *AUTH)[1]["settings"]["behaviour"]["cleanup"] is False


def test_a_job_says_whether_it_can_be_run_again(server):  # noqa: F811
    app, url = server
    job = app.start_job("made by hand", "build", lambda cfg: {"result": "done"})
    job.repeat = None
    rows = call(url + "/api/jobs", *AUTH)[1]
    assert [r["can_retry"] for r in rows if r["id"] == job.id] == [False]

    job.repeat = {"path": "/api/settings", "body": {}}
    rows = call(url + "/api/jobs", *AUTH)[1]
    assert [r["can_retry"] for r in rows if r["id"] == job.id] == [True]


def test_a_job_with_nothing_to_replay_says_so(server):  # noqa: F811
    app, url = server
    job = app.start_job("made by hand", "build", lambda cfg: {"result": "done"})
    job.repeat = None
    # (the test helper drops the body of an error answer, so only the code is checked here;
    #  the message itself is what the page shows in a toast)
    assert call(url + "/api/jobs/retry", *AUTH, body={"id": job.id})[0] == 400


def test_retrying_something_that_is_not_a_job(server):  # noqa: F811
    _, url = server
    assert call(url + "/api/jobs/retry", *AUTH, body={"id": 9999})[0] == 404
    assert call(url + "/api/jobs/retry", *AUTH, body={"id": "nonsense"})[0] == 404


def test_the_replayed_request_is_remembered_again(server):  # noqa: F811
    """So a retry of a retry still works."""
    app, url = server
    job = app.start_job("first go", "build", lambda cfg: {"result": "done"})
    settings = call(url + "/api/settings", *AUTH)[1]["settings"]
    job.repeat = {"path": "/api/settings", "body": {"settings": settings}}
    call(url + "/api/jobs/retry", *AUTH, body={"id": job.id})
    assert job.repeat["path"] == "/api/settings"        # the original is left as it was


BUILD = {"torrent": {"title": "Some.Release.2019.1080p-GRP", "protocol": "torrent",
                     "indexer": "ATracker", "indexer_id": 1, "size": 1, "guid": "g-1",
                     "download_url": "", "info_url": "", "publish_date": "", "grabs": 0,
                     "seeders": 0, "files": 1}, "nzbs": [], "options": {}}


def a_failed_build(app, monkeypatch):
    """A build that failed, whose request replays into a build that holds until told."""
    import threading
    from nzb2seed import gui
    gate = threading.Event()
    monkeypatch.setattr(gui, "execute_run", lambda *a, **k: gate.wait(5) and {"result": "done"})
    job = app.start_job("Some.Release.2019.1080p-GRP", "build", lambda cfg: {"result": "done"})
    job.status = "failed"
    job.repeat = {"path": "/api/build", "body": BUILD}
    return job, gate


def test_a_job_is_tried_again_only_once(server, monkeypatch):  # noqa: F811
    """Its Try again goes once pressed, and a second press - or a double tap that lands
    before the list catches up - is refused rather than starting it twice."""
    app, url = server
    job, gate = a_failed_build(app, monkeypatch)
    try:
        status, r = call(url + "/api/jobs/retry", *AUTH, body={"id": job.id})
        assert status == 200 and job.retried_as == r["id"]           # linked to the new job
        assert call(url + "/api/jobs/retry", *AUTH, body={"id": job.id})[0] == 409
        row = [x for x in call(url + "/api/jobs", *AUTH)[1] if x["id"] == job.id][0]
        assert row["can_retry"] is False and row["retried_as"] == r["id"]
    finally:
        gate.set()


def test_a_retry_that_starts_nothing_leaves_the_job_free(server):  # noqa: F811
    """If the replay is refused, the job has not been tried again - it keeps its button."""
    app, url = server
    job = app.start_job("x", "build", lambda cfg: {"result": "done"})
    job.status = "failed"
    job.repeat = {"path": "/api/build", "body": {"torrent": {"title": ""}}}    # refused
    call(url + "/api/jobs/retry", *AUTH, body={"id": job.id})
    assert job.retried_as is None


def test_the_link_survives_a_restart(tmp_path):
    from nzb2seed import gui
    cfgfile = tmp_path / "nzb2seed.toml"
    cfgfile.write_text("")
    app = gui.App(str(cfgfile))
    job = app.start_job("x", "build", lambda cfg: {"result": "done"})
    job.retried_as = 42
    app.store.flush()
    assert gui.App(str(cfgfile)).jobs[job.id].retried_as == 42


def test_an_ordinary_job_does_not_inherit_a_retry(server):  # noqa: F811
    """The link is only ever made by the retry request itself."""
    app, url = server
    earlier = app.start_job("earlier", "build", lambda cfg: {"result": "done"})
    app.request.retrying = None
    app.start_job("unrelated", "build", lambda cfg: {"result": "done"})
    assert earlier.retried_as is None


def test_finished_jobs_can_be_removed_from_the_list(server):  # noqa: F811
    """Remove from list takes finished jobs off; one still running is never removed."""
    import threading
    app, url = server
    hold = threading.Event()
    done = app.start_job("a", "build", lambda cfg: {"result": "done"})
    failed = app.start_job("b", "build", lambda cfg: (_ for _ in ()).throw(ValueError("no")))
    running = app.start_job("c", "build", lambda cfg: hold.wait(5) and {"result": "done"})
    try:
        import time
        end = time.time() + 5
        while time.time() < end and (done.status == "running" or failed.status == "running"):
            time.sleep(0.05)
        status, r = call(url + "/api/jobs/remove", *AUTH, body={"ids": [done.id, failed.id, running.id]})
        assert status == 200 and r == {"removed": 2, "kept": 1}
        ids = [j["id"] for j in call(url + "/api/jobs", *AUTH)[1]]
        assert done.id not in ids and failed.id not in ids and running.id in ids
    finally:
        hold.set()


def test_removing_with_downloads_needs_qbittorrent_to_say_what_is_in_use(server):  # noqa: F811
    """Deleting downloads is refused - and nothing is removed - when qBittorrent cannot be
    asked which of them a torrent depends on."""
    import time
    app, url = server
    job = app.start_job("Film.2020.1080p-GRP", "build", lambda cfg: {"result": "done"})
    end = time.time() + 5
    while time.time() < end and job.status == "running":
        time.sleep(0.05)
    job.infohash = "a" * 40
    app._qbit = lambda: None
    status, _ = call(url + "/api/jobs/remove", *AUTH, body={"ids": [job.id], "delete_downloads": True})
    assert status == 400 and job.id in app.jobs


def test_what_the_jobs_still_need(server):  # noqa: F811
    from nzb2seed.pipeline import release_key as pipeline_release_key
    """A failed job may be tried again, so its downloads are kept; a running one is using
    them; an abandoned one needs nothing."""
    import threading
    import time
    app, url = server
    hold = threading.Event()
    running = app.start_job("a", "build", lambda cfg: hold.wait(5) and {"result": "done"})
    failed = app.start_job("b", "build", lambda cfg: (_ for _ in ()).throw(ValueError("no")))
    gone = app.start_job("c", "build", lambda cfg: (_ for _ in ()).throw(ValueError("no")))
    end = time.time() + 5
    while time.time() < end and "running" in (failed.status, gone.status):
        time.sleep(0.05)
    running.infohash, failed.infohash, gone.infohash = "1" * 40, "2" * 40, "3" * 40
    gone.extra = {"abandoned": True}
    try:
        failed.retried_as = None
        keep, live, names = app.downloads_in_use()
        assert keep == {"2" * 40} and "1" * 40 in live and "3" * 40 not in keep | live
        assert names == {pipeline_release_key("a"), pipeline_release_key("b")}
        failed.retried_as = 999                            # tried again: it needs nothing now
        assert "2" * 40 not in app.downloads_in_use()[0]
    finally:
        hold.set()


def test_cleared_with_keep_files_its_downloads_are_never_cleared(server):  # noqa: F811
    """Clear from list, keep files: its Usenet downloads stay for good - the hourly clean-up
    leaves them alone, after a restart too. Clear from list, delete files lets them go."""
    import time
    from nzb2seed import gui
    from nzb2seed.pipeline import release_key
    app, url = server
    job = app.start_job("Film.2020.1080p-GRP", "build", lambda cfg: (_ for _ in ()).throw(ValueError("no")))
    end = time.time() + 5
    while time.time() < end and job.status == "running":
        time.sleep(0.05)
    job.infohash = "4" * 40
    status, _ = call(url + "/api/jobs/remove", *AUTH, body={"ids": [job.id]})
    assert status == 200 and job.id not in app.jobs
    keep, _, names = app.downloads_in_use()
    assert "4" * 40 in keep and release_key("Film.2020.1080p-GRP") in names
    again = gui.App(app.cfg.path)                          # remembered across a restart
    assert "4" * 40 in again.downloads_in_use()[0]
    app.forget_kept({"4" * 40}, {release_key("Film.2020.1080p-GRP")})
    assert "4" * 40 not in app.downloads_in_use()[0]
