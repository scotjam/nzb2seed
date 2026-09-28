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


def test_only_a_running_build_blocks_building_again(tmp_path):
    cfgfile = tmp_path / "nzb2seed.toml"
    cfgfile.write_text("[sabnzbd]\ncategory = \"x\"\n")
    app = gui.App(str(cfgfile))
    gate = threading.Event()

    def hang(cfg):
        gate.wait(10)
        raise gui.Abort("no post held the files")
    job = app.start_job("Release.Three", "build", hang)
    assert app.active_build("Release.Three") is job
    assert app.active_build("Release.Other") is None
    gate.set()
    wait(lambda: job.status == "failed")
    assert app.active_build("Release.Three") is None     # failed: it may be built again


def test_only_so_many_builds_run_at_once(tmp_path, monkeypatch):
    """Ticking twenty seasons must not start twenty builds: each would sit on a finished
    download while it waited, filling the staging disk with work nothing can finish."""
    import dataclasses
    cfgfile = tmp_path / "nzb2seed.toml"
    cfgfile.write_text("")
    app = gui.App(str(cfgfile))
    app.cfg = dataclasses.replace(app.cfg, build_parallel=2)
    slots = app.build_slot()
    taken = [slots.acquire(blocking=False) for _ in range(4)]
    assert taken == [True, True, False, False]        # two at a time
    slots.release(); slots.release()


def test_the_number_of_slots_follows_the_setting(tmp_path):
    import dataclasses
    cfgfile = tmp_path / "nzb2seed.toml"
    cfgfile.write_text("")
    app = gui.App(str(cfgfile))
    app.cfg = dataclasses.replace(app.cfg, build_parallel=3)
    slots = app.build_slot()
    assert [slots.acquire(blocking=False) for _ in range(4)] == [True, True, True, False]
    for _ in range(3):
        slots.release()
    app.cfg = dataclasses.replace(app.cfg, build_parallel=1)
    slots = app.build_slot()
    assert [slots.acquire(blocking=False) for _ in range(2)] == [True, False]
    slots.release()


def test_a_build_waiting_for_the_person_gives_up_its_slot(tmp_path):
    """Waiting on a pick is not building: another build takes the slot meanwhile, and the
    answered one queues for a slot again like any build that has not started."""
    cfgfile = tmp_path / "nzb2seed.toml"
    cfgfile.write_text("")
    app = gui.App(str(cfgfile))
    slots = threading.Semaphore(1)                 # one build at a time
    gate, order = threading.Event(), []

    def asking(cfg):
        gui.take_slot(slots)
        try:
            report.ask("pick one", [{"title": "A"}])
            order.append("asker carries on")
        finally:
            gui.give_slot()

    def other(cfg):
        gui.take_slot(slots)
        try:
            order.append("other builds")
            gate.wait(10)
        finally:
            gui.give_slot()
    one = app.start_job("One", "build", asking)
    wait(lambda: one.status == "waiting")
    two = app.start_job("Two", "build", other)
    wait(lambda: order == ["other builds"])        # the slot one was not using
    one.answer(0)
    time.sleep(0.3)
    assert order == ["other builds"] and one.status == "running"   # queued behind two
    assert any("Waiting for a build slot" in l["t"] for l in one.lines)
    gate.set()
    wait(lambda: one.status == "done" and two.status == "done")
    assert order == ["other builds", "asker carries on"]
    assert slots.acquire(blocking=False) and not slots.acquire(blocking=False)   # none leaked


def test_a_build_stopped_while_it_waits_for_the_person_frees_nothing_twice(tmp_path):
    cfgfile = tmp_path / "nzb2seed.toml"
    cfgfile.write_text("")
    app = gui.App(str(cfgfile))
    slots = threading.Semaphore(1)

    def asking(cfg):
        gui.take_slot(slots)
        try:
            return report.ask("pick one", [{"title": "A"}])
        finally:
            gui.give_slot()
    for stop in (lambda j: j.answer(None), lambda j: j.cancel.set()):
        job = app.start_job("One", "build", asking)
        wait(lambda: job.status == "waiting")
        stop(job)
        wait(lambda: job.status not in ("waiting", "running"))
    assert slots.acquire(blocking=False) and not slots.acquire(blocking=False)


def test_builds_a_restart_cut_off_carry_on_as_the_same_job(tmp_path, monkeypatch):
    import dataclasses
    from nzb2seed.clients import Release
    cfgfile = tmp_path / "nzb2seed.toml"
    cfgfile.write_text("")

    def request(title):
        t = dataclasses.asdict(Release(title, "torrent", "TrackerOne", 1, 10, title, "http://p/1", "", "", 0, 3, None))
        return {"path": "/api/build", "body": {"torrent": t, "nzbs": [], "options": {}}}
    jobs = [
        {"id": 1, "title": "Old.Build", "kind": "build", "status": "interrupted", "repeat": request("Old.Build"),
         "lines": [{"k": "warn", "t": gui.INTERRUPTED}]},                     # an earlier restart: left be
        {"id": 2, "title": "Cut.Off", "kind": "build", "status": "running", "repeat": request("Cut.Off"),
         "lines": [{"k": "info", "t": "Downloading"}]},
        {"id": 3, "title": "Asking", "kind": "build", "status": "waiting", "repeat": request("Asking"), "lines": []},
        {"id": 4, "title": "Folder", "kind": "assemble", "status": "running", "lines": []},  # not a build
        {"id": 5, "title": "Done", "kind": "build", "status": "done", "repeat": request("Done"), "lines": []},
    ]
    (tmp_path / "jobs.json").write_text(json.dumps({"jobs": jobs}))
    ran = []
    monkeypatch.setattr(gui, "execute_run", lambda cfg, opts, t, g, torrent_data=None:
                        ran.append(t.title) or {"result": "built"})
    app = gui.App(str(cfgfile))
    monkeypatch.setattr(app, "build_slot", lambda: threading.Semaphore(5))
    assert app.resume_builds() == [2, 3]
    wait(lambda: app.jobs[2].status == "done" and app.jobs[3].status == "done")
    assert sorted(ran) == ["Asking", "Cut.Off"] and len(app.jobs) == 5          # no new jobs
    assert gui.RESUMING in [l["t"] for l in app.jobs[2].lines] and app.jobs[2].lines[0]["t"] == "Downloading"
    assert app.jobs[1].status == "interrupted" and app.jobs[4].status == "interrupted"
    assert app.resume_builds() == []                                             # once only
