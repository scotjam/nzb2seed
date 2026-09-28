"""Which release groups, at which resolutions, turn out to be on Usenet - and what automatic
builds do with that: stop, untried, what never is; build first what likely is."""
import dataclasses
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from nzb2seed import clients, gui, inbox, usenet_odds  # noqa: E402
from nzb2seed.config import Config  # noqa: E402
from nzb2seed.pipeline import Abort, Incomplete  # noqa: E402

from helpers import make_torrent  # noqa: E402
from test_gui_login import call, server  # noqa: E402, F401  (fixture)

AUTH = ("admin", "nzb2seed")


def wait(pred, timeout=10):
    end = time.time() + timeout
    while time.time() < end and not pred():
        time.sleep(0.05)
    assert pred()


def odds(tmp_path):
    return usenet_odds.odds_for(Config(path=str(tmp_path / "c.toml")))


def test_a_group_and_resolution_is_the_key():
    k = usenet_odds.key
    assert k("Show.S01.1080p.WEB-DL-GRPA") == k("auto: Show S02 1080p WEB-GRPA") == "grpa|1080p"
    assert k("Show (2022) S01 (1080p WEB English - GRPH)") == "grph|1080p"
    assert k("Film.2020.BluRay-GRPA") == "grpa|any" and k("no group here") is None


def test_never_found_in_enough_tries_is_never(tmp_path):
    o = odds(tmp_path)
    for i in range(usenet_odds.MIN_TRIES - 1):
        o.record(f"Show.S0{i}.1080p-GRPB", False)
    assert o.never("Show.S09.1080p-GRPB") is None                 # not enough tries yet
    o.record("Show.S08.1080p-GRPB", False)
    assert "never been found on Usenet" in o.never("Show.S09.1080p-GRPB")
    assert o.never("Show.S09.720p-GRPB") is None                  # another resolution: its own
    o.record("Show.S10.1080p-GRPB", True)
    assert o.never("Show.S09.1080p-GRPB") is None                 # found once: worth trying


def test_the_likelier_ones_rate_higher(tmp_path):
    o = odds(tmp_path)
    for _ in range(4):
        o.record("Film.2020.1080p-GRPA", True)
    o.record("Film.2021.1080p-GRPC", False)
    assert o.rate("Film.2022.1080p-GRPA") > o.rate("Film.2022.1080p-NEWGRP") > o.rate("Film.2022.1080p-GRPC")


def test_the_history_there_is_seeds_it_once(tmp_path):
    o = odds(tmp_path)
    o.seed([("Show.S01.1080p-GRPA", True), ("Show.S02.1080p-GRPA", False)])
    o.seed([("Show.S03.1080p-GRPA", True)] * 10)                  # already seeded: ignored
    row = o.rows()[0]
    assert (row["group"], row["found"], row["missing"]) == ("grpa", 1, 1)


def test_every_build_that_ends_is_learned_from(server):  # noqa: F811
    app, _ = server
    o = usenet_odds.odds_for(app.cfg)
    jobs = [app.start_job("Show.S01.1080p-GRPA", "build", lambda cfg: {"result": "done"}),
            app.start_job("Show.S02.1080p-GRPA", "build",
                          lambda cfg: (_ for _ in ()).throw(Incomplete("short", 0.999, "a" * 40, "/t", "/d", in_client=False))),
            app.start_job("auto: Show.S01.1080p-GRPB", "auto",
                          lambda cfg: (_ for _ in ()).throw(Abort("gave up: no Usenet post could supply S01E01"))),
            app.start_job("Show.S03.1080p-GRPA", "build", lambda cfg: (_ for _ in ()).throw(Abort("disk full")))]
    wait(lambda: all(j.status != "running" for j in jobs))
    time.sleep(0.2)
    rows = {(r["group"], r["res"]): (r["found"], r["missing"]) for r in o.rows()}
    assert rows == {("grpa", "1080p"): (2, 0), ("grpb", "1080p"): (0, 1)}   # "disk full" says nothing


def automatic(tmp_path, monkeypatch, **cfg):
    cfgfile = tmp_path / "nzb2seed.toml"
    cfgfile.write_text("")
    app = gui.App(str(cfgfile))
    box = tmp_path / "inbox"
    box.mkdir()
    settings = dict(auto_enabled=True, auto_folder=str(box), auto_retry_minutes=60,
                    auto_retry_first_minutes=60, auto_wait_hours=48)
    settings.update(cfg)
    app.cfg = dataclasses.replace(app.cfg, **settings)
    calls = []
    monkeypatch.setattr(inbox, "execute_run", lambda *a, **k: calls.append(1) or {"result": "done"})
    monkeypatch.setattr(inbox, "POLL_SECONDS", 3600)
    app.start_inbox()
    return app, box, calls


def arrive(box, name):
    t = box / f"{name}.torrent"
    t.write_bytes(make_torrent(name, {"a.mkv": name.encode() * 3000}))
    old = time.time() - 60
    os.utime(t, (old, old))


def test_a_group_never_on_usenet_is_stopped_on_arrival_without_a_search(tmp_path, monkeypatch):
    app, box, calls = automatic(tmp_path, monkeypatch)
    try:
        o = usenet_odds.odds_for(app.cfg)
        for i in range(usenet_odds.MIN_TRIES):
            o.record(f"Show.S0{i}.1080p-GRPB", False)
        jobs_before = len(app.jobs)
        arrive(box, "Show.S09.1080p-GRPB")
        app.inbox.poll()
        it = app.inbox.items()[0]
        assert it["status"] == "failed" and "never been found on Usenet" in it["why"]
        assert len(app.jobs) == jobs_before and calls == []       # no job, no build, no search
        assert os.listdir(box / ".failed")                        # and it can be tried again
    finally:
        clients.SEARCH_GATE = None


def test_switched_off_it_is_tried_as_before(tmp_path, monkeypatch):
    app, box, calls = automatic(tmp_path, monkeypatch, auto_skip_unposted=False)
    try:
        o = usenet_odds.odds_for(app.cfg)
        for i in range(usenet_odds.MIN_TRIES):
            o.record(f"Show.S0{i}.1080p-GRPB", False)
        arrive(box, "Show.S09.1080p-GRPB")
        app.inbox.poll()
        wait(lambda: calls)
    finally:
        clients.SEARCH_GATE = None


def test_the_ones_likely_on_usenet_are_built_first(tmp_path, monkeypatch):
    app, box, calls = automatic(tmp_path, monkeypatch, auto_enabled=False)
    try:
        o = usenet_odds.odds_for(app.cfg)
        for _ in range(4):
            o.record("Film.2019.1080p-GOOD", True)
        for _ in range(3):
            o.record("Film.2019.1080p-POOR", False)
        now = time.time()
        for name, first in (("Film.2020.1080p-POOR", now - 30), ("Film.2021.1080p-GOOD", now)):
            app.inbox.state.update(name, name=name, status="queued", first_seen=first, file="x")
        order = []
        monkeypatch.setattr(app.inbox, "start", lambda h: order.append(h))
        app.cfg = dataclasses.replace(app.cfg, auto_enabled=True)
        app.inbox.poll()
        assert order == ["Film.2021.1080p-GOOD", "Film.2020.1080p-POOR"]   # likelier first, though newer
    finally:
        clients.SEARCH_GATE = None


def test_the_demand_tab_shows_what_is_found_on_usenet(server, monkeypatch):  # noqa: F811
    app, url = server
    usenet_odds.odds_for(app.cfg).record("Film.2020.1080p-GRPA", True)
    monkeypatch.setattr(app, "_qbit", lambda: type("Q", (), {"torrents": lambda self: []})())
    app.cfg = dataclasses.replace(app.cfg, qbit_url="http://qbit.example")
    status, r = call(url + "/api/demand", *AUTH)
    assert status == 200 and r["usenet"][0]["group"] == "grpa" and r["never_after"] == usenet_odds.MIN_TRIES


class Autobrr:
    """Two filters feed nzb2seed, one does not."""
    ACTION_NAME = clients.Autobrr.ACTION_NAME
    ours = clients.Autobrr.ours
    exclude_group = clients.Autobrr.exclude_group

    data, patched = {}, []                  # autobrr's own state, across requests

    def __init__(self, url, key):
        pass

    def filters(self):
        return [{"id": i} for i in self.data]

    def filter(self, fid):
        return self.data[fid]

    def _call(self, method, path, json=None):
        assert method == "PATCH" and set(json) == {"id", "except_release_groups"}   # nothing else changed
        self.patched.append(json)
        self.data[json["id"]]["except_release_groups"] = json["except_release_groups"]


def test_a_group_never_on_usenet_is_excluded_in_autobrr_on_request(server, monkeypatch):  # noqa: F811
    app, url = server
    Autobrr.data = {1: {"id": 1, "actions": [{"name": "nzb2seed", "type": "WATCH_FOLDER"}], "except_release_groups": "OLD"},
                    2: {"id": 2, "actions": [{"name": "nzb2seed", "type": "WATCH_FOLDER"}], "except_release_groups": ""},
                    3: {"id": 3, "actions": [{"name": "qbit", "type": "QBITTORRENT"}], "except_release_groups": ""}}
    Autobrr.patched = []
    monkeypatch.setattr(gui, "Autobrr", Autobrr)
    app.cfg = dataclasses.replace(app.cfg, autobrr_url="http://autobrr.example", autobrr_key="k")
    status, r = call(url + "/api/auto/exclude_group", *AUTH, body={"group": "GRPB"})
    assert status == 200 and r["filters"] == 2
    assert [p["except_release_groups"] for p in Autobrr.patched] == ["OLD,GRPB", "GRPB"]
    assert Autobrr.data[3]["except_release_groups"] == ""        # a filter not feeding nzb2seed: untouched
    status, r = call(url + "/api/auto/exclude_group", *AUTH, body={"group": "grpb"})
    assert r["filters"] == 0                                      # already excluded: not added twice
    assert call(url + "/api/auto/exclude_group", *AUTH, body={"group": "a b;c"})[0] == 400
