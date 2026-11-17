"""Build slots in a known order: builds started by hand in the order they came, automatic
ones newest torrent first - and the Jobs tab shows that order."""
import threading
import time

from nzb2seed.slots import FairSlots


def _wait_in_line(slots, names, started, rank=lambda n: 0.0):
    """Each name queues for a slot from its own thread, in this order; records the order
    they get one in."""
    threads = []
    for n in names:
        t = threading.Thread(target=lambda n=n: (slots.take(slots.enter(n, rank(n)), 10), started.append(n)))
        t.start()
        threads.append(t)
        while n not in slots.waiting():
            time.sleep(0.005)
    return threads


def test_slots_go_in_the_order_builds_came():
    slots = FairSlots(1)
    assert slots.acquire(blocking=False)                # one build running
    started = []
    threads = _wait_in_line(slots, ["a", "b", "c"], started)
    assert slots.waiting() == ["a", "b", "c"]
    for i in range(3):
        slots.release()                                  # one build ends: the next starts
        while len(started) < i + 1:
            time.sleep(0.005)
    for t in threads:
        t.join(2)
    assert started == ["a", "b", "c"]


def test_a_ranked_line_starts_the_newest_torrent_first():
    slots = FairSlots(1)
    assert slots.acquire(blocking=False)
    posted = {"old": 1_000, "new": 3_000, "middle": 2_000}
    started = []
    threads = _wait_in_line(slots, ["old", "new", "middle"], started, rank=lambda n: -posted[n])
    assert slots.waiting() == ["new", "middle", "old"]
    assert [round(-r) for r, _ in slots.line()] == [3000, 2000, 1000]
    for i in range(3):
        slots.release()                                  # one build ends: the next starts
        while len(started) < i + 1:
            time.sleep(0.005)
    for t in threads:
        t.join(2)
    assert started == ["new", "middle", "old"]


def test_a_free_slot_is_not_taken_past_a_build_already_waiting():
    slots = FairSlots(1)
    assert slots.acquire(blocking=False)
    ticket = slots.enter("waiting")
    slots.release()
    assert not slots.acquire(blocking=False)            # it is the waiting build's
    assert slots.take(ticket, 1)


def test_a_cancelled_build_leaves_the_line():
    slots = FairSlots(1)
    assert slots.acquire(blocking=False)
    a, b = slots.enter("a"), slots.enter("b")
    slots.leave(a)
    assert slots.waiting() == ["b"]
    slots.release()
    assert slots.take(b, 1)


def test_more_slots_let_more_of_the_line_start():
    slots = FairSlots(1)
    assert slots.acquire(blocking=False)
    a, b = slots.enter("a"), slots.enter("b")
    slots.resize(3)
    assert slots.take(a, 1) and slots.take(b, 1)


def test_the_jobs_list_says_where_a_waiting_build_stands(tmp_path):
    from nzb2seed import gui
    cfgfile = tmp_path / "nzb2seed.toml"
    cfgfile.write_text("")
    app = gui.App(str(cfgfile))
    app.build_slots = FairSlots(1)
    assert app.build_slots.acquire(blocking=False)            # one build running
    first, second, building = (gui.Job(i, f"Film.{i}", "build") for i in (1, 2, 3))
    app.build_slots.enter(first)
    app.build_slots.enter(second)
    assert app.queue_place(first) == {"queue": "build", "place": 1}
    assert app.queue_place(second) == {"queue": "build", "place": 2}
    assert app.queue_place(building) == {}                   # not in line: building


def test_a_build_moved_to_the_top_starts_next():
    slots = FairSlots(1)
    assert slots.acquire(blocking=False)
    a, b, c = slots.enter("a"), slots.enter("b"), slots.enter("c")
    assert slots.to_front("c")
    assert slots.waiting() == ["c", "a", "b"]
    assert slots.to_front("b")                                # the latest move goes on top
    assert slots.waiting() == ["b", "c", "a"]
    assert not slots.to_front("not waiting")
    slots.release()
    assert not slots.take(a, 0.05) and slots.take(b, 1)


def test_moving_to_the_top_keeps_how_new_the_torrent_is():
    slots = FairSlots(1)
    assert slots.acquire(blocking=False)
    slots.enter("new", -3000.0), slots.enter("old", -1000.0)
    slots.to_front("old")
    assert slots.line() == [(-1000.0, "old"), (-3000.0, "new")]   # the Jobs tab still says when it was posted


def test_a_forced_build_starts_on_top_of_the_slots_in_use():
    slots = FairSlots(1)
    assert slots.acquire(blocking=False)                    # the only slot is busy
    a, b = slots.enter("a"), slots.enter("b")
    assert slots.force("b")
    assert slots.take(b, 1)                                 # starts now, past the limit
    assert slots.used == 2 and slots.waiting() == ["a"]
    slots.release()                                         # one ends: still one more than the limit
    assert not slots.take(a, 0.05)
    slots.release()
    assert slots.take(a, 1)
    assert not slots.force("not waiting")


def test_a_forced_build_still_waits_while_paused():
    from nzb2seed.slots import PAUSED
    slots = FairSlots(1)
    t = slots.enter("a")
    PAUSED.set()
    try:
        slots.force("a")
        assert not slots.take(t, 0.05)
    finally:
        PAUSED.clear()
    assert slots.take(t, 1)


def test_the_jobs_tab_can_move_or_start_a_queued_build(tmp_path):
    import pytest
    from nzb2seed import gui
    cfgfile = tmp_path / "nzb2seed.toml"
    cfgfile.write_text("")
    app = gui.App(str(cfgfile))
    app.build_slots = FairSlots(1)
    assert app.build_slots.acquire(blocking=False)
    first, second, building = (gui.Job(i, f"Film.{i}", "build") for i in (1, 2, 3))
    app.build_slots.enter(first)
    ticket = app.build_slots.enter(second)
    assert "top of the build queue" in app.jump_queue(second, "top")
    assert app.queue_place(second) == {"queue": "build", "place": 1}
    assert "Starting now, on top of the 1 build already running" in app.jump_queue(first, "force")
    with pytest.raises(ValueError):
        app.jump_queue(building, "top")                      # not waiting: nothing to move
    with pytest.raises(ValueError):
        app.jump_queue(second, "sideways")
    assert ticket


def test_builds_resumed_at_once_keep_the_order_they_were_queued_in(tmp_path, monkeypatch):
    """After a restart every interrupted build comes back at once, each from its own thread:
    the line goes by job number, not by which thread happens to get there first."""
    from nzb2seed import gui, report
    cfgfile = tmp_path / "nzb2seed.toml"
    cfgfile.write_text("")
    app = gui.App(str(cfgfile))
    app.build_slots = FairSlots(1)
    assert app.build_slots.acquire(blocking=False)               # one build running
    jobs = {i: gui.Job(i, f"Film.{i}", "build") for i in (5, 3, 9, 4)}

    class Sink:
        def __init__(self, job):
            self.job = job

        def __getattr__(self, name):
            return lambda *a, **k: None
    monkeypatch.setattr(report, "check_cancel", lambda: None)
    started, threads = [], []
    for i in (9, 4, 5, 3):                                       # threads arriving out of order
        def run(job=jobs[i]):
            report.use(Sink(job))
            gui.take_slot(app.build_slots)
            started.append(job.id)
            gui.give_slot()
        t = threading.Thread(target=run)
        t.start()
        threads.append(t)
        while jobs[i] not in app.build_slots.waiting():
            time.sleep(0.005)
    assert [j.id for j in app.build_slots.waiting()] == [3, 4, 5, 9]
    app.build_slots.release()
    for t in threads:
        t.join(5)
    assert started == [3, 4, 5, 9]


def test_a_build_cut_off_part_way_gets_its_slot_back_first(tmp_path, monkeypatch):
    """A build that held a slot when nzb2seed stopped (SABnzbd already downloading for it)
    takes one back before older builds that had not started - and one you had started now
    is started now again. Handing the slot back (done, or waiting on you) forgets it."""
    from nzb2seed import gui, report, slots as slots_mod
    monkeypatch.setattr(report, "check_cancel", lambda: None)

    class Sink:
        def __init__(self, job):
            self.job = job

        def __getattr__(self, name):
            return lambda *a, **k: None
    old, cut_off, forced = gui.Job(1, "Film.1", "build"), gui.Job(5, "Film.5", "build"), gui.Job(7, "Film.7", "build")
    cut_off.extra = {slots_mod.HOLDING: "slot"}                      # kept in jobs.json over the restart
    forced.extra = {slots_mod.HOLDING: "forced"}
    slots = FairSlots(1)
    assert slots.acquire(blocking=False)                             # the only slot is busy
    slots.enter(old, 1.0, **slots_mod.queue_marks(old))
    got = []

    def run(job):
        report.use(Sink(job))
        gui.take_slot(slots)
        got.append(job.id)
        assert job.extra.get(slots_mod.HOLDING)                       # holds one now
        if job is cut_off:
            gui.give_slot()
            assert slots_mod.HOLDING not in job.extra               # handed back: forgotten
    t7 = threading.Thread(target=run, args=(forced,))
    t7.start()
    t7.join(5)
    assert got == [7]                                                 # started now again, past the limit
    t5 = threading.Thread(target=run, args=(cut_off,))
    t5.start()
    while cut_off not in slots.waiting():
        time.sleep(0.005)
    assert slots.waiting() == [cut_off, old]                          # ahead of the older one
    slots.release()                                                   # the forced build's slot
    slots.release()                                                   # the one that was busy
    t5.join(5)
    assert got == [7, 5]


def test_every_line_breaks_ties_by_job_number_not_arrival():
    """The automatic line (newest torrent first) too: torrents posted at the same moment, or
    with no time known, go in the order their jobs were made, whichever thread is first."""
    from nzb2seed import gui
    slots = FairSlots(1)
    assert slots.acquire(blocking=False)
    j7, j2, j5 = (gui.Job(i, f"Film.{i}", "auto") for i in (7, 2, 5))
    for j in (j7, j2, j5):                                        # arriving 7, 2, 5
        slots.enter(j, -1_000.0)                                  # same posting time
    assert [j.id for j in slots.waiting()] == [2, 5, 7]
    newer = gui.Job(9, "Film.9", "auto")
    slots.enter(newer, -2_000.0)                                  # posted later: still first
    assert [j.id for j in slots.waiting()] == [9, 2, 5, 7]


def test_move_to_top_and_start_now_survive_a_restart(tmp_path):
    """Kept on the job, so the line rebuilt after a restart puts them back - and forgotten
    once the build has its slot."""
    from nzb2seed import gui, slots as slots_mod
    cfgfile = tmp_path / "nzb2seed.toml"
    cfgfile.write_text("")
    app = gui.App(str(cfgfile))
    app.build_slots = FairSlots(1)
    assert app.build_slots.acquire(blocking=False)
    a, b, c = (gui.Job(i, f"Film.{i}", "build") for i in (1, 2, 3))
    for j in (a, b, c):
        app.build_slots.enter(j, float(j.id))
    app.jump_queue(c, "top")
    app.jump_queue(b, "force")
    assert c.extra.get("queue_front") and b.extra.get("queue_forced")
    # the restart: a new line, everyone back in it in whatever order
    after = FairSlots(1)
    assert after.acquire(blocking=False)
    tb = after.enter(b, 2.0, **slots_mod.queue_marks(b))
    after.enter(a, 1.0, **slots_mod.queue_marks(a))
    after.enter(c, 3.0, **slots_mod.queue_marks(c))
    assert after.waiting()[0] is c                               # still on top
    assert after.take(tb, 1)                                     # still started now, past the limit
    slots_mod.clear_queue_marks(b)
    assert "queue_forced" not in (b.extra or {})


def test_a_download_shows_how_far_it_is_and_how_fast(monkeypatch):
    from nzb2seed import pipeline, report
    seen = []

    class Sink(report.ConsoleSink):
        def meter(self, pct, speed, what):
            seen.append((pct, speed, what))
    looks = iter([("Queued: Downloading", {"mb": "1000", "mbleft": "800"}),
                  ("Queued: Downloading", {"mb": "1000", "mbleft": "300"}),
                  ("Completed", {"storage": "/downloads/Rel"})])

    class Sab:
        def status(self, nzo):
            return next(looks)
    clock = iter([100.0, 110.0, 120.0, 130.0])
    monkeypatch.setattr(pipeline.time, "sleep", lambda s: None)
    monkeypatch.setattr(pipeline.time, "monotonic", lambda: next(clock))
    report.use(Sink())
    try:
        pipeline.sab_wait(Sab(), {"nzo1": "Rel"})
    finally:
        report.use(None)
    assert seen[0] == (20.0, None, "downloading")
    pct, speed, _ = seen[1]
    assert pct == 70.0 and abs(speed - 50 * 1024 * 1024) < 1      # 500 MB in 10 s
