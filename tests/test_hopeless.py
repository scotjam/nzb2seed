"""SABnzbd's "Abort jobs that cannot be completed": a post it gave up on part-way is
downloaded in full when asked to - with the abort off only while it downloads, or off for
good - and otherwise kept as a last resort, on a retry too."""
import os

import pytest

from nzb2seed import assemble as asm
from nzb2seed import hopeless
from nzb2seed import pipeline
from nzb2seed.pipeline import Options, plan_and_fetch

from test_planner import PR, ep_name, rel
from test_salvage import NAME, HoledSAB, world

ABORT = "Aborted, cannot be completed - https://sabnzbd.org/not-complete"


class Switch:
    """Only SABnzbd's settings: the abort, and every change made to it."""

    def __init__(self, on=True):
        self.on, self.changes = on, []

    def config(self):
        return {"misc": {"fail_hopeless_jobs": 1 if self.on else 0}}

    def _call(self, mode, **p):
        assert mode == "set_config" and p["section"] == "misc" and p["keyword"] == "fail_hopeless_jobs"
        self.on = p["value"] == "1"
        self.changes.append(self.on)


class AbortSAB(HoledSAB, Switch):
    """``aborted``: guids SABnzbd stops part-way while its abort is on; ``retry`` brings
    the rest of the post down."""

    def __init__(self, *a, aborted=(), on=True, **k):
        HoledSAB.__init__(self, *a, **k)
        Switch.__init__(self, on)
        self.aborted, self.retried, self.abort_when_retried = set(aborted), [], []

    def status(self, nzo):
        if self.jobs[nzo] in self.aborted and nzo not in self.retried:
            return "Failed", {"storage": f"/dl/{nzo}", "fail_message": ABORT}
        return super().status(nzo)

    def retry(self, nzo):
        self.retried.append(nzo)
        self.abort_when_retried.append(self.on)
        d = os.path.join(self.root, nzo)
        for rel_, data in self.files.items():            # the rest of it comes down
            p = os.path.join(d, rel_.split("/")[-1])
            if os.path.exists(p):
                with open(p, "wb") as fh:
                    fh.write(data)
        self.holes.pop(self.jobs[nzo], None)
        return nzo


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(pipeline, "GAP_MIN", 1024)
    monkeypatch.setattr(pipeline, "GAP_FILE_MIN", 0)
    monkeypatch.setattr(hopeless, "_holds", 0)
    monkeypatch.setattr(hopeless, "_restore", False)
    pipeline._this_build.aborted = []
    pipeline._this_build.abort_holds = set()


# ------------------------------------------------------------------ the switch itself

def test_the_abort_goes_off_while_posts_download_and_back_on_after_the_last(tmp_path):
    cfg = type("C", (), {"path": str(tmp_path / "c.toml")})()
    sab = Switch(on=True)
    assert hopeless.hold(cfg, sab) and hopeless.hold(cfg, sab)
    assert sab.on is False and sab.changes == [False]
    assert os.path.exists(hopeless._marker(cfg))
    hopeless.release(cfg, sab)
    assert sab.on is False                              # one post still downloading
    hopeless.release(cfg, sab)
    assert sab.on is True and not os.path.exists(hopeless._marker(cfg))


def test_an_abort_already_off_is_left_off(tmp_path):
    cfg = type("C", (), {"path": str(tmp_path / "c.toml")})()
    sab = Switch(on=False)
    assert hopeless.hold(cfg, sab)
    hopeless.release(cfg, sab)
    assert sab.on is False and sab.changes == []


def test_turned_off_in_settings_while_a_post_downloads_it_stays_off(tmp_path):
    cfg = type("C", (), {"path": str(tmp_path / "c.toml")})()
    sab = Switch(on=True)
    hopeless.hold(cfg, sab)
    hopeless.turn_off(cfg, sab)
    hopeless.release(cfg, sab)
    assert sab.on is False and not os.path.exists(hopeless._marker(cfg))


def test_a_restart_mid_download_puts_the_abort_back(tmp_path, monkeypatch):
    cfg = type("C", (), {"path": str(tmp_path / "c.toml")})()
    sab = Switch(on=True)
    hopeless.hold(cfg, sab)
    monkeypatch.setattr(hopeless, "_holds", 0)          # the process restarted
    assert hopeless.restore_after_restart(cfg, sab) and sab.on is True
    assert not hopeless.restore_after_restart(cfg, sab)


def test_unknown_when_sabnzbd_cannot_be_asked():
    class Down:
        def config(self):
            raise OSError("refused")
    assert hopeless.aborts(Down()) is None


# ------------------------------------------------------------------ in a build

def scenario(tmp_path, **k):
    t, cfg, files, size = world(tmp_path)
    a, f = rel(NAME, size, "A"), rel(NAME + "-xpost", size, "F")
    sab = AbortSAB(str(tmp_path / "dl"), t, files, lacks={"A": {ep_name(1, 3)}},
                   holes={"F": {ep_name(1, 3): (2000, 40000)}}, aborted={"F"}, **k)
    return t, cfg, sab, PR([a, f])


def ep3_from(t, dirs):
    return asm.find_sources(t, dirs)[next(r.relpath for r in t.real_files if r.name == ep_name(1, 3))]


def test_without_asking_the_aborted_post_is_a_last_resort_and_offered(tmp_path):
    t, cfg, sab, pr = scenario(tmp_path)
    dirs, _, _, _ = plan_and_fetch(cfg, Options(), pr, sab, t, t.name, [], 2)
    assert sab.retried == [] and sab.changes == []
    assert ep3_from(t, dirs).startswith(os.path.join(str(tmp_path / "dl"), "nzo2"))
    assert [p["title"] for p in pipeline.aborted_posts()] == [NAME + "-xpost"]


def test_asked_to_it_downloads_in_full_with_the_abort_off_only_meanwhile(tmp_path):
    t, cfg, sab, pr = scenario(tmp_path)
    dirs, _, _, _ = plan_and_fetch(cfg, Options(full_posts=True), pr, sab, t, t.name, [], 2)
    assert sab.retried == ["nzo2"] and sab.abort_when_retried == [False]
    assert sab.on is True and sab.changes == [False, True]
    assert pipeline.zero_runs(ep3_from(t, dirs), 1024) == []        # all of it now
    assert pipeline.aborted_posts() == []


def test_with_the_abort_off_in_sabnzbd_a_retry_fetches_the_rest_by_itself(tmp_path):
    t, cfg, sab, pr = scenario(tmp_path)
    plan_and_fetch(cfg, Options(), pr, sab, t, t.name, [], 2)      # aborted, kept as last resort
    sab.on = False                                                  # Settings: download posts in full
    pipeline._this_build.aborted = []
    dirs, _, _, _ = plan_and_fetch(cfg, Options(), PR(pr.results), sab, t, t.name, [], 2)
    assert sab.retried == ["nzo2"] and sab.changes == []            # nothing to turn off or back on
    assert pipeline.zero_runs(ep3_from(t, dirs), 1024) == []


def test_a_retry_salvages_a_post_that_failed_before_instead_of_skipping_it(tmp_path):
    t, cfg, sab, pr = scenario(tmp_path)
    plan_and_fetch(cfg, Options(), pr, sab, t, t.name, [], 2)
    pipeline._this_build.aborted = []
    dirs, _, _, _ = plan_and_fetch(cfg, Options(), PR(pr.results), sab, t, t.name, [], 2)
    assert sab.retried == [] and len(sab.added) == 2                # nothing downloaded again
    assert ep3_from(t, dirs).startswith(os.path.join(str(tmp_path / "dl"), "nzo2"))
    assert [p["title"] for p in pipeline.aborted_posts()] == [NAME + "-xpost"]
