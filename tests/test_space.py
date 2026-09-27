"""Pausing downloads before the disk fills up - and starting again only what was paused."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from nzb2seed import space  # noqa: E402
from nzb2seed.clients import ApiError  # noqa: E402
from nzb2seed.config import Config, finalize  # noqa: E402

GB = space.GB


class FakeSab:
    def __init__(self, paused=False, incomplete="/downloads/incomplete"):
        self.paused, self.incomplete = paused, incomplete
        self.calls = []

    def incomplete_dir(self):
        return self.incomplete

    def queue_paused(self):
        return self.paused

    def pause_all(self):
        self.calls.append("pause")
        self.paused = True

    def resume_all(self):
        self.calls.append("resume")
        self.paused = False


class FakeQbit:
    def __init__(self, torrents):
        self._t = {t["hash"]: t for t in torrents}
        self.stopped, self.started = [], []

    def torrents(self):
        return list(self._t.values())

    def stop(self, h):
        self.stopped.append(h)
        self._t[h]["state"] = "pausedDL"

    def start(self, h):
        self.started.append(h)
        self._t[h]["state"] = "downloading"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    def build(free_gb=100.0, total_gb=1000.0, min_pct=0.0, min_gb=0.0, torrents=False):
        out = tmp_path / "out"
        out.mkdir(exist_ok=True)
        cfg = finalize(Config(path=tmp_path / "nzb2seed.toml", output_dir=str(out),
                              space_min_percent=min_pct, space_min_gb=min_gb,
                              space_pause_torrents=torrents))
        monkeypatch.setattr(space, "free_on", lambda p: (int(free_gb * GB), int(total_gb * GB)))
        guard = space.Guard(str(tmp_path / "space.json"))
        return cfg, guard
    return build


# ---------------------------------------------------------------- reading the limit

def test_no_limit_means_the_guard_never_acts(setup):
    cfg, guard = setup(free_gb=0.5)
    sab = FakeSab()
    assert space.limits_set(cfg) is False
    assert guard.run(cfg, sab, None, log=lambda *_: None)["held"] is False
    assert sab.calls == []


def test_a_percentage_limit(setup):
    cfg, _ = setup(free_gb=20, total_gb=1000, min_pct=5)
    assert "2.0% free" in space.check(cfg)["why"]
    cfg2, _ = setup(free_gb=200, total_gb=1000, min_pct=5)
    assert space.check(cfg2)["low"] is False


def test_a_gigabyte_limit(setup):
    cfg, _ = setup(free_gb=20, min_gb=50)
    assert "20.0 GB free" in space.check(cfg)["why"]
    cfg2, _ = setup(free_gb=80, min_gb=50)
    assert space.check(cfg2)["low"] is False


def test_either_limit_is_enough_to_trip_it(setup):
    cfg, _ = setup(free_gb=20, total_gb=1000, min_pct=50, min_gb=1)
    why = space.check(cfg)["why"]
    assert "50% limit" in why and "1 GB limit" not in why   # only the one that is breached


def test_it_says_how_much_has_to_be_freed(setup):
    """The number you act on is what restarts downloading - which is the limit plus the
    margin, not the limit. Freeing just enough to reach the limit would restart nothing."""
    cfg, _ = setup(free_gb=20, total_gb=1000, min_pct=5)            # 5% of 1000 = 50 GB
    assert "free 35.0 GB more to start downloading again" in space.check(cfg)["why"]
    cfg2, _ = setup(free_gb=20, min_gb=50)                          # 50 * 1.1 - 20
    assert "free 35.0 GB more to start downloading again" in space.check(cfg2)["why"]


def test_the_amount_to_free_is_the_same_whether_stopping_or_held(setup):
    """It is the same disk and the same bar: only the wording of the breach differs."""
    cfg, _ = setup(free_gb=20, min_gb=50)
    stopping, held = space.check(cfg)["why"], space.check(cfg, resuming=True)["why"]
    assert "free 35.0 GB more to start downloading again" in stopping
    assert "free 35.0 GB more to start downloading again" in held
    assert "under the 50 GB limit" in stopping                   # what stopped it
    assert "55.0 GB is needed before downloading starts again" in held   # what restarts it


def test_the_worst_of_the_two_limits_decides_how_much_to_free(setup):
    cfg, _ = setup(free_gb=20, total_gb=1000, min_pct=5, min_gb=100)
    assert "free 90.0 GB more" in space.check(cfg)["why"]        # 100 * 1.1 - 20, not the 5%


# ---------------------------------------------------------------- pausing and resuming

def test_it_pauses_sabnzbd_when_the_disk_is_low(setup):
    cfg, guard = setup(free_gb=5, min_gb=50)
    sab = FakeSab()
    out = guard.run(cfg, sab, None, log=lambda *_: None)
    assert out["held"] and sab.paused and sab.calls == ["pause"]
    assert guard.holding()


def test_it_resumes_once_there_is_room(setup, tmp_path, monkeypatch):
    cfg, guard = setup(free_gb=5, min_gb=50)
    sab = FakeSab()
    guard.run(cfg, sab, None, log=lambda *_: None)
    monkeypatch.setattr(space, "free_on", lambda p: (int(200 * GB), int(1000 * GB)))
    out = guard.run(cfg, sab, None, log=lambda *_: None)
    assert out["held"] is False and not sab.paused and sab.calls == ["pause", "resume"]
    assert not guard.holding()


def test_a_queue_you_paused_yourself_is_not_claimed(setup):
    """It only resumes what it paused - your own pause survives."""
    cfg, guard = setup(free_gb=5, min_gb=50)
    sab = FakeSab(paused=True)
    guard.run(cfg, sab, None, log=lambda *_: None)
    assert sab.calls == [] and not guard.holding()


def test_it_waits_for_a_margin_before_resuming(setup, monkeypatch):
    """Just over the line is not enough, or downloads would stop and start all day.
    The margin is 1.1x, so a 50 GB limit resumes at 55 GB."""
    cfg, guard = setup(free_gb=5, min_gb=50)
    sab = FakeSab()
    guard.run(cfg, sab, None, log=lambda *_: None)
    monkeypatch.setattr(space, "free_on", lambda p: (int(52 * GB), int(1000 * GB)))
    assert guard.run(cfg, sab, None, log=lambda *_: None)["held"] is True     # past 50, under 55
    monkeypatch.setattr(space, "free_on", lambda p: (int(60 * GB), int(1000 * GB)))
    assert guard.run(cfg, sab, None, log=lambda *_: None)["held"] is False


def test_what_it_paused_is_remembered_across_a_restart(setup, tmp_path, monkeypatch):
    cfg, guard = setup(free_gb=5, min_gb=50)
    sab = FakeSab()
    guard.run(cfg, sab, None, log=lambda *_: None)
    again = space.Guard(str(tmp_path / "space.json"))         # a new process
    assert again.holding()
    monkeypatch.setattr(space, "free_on", lambda p: (int(500 * GB), int(1000 * GB)))
    again.run(cfg, sab, None, log=lambda *_: None)
    assert sab.calls == ["pause", "resume"] and not again.holding()


def test_sabnzbd_refusing_to_resume_keeps_the_note(setup, monkeypatch):
    cfg, guard = setup(free_gb=5, min_gb=50)
    sab = FakeSab()
    guard.run(cfg, sab, None, log=lambda *_: None)
    monkeypatch.setattr(space, "free_on", lambda p: (int(500 * GB), int(1000 * GB)))

    def boom():
        raise ApiError("no")

    sab.resume_all = boom
    guard.run(cfg, sab, None, log=lambda *_: None)
    assert guard.holding()                                    # so the next pass tries again


# ---------------------------------------------------------------- torrents

TORRENTS = [{"hash": "a" * 40, "name": "leeching", "state": "downloading"},
            {"hash": "b" * 40, "name": "seeding", "state": "uploading"},
            {"hash": "c" * 40, "name": "yours, already paused", "state": "pausedDL"}]


def test_torrents_are_left_alone_by_default(setup):
    cfg, guard = setup(free_gb=5, min_gb=50)
    qb = FakeQbit([dict(t) for t in TORRENTS])
    guard.run(cfg, FakeSab(), qb, log=lambda *_: None)
    assert qb.stopped == []                       # pausing a leech can cost H&R grace


def test_only_downloading_torrents_are_stopped_when_asked(setup):
    cfg, guard = setup(free_gb=5, min_gb=50, torrents=True)
    qb = FakeQbit([dict(t) for t in TORRENTS])
    guard.run(cfg, FakeSab(), qb, log=lambda *_: None)
    assert qb.stopped == ["a" * 40]               # never the seeding one, never yours


def test_only_the_torrents_it_stopped_are_started_again(setup, monkeypatch):
    cfg, guard = setup(free_gb=5, min_gb=50, torrents=True)
    qb = FakeQbit([dict(t) for t in TORRENTS])
    guard.run(cfg, FakeSab(), qb, log=lambda *_: None)
    monkeypatch.setattr(space, "free_on", lambda p: (int(500 * GB), int(1000 * GB)))
    guard.run(cfg, FakeSab(), qb, log=lambda *_: None)
    assert qb.started == ["a" * 40]               # the one you had paused stays paused


def test_check_reports_each_disk(setup):
    cfg, _ = setup(free_gb=20, total_gb=1000, min_gb=50)
    out = space.check(cfg)
    assert out["limits_set"] and out["low"] and len(out["disks"]) == 1
    assert out["disks"][0]["percent"] == 2.0


def test_defaults_are_off(tmp_path):
    cfg = finalize(Config(path=tmp_path / "nzb2seed.toml"))
    assert cfg.space_min_percent == 0 and cfg.space_min_gb == 0
    assert cfg.space_pause_torrents is False
    assert space.limits_set(cfg) is False


# ---------------------------------------------------------------- a running build waits

@pytest.fixture
def gate(monkeypatch):
    """Control what the gate says, and count how long a build would wait."""
    state = {"why": "", "slept": 0, "said": []}
    monkeypatch.setattr(space, "GATE", lambda role="": state["why"])

    def sleep(_):
        state["slept"] += 1
        if state["slept"] >= 3:
            state["why"] = ""                 # room appears on the third look
    state["sleep"] = sleep
    return state


def test_a_build_waits_instead_of_writing_while_the_disk_is_full(gate):
    gate["why"] = "2.0% free, less than the 5% limit"
    space.wait_for_room(step=gate["said"].append, sleep=gate["sleep"])
    assert gate["slept"] == 3                                    # it waited, then carried on
    assert gate["said"] == ["waiting for disk space: 2.0% free, less than the 5% limit"]


def test_a_build_is_not_held_up_when_there_is_room(gate):
    space.wait_for_room(step=gate["said"].append, sleep=gate["sleep"])
    assert gate["slept"] == 0 and gate["said"] == []


def test_waiting_says_so_once_not_every_time_round(gate):
    gate["why"] = "full"
    space.wait_for_room(step=gate["said"].append, sleep=gate["sleep"])
    assert len(gate["said"]) == 1


def test_stopping_a_build_works_while_it_waits(gate, monkeypatch):
    from nzb2seed import report
    gate["why"] = "full"
    monkeypatch.setattr(report, "_sink", None, raising=False)

    class Stopped(Exception):
        pass

    def boom():
        raise Stopped()

    monkeypatch.setattr("nzb2seed.report.check_cancel", boom)
    with pytest.raises(Stopped):
        space.wait_for_room(step=gate["said"].append, sleep=gate["sleep"])


def test_a_broken_gate_never_wedges_a_build(monkeypatch):
    def boom(role=""):
        raise RuntimeError("qBittorrent is down")

    monkeypatch.setattr(space, "GATE", boom)
    space.wait_for_room(sleep=lambda _: pytest.fail("it should not have waited"))


def test_no_gate_at_all_means_no_waiting(monkeypatch):
    monkeypatch.setattr(space, "GATE", None)
    space.wait_for_room(sleep=lambda _: pytest.fail("it should not have waited"))


# ---------------------------------------------------------------- what a blocked job says

def test_a_job_waiting_on_the_disk_says_the_limit_in_its_log(monkeypatch):
    """The progress line is not kept, so the reason - which names the limit and how much
    to free - has to reach the job's log as well."""
    from nzb2seed import pipeline
    said = []
    monkeypatch.setattr(pipeline, "warn", said.append)
    monkeypatch.setattr(pipeline, "info", said.append)
    monkeypatch.setattr(pipeline, "progress", lambda *_: None)
    monkeypatch.setattr(pipeline, "end_progress", lambda: None)
    monkeypatch.setattr(pipeline.time, "sleep", lambda *_: None)
    reason = ["529.2 GB free, less than the 550 GB limit; free 20.8 GB more to carry on"]
    monkeypatch.setattr(space, "GATE", lambda role="": reason[0])

    class Sab:
        def __init__(self):
            self.looks = 0

        def status(self, nzo):
            self.looks += 1
            if self.looks > 3:                      # room appears, then it finishes
                reason[0] = ""
            if self.looks > 4:
                return "Completed", {"storage": "/somewhere"}
            return "Queued:Paused", {"percentage": "86", "timeleft": "0:00:00"}

    pipeline.sab_wait(Sab(), {"nzo1": "Some.Release"})
    blocked = [x for x in said if "waiting for disk space" in x]
    assert len(blocked) == 1                        # said once, not every five seconds
    assert "550 GB limit" in blocked[0] and "free 20.8 GB more" in blocked[0]
    assert any("there is room again" in x for x in said)


# ---------------------------------------------------------------- a limit per disk

def two_disks(tmp_path, monkeypatch, out_free, dl_free, **limits):
    """An output disk and a downloads disk, each with its own free space."""
    import dataclasses
    from nzb2seed.config import Config, finalize
    out, dl = tmp_path / "out", tmp_path / "dl"
    out.mkdir(exist_ok=True); dl.mkdir(exist_ok=True)
    cfg = finalize(Config(path=tmp_path / "nzb2seed.toml", output_dir=str(out), **limits))
    monkeypatch.setattr(space, "_sab_dir", lambda c, s: str(dl))
    free = {str(out): out_free, str(dl): dl_free}
    monkeypatch.setattr(space, "free_on", lambda p: (int(free[p] * GB), int(1000 * GB)))
    return cfg


def test_each_disk_is_held_to_its_own_limit(tmp_path, monkeypatch):
    """Staging empties after every build; the disk that keeps what is seeded only grows."""
    cfg = two_disks(tmp_path, monkeypatch, out_free=400, dl_free=120,
                    space_output_min_gb=300, space_downloads_min_gb=200)
    out = space.check(cfg, FakeSab())
    assert out["low"] is True
    by_role = {d["role"]: d for d in out["disks"]}
    assert by_role["output"]["why"] == ""                       # 400 free, limit 300
    assert "120.0 GB free, under the 200 GB limit" in by_role["downloads"]["why"]
    assert "downloads disk" in out["why"]                       # it says which one


def test_a_disk_without_its_own_limit_uses_the_general_one(tmp_path, monkeypatch):
    cfg = two_disks(tmp_path, monkeypatch, out_free=90, dl_free=400,
                    space_min_gb=100, space_downloads_min_gb=200)
    by_role = {d["role"]: d for d in space.check(cfg, FakeSab())["disks"]}
    assert "under the 100 GB limit" in by_role["output"]["why"]  # fell back to the general
    assert by_role["downloads"]["why"] == ""                     # 400 free, its own limit 200


def test_one_disk_over_its_limit_is_enough_to_hold_everything(tmp_path, monkeypatch):
    cfg = two_disks(tmp_path, monkeypatch, out_free=900, dl_free=50, space_downloads_min_gb=100)
    assert space.check(cfg, FakeSab())["low"] is True


def test_no_limit_anywhere_means_the_guard_is_idle(tmp_path, monkeypatch):
    cfg = two_disks(tmp_path, monkeypatch, out_free=1, dl_free=1)
    assert space.limits_set(cfg) is False
    assert space.check(cfg, FakeSab())["low"] is False


def test_a_limit_on_either_disk_switches_the_guard_on(tmp_path, monkeypatch):
    assert space.limits_set(two_disks(tmp_path, monkeypatch, 500, 500, space_output_min_gb=10))
    assert space.limits_set(two_disks(tmp_path, monkeypatch, 500, 500, space_downloads_min_percent=5))


def test_placing_files_waits_only_on_the_disk_it_writes_to(monkeypatch):
    """Several builds at once, all sitting on finished downloads, is a deadlock if the step
    that empties staging is itself held back by staging being full."""
    asked = []

    def gate(role=""):
        asked.append(role)
        return "staging is full" if role == "downloads" else ""

    monkeypatch.setattr(space, "GATE", gate)
    waited = []
    space.wait_for_room(step=waited.append, sleep=lambda _: waited.append("slept"),
                        role="output")
    assert asked == ["output"] and waited == []        # the output disk has room: carry on


def test_a_download_still_waits_on_staging(monkeypatch):
    calls = {"n": 0}

    def gate(role=""):
        calls["n"] += 1
        return "staging is full" if calls["n"] < 3 else ""

    monkeypatch.setattr(space, "GATE", gate)
    slept = []
    space.wait_for_room(step=lambda *_: None, sleep=lambda _: slept.append(1), role="downloads")
    assert len(slept) == 2                             # waited, then went on by itself
