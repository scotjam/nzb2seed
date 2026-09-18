"""The GUI's job list survives a restart."""
import json
import threading
import time

from nzb2seed import gui, report


def wait(pred, timeout=5):
    end = time.time() + timeout
    while time.time() < end and not pred():
        time.sleep(0.05)
    assert pred()


def test_jobs_survive_restart(tmp_path):
    cfgfile = tmp_path / "nzb2seed.toml"
    cfgfile.write_text("[sabnzbd]\ncategory = \"x\"\n")
    app = gui.App(str(cfgfile))

    def work(cfg):
        report.step("Doing a thing")
        report.info("detail")
        report.pieces("##x.")
        return {"result": "100.0% (stopped)"}
    done = app.start_job("Release.One", "build", work)
    wait(lambda: done.status == "done")

    gate = threading.Event()

    def hang(cfg):
        report.step("Waiting for SABnzbd")
        gate.wait(10)
    running = app.start_job("Release.Two", "build", hang)
    wait(lambda: any(l["t"] == "Waiting for SABnzbd" for l in running.lines))
    wait(lambda: (tmp_path / "jobs.json").exists()
         and "Waiting for SABnzbd" in (tmp_path / "jobs.json").read_text())

    # "restart": a new App reading the same folder while job 2 was still running
    app2 = gui.App(str(cfgfile))
    j1, j2 = app2.jobs[done.id], app2.jobs[running.id]
    assert j1.status == "done" and j1.result == "100.0% (stopped)"
    assert [l["t"] for l in j1.lines] == ["Doing a thing", "detail"] and j1.pieces == "##x."
    assert j2.status == "interrupted" and "build it again" in j2.result
    saved = json.loads((tmp_path / "jobs.json").read_text())["jobs"]
    assert {j["id"]: j["status"] for j in saved}[running.id] == "interrupted"
    assert app2.start_job("Release.Three", "build", lambda cfg: None).id == running.id + 1
    gate.set()


def test_a_build_can_wait_for_the_person(tmp_path):
    cfgfile = tmp_path / "nzb2seed.toml"
    cfgfile.write_text("")
    app = gui.App(str(cfgfile))
    got = []

    def work(cfg):
        got.append(report.ask("pick one", [{"title": "A"}, {"title": "B"}]))
        return {"result": "done"}
    job = app.start_job("Pack", "build", work)
    wait(lambda: job.status == "waiting" and job.question)
    assert job.summary()["question"]["prompt"] == "pick one"
    job.answer(1)
    wait(lambda: job.status == "done")
    assert got == [1] and job.question is None
    # a GUI restart while waiting turns the question into "interrupted"
    job2 = app.start_job("Pack 2", "build", lambda cfg: report.ask("again?", [{"title": "A"}]))
    wait(lambda: job2.status == "waiting")
    app.store.flush()
    assert gui.App(str(cfgfile)).jobs[job2.id].status == "interrupted"
    job2.answer(None)
