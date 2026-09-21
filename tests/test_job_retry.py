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
