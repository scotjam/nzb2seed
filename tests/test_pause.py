"""Pause (Jobs tab, Torrent list 2 Seed): no build starts, running ones hold, the list
queue waits - and qBittorrent is never touched."""
import threading

import pytest

from nzb2seed import slots, space, tracklist


@pytest.fixture(autouse=True)
def unpaused():
    slots.PAUSED.clear()
    yield
    slots.PAUSED.clear()


def test_no_build_takes_a_slot_while_paused():
    s = slots.FairSlots(2)
    slots.PAUSED.set()
    assert not s.acquire(blocking=False)
    t = s.enter("job")
    assert not s.take(t, timeout=0.05)
    slots.PAUSED.clear()
    s.resize(2)
    assert s.take(t, timeout=0.5)


def test_a_running_build_holds_before_its_next_big_step(monkeypatch):
    monkeypatch.setattr(space, "GATE", None)
    said, naps = [], []
    slots.PAUSED.set()

    def nap(sec):
        naps.append(sec)
        if len(naps) == 3:
            slots.PAUSED.clear()
    space.wait_for_room(step=said.append, sleep=nap)
    assert len(naps) == 3 and "paused" in said[0]


def test_the_list_queue_checks_nothing_while_paused(tmp_path, monkeypatch):
    monkeypatch.setattr(tracklist, "check", lambda *a, **k: pytest.fail("searched while paused"))

    class App:
        cfg = None

        def listed(self, t):
            return None
    q = tracklist.Queue(App(), str(tmp_path / "t.json"), start=False)
    q.add(["Film.2020.1080p-Rel"], "TrackerNine", False, {})
    slots.PAUSED.set()
    assert q.step() is False
    assert q.snapshot()[0]["rows"][0]["state"] == "waiting"


class FakeSab:
    def __init__(self, paused=False):
        self.paused, self.calls = paused, []

    def queue_paused(self):
        return self.paused

    def pause_all(self):
        self.paused = True
        self.calls.append("pause")

    def resume_all(self):
        self.paused = False
        self.calls.append("resume")


def make_app(tmp_path, sab):
    from nzb2seed import gui
    app = gui.App.__new__(gui.App)
    app.cfg = type("C", (), {"path": str(tmp_path / "nzb2seed.toml")})()
    app._sab = lambda: sab
    app.guard = lambda: type("G", (), {"holding": lambda self: False})()
    app.events = type("E", (), {"publish": lambda self, n: None})()
    app.store = type("S", (), {"changed": lambda self, urgent=False: None})()
    app.build_slots = slots.FairSlots(2)
    return app


def test_pause_pauses_sabnzbd_and_resume_undoes_only_that(tmp_path):
    sab = FakeSab()
    app = make_app(tmp_path, sab)
    assert app.set_paused(True)["paused"] and sab.calls == ["pause"]
    assert slots.PAUSED.is_set()
    assert not app.set_paused(False)["paused"] and sab.calls == ["pause", "resume"]
    mine = FakeSab(paused=True)                       # paused by you before: left paused
    app = make_app(tmp_path, mine)
    app.set_paused(True)
    app.set_paused(False)
    assert mine.calls == [] and mine.paused


def test_a_pause_survives_a_restart(tmp_path):
    app = make_app(tmp_path, FakeSab())
    app.set_paused(True)
    slots.PAUSED.clear()                               # the restart
    make_app(tmp_path, FakeSab()).restore_pause()
    assert slots.PAUSED.is_set()


def test_both_tabs_show_the_pause_button():
    import pathlib
    web = pathlib.Path(tracklist.__file__).parent / "web" / "js"
    for tab in ("jobs.js", "tracklist.js"):
        assert "<${PauseButton} />" in (web / "tabs" / tab).read_text(encoding="utf-8")
    src = (web / "components" / "pause.js").read_text(encoding="utf-8")
    assert "Pause" in src and "Resume" in src and "/api/pause" in src
