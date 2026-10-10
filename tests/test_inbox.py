"""Automatic builds: autobrr's torrents in the inbox are built by themselves."""
import os
import threading
import time

from nzb2seed import clients, gui, inbox
from nzb2seed.pipeline import Abort

from helpers import make_torrent
from test_gui_login import call, server  # noqa: F401  (fixture)

AUTH = ("admin", "nzb2seed")


def wait(pred, timeout=10):
    end = time.time() + timeout
    while time.time() < end and not pred():
        time.sleep(0.05)
    assert pred()


def test_the_search_budget_only_limits_automatic_builds():
    b = inbox.Budget(lambda: 2)
    for _ in range(5):
        b()                                          # a manual search: never waits
    assert b.times == []
    inbox._auto.on = True
    try:
        b(); b()
        assert len(b.times) == 2
    finally:
        inbox._auto.on = False


def test_a_torrent_in_the_inbox_waits_for_its_post_then_builds(tmp_path, monkeypatch):
    cfgfile = tmp_path / "nzb2seed.toml"
    cfgfile.write_text("")
    app = gui.App(str(cfgfile))
    box = tmp_path / "inbox"
    box.mkdir()
    import dataclasses
    app.cfg = dataclasses.replace(app.cfg, auto_enabled=True, auto_folder=str(box), auto_retry_at=[i * 0.001 for i in range(1000)])
    calls = []

    def fake_run(cfg, opts, rel, groups, torrent_data=None):
        calls.append(opts)
        if len(calls) == 1:
            raise Abort("no Usenet post could supply S01E01")
        return {"result": "100.0% (seeding)"}
    monkeypatch.setattr(inbox, "execute_run", fake_run)
    monkeypatch.setattr(inbox, "POLL_SECONDS", 3600)      # the test drives poll() itself
    app.start_inbox()
    t = box / "Show.S01E01.720p-GRPA.torrent"
    t.write_bytes(make_torrent("Show.S01E01.720p-GRPA", {"a.mkv": b"x" * 40000}))
    old = time.time() - 60
    os.utime(t, (old, old))
    app.inbox.poll()
    # the item is marked done a moment before its file is moved, so wait for the move
    wait(lambda: any(i["status"] == "done" for i in app.inbox.items())
         and os.path.isdir(box / ".done") and os.listdir(box / ".done"))
    item = app.inbox.items()[0]
    assert item["attempts"] == 2 and len(calls) == 2 and all(o.unattended for o in calls)
    assert not t.exists() and len(os.listdir(box / ".done")) == 1
    assert calls[0].start is True                          # start only at 100% (finish decides)
    app.inbox.poll()                                       # the same torrent again: not rebuilt
    assert len(calls) == 2
    clients.SEARCH_GATE = None


def test_switched_off_the_inbox_is_left_alone(tmp_path):
    cfgfile = tmp_path / "nzb2seed.toml"
    cfgfile.write_text("")
    app = gui.App(str(cfgfile))
    box = tmp_path / "inbox"
    box.mkdir()
    (box / "x.torrent").write_bytes(b"d4:infod4:name1:xee")
    import dataclasses
    app.cfg = dataclasses.replace(app.cfg, auto_enabled=False, auto_folder=str(box))
    app.start_inbox()
    app.inbox.poll()
    assert os.listdir(box) == ["x.torrent"]
    clients.SEARCH_GATE = None


class FakeAutobrr:
    filters_ = [{"id": 1, "name": "TV", "enabled": False},          # disabled in autobrr
                {"id": 2, "name": "Movies", "enabled": True}]
    actions = {1: [{"id": 10, "name": "qbit", "type": "QBITTORRENT", "enabled": True},
                   {"id": 11, "name": "tell me", "type": "TEST", "enabled": True}],
               2: [{"id": 20, "name": "nzb2seed", "type": "WATCH_FOLDER", "watch_folder": "/config/nzb2seed-inbox"}]}
    log = []

    def __init__(self, url, key):
        pass

    def version(self):
        return "v1"

    def filters(self):
        return self.filters_

    def filter(self, fid):
        return {"id": fid, "indexers": [], "actions": self.actions[fid]}

    ACTION_NAME = clients.Autobrr.ACTION_NAME
    GRABBERS = clients.Autobrr.GRABBERS
    ours = clients.Autobrr.ours
    grabbers = clients.Autobrr.grabbers

    def toggle(self, aid):
        self.log.append(("toggle", aid))

    def set_enabled(self, fid, on):
        self.log.append(("filter", fid, on))
        for f in self.filters_:
            if f["id"] == fid:
                f["enabled"] = on

    def add_action(self, fid, folder):
        self.log.append(("add", fid, folder))
        self.actions.setdefault(fid, []).append(
            {"id": 90 + fid, "name": "nzb2seed", "type": "WATCH_FOLDER", "watch_folder": folder, "enabled": True})

    def delete_action(self, aid):
        self.log.append(("delete", aid))
        for acts in self.actions.values():
            acts[:] = [a for a in acts if a["id"] != aid]

    def set_folder(self, a, folder):
        self.log.append(("move", a["id"], folder))


def test_autobrr_filters_get_and_lose_only_the_nzb2seed_action(server, monkeypatch):  # noqa: F811
    app, url = server
    import dataclasses
    app.cfg = dataclasses.replace(app.cfg, auto_autobrr_folder="/config/nzb2seed-inbox",
                                  autobrr_url="http://autobrr.local:7474", autobrr_key="k")
    monkeypatch.setattr(gui, "Autobrr", FakeAutobrr)
    status, r = call(url + "/api/auto/filters", *AUTH, body={})
    assert status == 200 and [f["ours"] for f in r["filters"]] == [False, True]
    # a download client is shown by its own state line, not as an "other" action
    assert r["filters"][0]["actions"] == ["tell me"]
    assert r["filters"][0]["grabbers"] == [{"id": 10, "name": "qbit", "enabled": True}]
    status, r = call(url + "/api/auto/apply", *AUTH, body={"filters": [1]})     # TV on, Movies off
    # TV: our action added, its qBittorrent action switched off (it would download the same
    # release over BitTorrent) and the filter enabled; Movies: our action removed
    assert status == 200 and r == {"added": 1, "removed": 1, "fixed": 0, "off": 1, "back": 0,
                                   "on": 1, "stopped": 0}
    assert FakeAutobrr.log == [("add", 1, "/config/nzb2seed-inbox"), ("toggle", 10), ("filter", 1, True),
                               ("delete", 20)]
    FakeAutobrr.log.clear()
    FakeAutobrr.actions[1][0]["enabled"] = False                     # as autobrr has it now
    status, r = call(url + "/api/auto/apply", *AUTH, body={"filters": []})       # TV off again
    assert status == 200 and r["back"] == 1 and r["stopped"] == 1
    # only what nzb2seed changed: its own action gone, qbit back on, and the filter
    # itself switched off in autobrr - unticking here stops it there too
    assert FakeAutobrr.log == [("delete", 91), ("toggle", 10), ("filter", 1, False)]


def test_unticking_replace_leaves_the_filters_own_download_on(server, monkeypatch):  # noqa: F811
    """"nzb2seed dl replaces torrent dl" off: autobrr keeps downloading it itself too."""
    app, url = server
    import dataclasses
    app.cfg = dataclasses.replace(app.cfg, auto_autobrr_folder="/config/nzb2seed-inbox",
                                  autobrr_url="http://autobrr.local:7474", autobrr_key="k")
    monkeypatch.setattr(gui, "Autobrr", FakeAutobrr)
    FakeAutobrr.filters_ = [{"id": 1, "name": "TV", "enabled": True}]
    FakeAutobrr.actions = {1: [{"id": 10, "name": "qbit", "type": "QBITTORRENT", "enabled": True}]}
    FakeAutobrr.log = []

    status, r = call(url + "/api/auto/apply", *AUTH, body={"filters": [1], "replace": {"1": False}})
    assert status == 200 and r["off"] == 0 and r["added"] == 1
    assert FakeAutobrr.log == [("add", 1, "/config/nzb2seed-inbox")]          # qbit left alone

    FakeAutobrr.log = []
    call(url + "/api/auto/apply", *AUTH, body={"filters": [1], "replace": {"1": True}})
    assert ("toggle", 10) in FakeAutobrr.log                                  # now switched off
    FakeAutobrr.actions[1][0]["enabled"] = False
    FakeAutobrr.log = []
    status, r = call(url + "/api/auto/apply", *AUTH, body={"filters": [1], "replace": {"1": False}})
    assert r["back"] == 1 and FakeAutobrr.log == [("toggle", 10)]             # and back on again


def test_unticking_disables_the_filter_in_autobrr_even_if_you_had_it_on(server, monkeypatch):  # noqa: F811
    """Unticking a filter here switches it off in autobrr, so nothing acts on its releases -
    not nzb2seed, and not autobrr on its own. It is not put back the way it was."""
    app, url = server
    import dataclasses
    app.cfg = dataclasses.replace(app.cfg, auto_autobrr_folder="/config/nzb2seed-inbox",
                                  autobrr_url="http://autobrr.local:7474", autobrr_key="k")
    monkeypatch.setattr(gui, "Autobrr", FakeAutobrr)
    FakeAutobrr.filters_ = [{"id": 1, "name": "TV", "enabled": True}]     # you had it enabled
    FakeAutobrr.actions = {1: []}
    FakeAutobrr.log = []

    status, r = call(url + "/api/auto/apply", *AUTH, body={"filters": [1]})
    assert status == 200 and r["on"] == 0                    # already enabled: nothing to do
    assert FakeAutobrr.log == [("add", 1, "/config/nzb2seed-inbox")]

    FakeAutobrr.log = []
    status, r = call(url + "/api/auto/apply", *AUTH, body={"filters": []})
    assert status == 200 and r["stopped"] == 1
    assert ("filter", 1, False) in FakeAutobrr.log           # switched off, not left enabled
    assert FakeAutobrr.filters_[0]["enabled"] is False


def test_a_zero_day_preview_means_everything_not_the_saved_age(server, monkeypatch):  # noqa: F811
    """Asking the API for 0 days must not fall back to the configured 30."""
    app, url = server
    import dataclasses
    from nzb2seed import gui as gui_mod
    app.cfg = dataclasses.replace(app.cfg, retention_days=30, retention_enabled=False)
    monkeypatch.setattr(gui_mod.App, "_qbit", lambda self: None)
    seen = {}

    def fake_sweep(cfg, qb, log=print, dry_run=False, force=False, days=None):
        seen["days"] = days
        return {"removed": [], "kept": [], "bytes": 0, "dry_run": dry_run,
                "days": cfg.retention_days if days is None else days, "was_off": True}

    monkeypatch.setattr(gui_mod.retention_mod, "sweep", fake_sweep)
    status, r = call(url + "/api/retention/sweep", *AUTH, body={"dry_run": True, "days": 0})
    assert status == 200 and seen["days"] == 0.0 and r["days"] == 0

    status, r = call(url + "/api/retention/sweep", *AUTH, body={"dry_run": True})
    assert seen["days"] is None and r["days"] == 30      # no age given: the saved one


def test_it_looks_at_once_then_at_2_10_20_and_60_minutes(tmp_path):
    """Most posts turn up within minutes, some take half an hour or more: looks close
    together at first, a last one at an hour, then it gives up."""
    from nzb2seed.config import Config, finalize
    from nzb2seed.inbox import next_try, wait_seconds
    cfg = finalize(Config(path=tmp_path / "nzb2seed.toml"))
    assert cfg.auto_retry_at == [0, 2, 10, 20, 60] and wait_seconds(cfg) == 3600
    tries, at = [0.0], 0.0
    while (nxt := next_try(cfg, 0.0, at)) is not None:
        at = nxt
        tries.append(at / 60)
    assert tries == [0, 2, 10, 20, 60]


def test_a_build_that_ran_long_takes_the_next_look_still_to_come(tmp_path):
    """The looks are timed from when the torrent arrived: a try that ran past a look's
    time goes on to the next one, never two at once."""
    from nzb2seed.config import Config, finalize
    from nzb2seed.inbox import next_try
    cfg = finalize(Config(path=tmp_path / "nzb2seed.toml"))
    assert next_try(cfg, 0.0, 11 * 60) == 20 * 60
    assert next_try(cfg, 0.0, 61 * 60) is None


def test_the_looks_can_be_written_any_way(tmp_path):
    from nzb2seed.config import minutes_list
    assert minutes_list("60, 2 10;20 0 2") == [0, 2, 10, 20, 60]
    assert minutes_list([5, "x", -1, 1.5]) == [1.5, 5]
    assert minutes_list("") == []


def waiting_inbox(tmp_path, monkeypatch, names=("Show.S01E01.720p-GRPA",), building=None):
    """An inbox whose torrents are all waiting for their post (next try an hour off);
    ``building`` names one that is mid-build instead, held until the event is set."""
    import dataclasses
    cfgfile = tmp_path / "nzb2seed.toml"
    cfgfile.write_text("")
    app = gui.App(str(cfgfile))
    box = tmp_path / "inbox"
    box.mkdir()
    app.cfg = dataclasses.replace(app.cfg, auto_enabled=True, auto_folder=str(box), auto_parallel=2,
                                  auto_retry_at=list(range(0, 48 * 60 + 1, 60)))
    hold = threading.Event()

    def fake_run(cfg, opts, rel, groups, torrent_data=None):
        if building and building in rel.title:
            hold.wait(10)
            return {"result": "done"}
        raise Abort("no Usenet post could supply S01E01")
    monkeypatch.setattr(inbox, "execute_run", fake_run)
    monkeypatch.setattr(inbox, "POLL_SECONDS", 3600)
    app.start_inbox()
    for n in names:
        t = box / f"{n}.torrent"
        t.write_bytes(make_torrent(n, {"a.mkv": n.encode() * 4000}))
        old = time.time() - 60
        os.utime(t, (old, old))
    app.inbox.poll()
    want = {n: ("building" if n == building else "waiting") for n in names}
    wait(lambda: {i["name"]: i["status"] for i in app.inbox.items()} == want)
    return app, box, hold


def test_a_torrent_not_built_within_a_day_is_stopped_not_removed(tmp_path, monkeypatch):
    app, box, hold = waiting_inbox(tmp_path, monkeypatch)
    try:
        h = app.inbox.items()[0]["infohash"]
        app.inbox.state.update(h, first_seen=time.time() - 25 * 3600)
        app.inbox.poll()
        it = app.inbox.items()[0]
        assert it["status"] == "failed" and "24 hours" in it["why"]       # stopped, still listed
        wait(lambda: h not in app.inbox.running)                          # its idle job ended
        assert os.listdir(box / ".failed")                                # where Try again looks
        app.inbox.retry(h)                                                # and it can be
        assert app.inbox.state.items[h]["status"] in ("queued", "waiting", "building")
    finally:
        hold.set()
        clients.SEARCH_GATE = None


def test_a_younger_torrent_is_left_waiting(tmp_path, monkeypatch):
    app, box, hold = waiting_inbox(tmp_path, monkeypatch)
    try:
        h = app.inbox.items()[0]["infohash"]
        app.inbox.state.update(h, first_seen=time.time() - 23 * 3600)
        app.inbox.poll()
        assert app.inbox.items()[0]["status"] == "waiting"
    finally:
        hold.set()
        clients.SEARCH_GATE = None


def test_clear_queue_stops_what_waits_and_leaves_builds_alone(server, tmp_path, monkeypatch):  # noqa: F811
    app, box, hold = waiting_inbox(tmp_path, monkeypatch,
                                   names=("Show.S01E01.720p-GRPA", "Show.S01E02.720p-GRPA"),
                                   building="Show.S01E02.720p-GRPA")
    try:
        assert app.inbox.stop_pending() == 1
        st = {i["name"]: i for i in app.inbox.items()}
        assert st["Show.S01E01.720p-GRPA"]["status"] == "failed"
        assert "cleared" in st["Show.S01E01.720p-GRPA"]["why"]
        assert st["Show.S01E02.720p-GRPA"]["status"] == "building"        # carries on
        hold.set()
        wait(lambda: {i["name"]: i["status"] for i in app.inbox.items()}["Show.S01E02.720p-GRPA"] == "done")
    finally:
        hold.set()
        clients.SEARCH_GATE = None


def full_queue(tmp_path, monkeypatch, keep_older):
    """Cap 1: the first torrent is queued, then a second arrives an hour later."""
    import dataclasses
    cfgfile = tmp_path / "nzb2seed.toml"
    cfgfile.write_text("")
    app = gui.App(str(cfgfile))
    box = tmp_path / "inbox"
    box.mkdir()
    app.cfg = dataclasses.replace(app.cfg, auto_enabled=True, auto_folder=str(box), auto_queue_max=1,
                                  auto_queue_keep_older=keep_older, auto_retry_at=list(range(0, 48 * 60 + 1, 60)))
    monkeypatch.setattr(inbox, "execute_run",
                        lambda *a, **k: (_ for _ in ()).throw(Abort("no Usenet post could supply S01E01")))
    monkeypatch.setattr(inbox, "POLL_SECONDS", 3600)
    app.start_inbox()
    for i, n in enumerate(("Show.S01E01.720p-GRPA", "Show.S01E02.720p-GRPA")):
        t = box / f"{n}.torrent"
        t.write_bytes(make_torrent(n, {"a.mkv": n.encode() * 4000}))
        old = time.time() - 60
        os.utime(t, (old, old))
        app.inbox.poll()
        if i == 0:
            wait(lambda: app.inbox.items()[0]["status"] == "waiting")
            h = app.inbox.items()[0]["infohash"]
            app.inbox.state.update(h, first_seen=time.time() - 3600)
    return app, box, {i["name"]: i for i in app.inbox.items()}


def test_a_full_queue_makes_room_for_the_new_arrival(tmp_path, monkeypatch):
    """By default a fresh release wins: the oldest waiting one is stopped for it."""
    try:
        app, box, st = full_queue(tmp_path, monkeypatch, keep_older=False)
        old, new = st["Show.S01E01.720p-GRPA"], st["Show.S01E02.720p-GRPA"]
        assert old["status"] == "failed" and "made room" in old["why"]
        assert new["status"] in ("queued", "waiting", "building")
        assert os.listdir(box / ".failed")                     # there for Try again
    finally:
        clients.SEARCH_GATE = None


def test_keeping_older_ones_turns_the_new_arrival_away(tmp_path, monkeypatch):
    try:
        app, box, st = full_queue(tmp_path, monkeypatch, keep_older=True)
        old, new = st["Show.S01E01.720p-GRPA"], st["Show.S01E02.720p-GRPA"]
        assert old["status"] in ("queued", "waiting", "building")
        assert new["status"] == "failed" and "not queued" in new["why"]
        assert os.listdir(box / ".failed")
    finally:
        clients.SEARCH_GATE = None


def test_under_the_cap_everything_is_queued(tmp_path, monkeypatch):
    app, box, hold = waiting_inbox(tmp_path, monkeypatch,
                                   names=("Show.S01E01.720p-GRPA", "Show.S01E02.720p-GRPA"))
    hold.set()
    clients.SEARCH_GATE = None


def test_after_a_restart_the_same_job_carries_on(tmp_path, monkeypatch):
    """A queued automatic build cut off by a restart comes back as the same job - not an
    "interrupted" entry left behind next to a new one - and its log says it is resuming."""
    import dataclasses
    cfgfile = tmp_path / "nzb2seed.toml"
    cfgfile.write_text("")
    box = tmp_path / "inbox"
    box.mkdir()
    monkeypatch.setattr(inbox, "execute_run",
                        lambda *a, **k: (_ for _ in ()).throw(Abort("no Usenet post could supply S01E01")))
    monkeypatch.setattr(inbox, "POLL_SECONDS", 3600)

    def boot():
        app = gui.App(str(cfgfile))
        app.cfg = dataclasses.replace(app.cfg, auto_enabled=True, auto_folder=str(box),
                                      auto_retry_at=list(range(0, 48 * 60 + 1, 60)))
        app.start_inbox()
        return app
    try:
        app = boot()
        t = box / "Show.S01E01.720p-GRPA.torrent"
        t.write_bytes(make_torrent("Show.S01E01.720p-GRPA", {"a.mkv": b"x" * 40000}))
        old = time.time() - 60
        os.utime(t, (old, old))
        app.inbox.poll()
        wait(lambda: app.inbox.items()[0]["status"] == "waiting")
        first = app.inbox.items()[0]["job"]
        app.store.flush()
        app.inbox.stop.set()

        again = boot()                                  # the restart
        assert again.jobs[first].status == "interrupted"
        again.inbox.poll()
        wait(lambda: again.jobs[first].status == "running")
        assert again.inbox.items()[0]["job"] == first
        assert [j.id for j in again.jobs.values() if j.kind == "auto"] == [first]   # no new entry
        texts = [line["t"] for line in again.jobs[first].lines]
        assert gui.RESUMING in texts and gui.INTERRUPTED not in texts
    finally:
        clients.SEARCH_GATE = None


def test_try_again_on_the_jobs_tab_works_for_automatic_jobs(server, tmp_path, monkeypatch):  # noqa: F811
    import dataclasses
    app, url = server
    box = tmp_path / "inbox"
    box.mkdir()
    app.cfg = dataclasses.replace(app.cfg, auto_enabled=True, auto_folder=str(box),
                                  auto_retry_at=list(range(0, 48 * 60 + 1, 60)))
    monkeypatch.setattr(inbox, "execute_run",
                        lambda *a, **k: (_ for _ in ()).throw(Abort("no Usenet post could supply S01E01")))
    monkeypatch.setattr(inbox, "POLL_SECONDS", 3600)
    app.start_inbox()
    try:
        t = box / "Show.S01E01.720p-GRPA.torrent"
        t.write_bytes(make_torrent("Show.S01E01.720p-GRPA", {"a.mkv": b"x" * 40000}))
        old = time.time() - 60
        os.utime(t, (old, old))
        app.inbox.poll()
        wait(lambda: app.inbox.items()[0]["status"] == "waiting")
        h, first = app.inbox.items()[0]["infohash"], app.inbox.items()[0]["job"]
        app.inbox.state.update(h, first_seen=time.time() - 25 * 3600)
        app.inbox.poll()                                                   # the 24-hour stop
        wait(lambda: app.jobs[first].status != "running")
        row = [j for j in call(url + "/api/jobs", *AUTH)[1] if j["id"] == first][0]
        assert row["can_retry"] is True
        status, r = call(url + "/api/jobs/retry", *AUTH, body={"id": first})
        assert status == 200 and r["id"] != first
        assert app.inbox.state.items[h]["job"] == r["id"]                  # back in the queue
        assert app.inbox.state.items[h]["status"] in ("queued", "waiting", "building")
        assert call(url + "/api/jobs/retry", *AUTH, body={"id": first})[0] == 409
        row = [j for j in call(url + "/api/jobs", *AUTH)[1] if j["id"] == first][0]
        assert row["can_retry"] is False and row["retried_as"] == r["id"]
    finally:
        clients.SEARCH_GATE = None


def test_an_old_release_missing_from_usenet_is_tried_once(tmp_path, monkeypatch):
    """Posted to the tracker 35 days ago and not on Usenet: a few more minutes will not
    change that, so there are no quick retries (each one costs an indexer search)."""
    import dataclasses
    from types import SimpleNamespace
    cfgfile = tmp_path / "nzb2seed.toml"
    cfgfile.write_text("")
    app = gui.App(str(cfgfile))
    box = tmp_path / "inbox"
    box.mkdir()
    app.cfg = dataclasses.replace(app.cfg, auto_enabled=True, auto_folder=str(box),
                                  auto_retry_at=[i * 0.001 for i in range(1000)],
                                  auto_max_age_days=100)      # an old release on purpose, within this limit
    calls = []
    monkeypatch.setattr(inbox, "execute_run",
                        lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(Abort("no Usenet post could supply X")))
    monkeypatch.setattr(inbox.lookup_mod, "find", lambda *a, **k: SimpleNamespace(
        published=time.time() - 35 * 86400, seeders=11, leechers=0, grabs=5, age_min=35 * 1440))
    monkeypatch.setattr(inbox, "POLL_SECONDS", 3600)
    app.start_inbox()
    try:
        t = box / "Film.2003.1080p.BluRay-GRP.torrent"
        t.write_bytes(make_torrent("Film.2003.1080p.BluRay-GRP", {"a.mkv": b"x" * 40000}))
        old = time.time() - 60
        os.utime(t, (old, old))
        app.inbox.poll()
        wait(lambda: app.inbox.items()[0]["status"] == "failed")
        time.sleep(0.3)
        assert len(calls) == 1                                  # one search, no retries
    finally:
        clients.SEARCH_GATE = None


def test_a_stopped_automatic_torrents_downloads_are_kept_only_while_its_job_is_listed(tmp_path, monkeypatch):
    """Taking the job off the Jobs list - or abandoning it - says you are done with its
    downloads, whatever the Automatic tab could still try again."""
    app, box, hold = waiting_inbox(tmp_path, monkeypatch)
    try:
        h = app.inbox.items()[0]["infohash"]
        app.inbox.stop_one(h, "stopped: the queue was cleared")
        wait(lambda: h not in app.inbox.running)           # its job has wound down
        job = app.jobs[app.inbox.state.items[h]["job"]]
        job.infohash = h
        assert h in app.downloads_in_use()[0]              # its job is listed and could retry
        job.extra = {"abandoned": True}
        assert h not in app.downloads_in_use()[0]          # abandoned: done with
        job.extra = None
        app.jobs.clear()                                   # its job taken off the list
        keep, live, _ = app.downloads_in_use()
        assert h not in keep | live                        # done with, though the tab can retry
    finally:
        hold.set()
        clients.SEARCH_GATE = None


def test_an_automatic_build_that_settled_can_be_tried_with_whole_posts(server, tmp_path, monkeypatch):  # noqa: F811
    """Not only Build-tab builds: an automatic one goes back into the Automatic tab's queue,
    and that run tries whole posts - a plain Try again after it settles again."""
    import dataclasses
    from nzb2seed import pipeline
    app, url = server
    box = tmp_path / "inbox"
    box.mkdir()
    app.cfg = dataclasses.replace(app.cfg, auto_enabled=True, auto_folder=str(box), auto_retry_at=list(range(0, 48 * 60 + 1, 60)))
    runs = []

    def fake_run(cfg, opts, rel, groups, torrent_data=None):
        runs.append(opts.whole_posts)
        pipeline._this_build.settled = [{"label": "S01E03", "missing": 5_000_000, "post": 1_400_000_000}]
        raise pipeline.Incomplete("short", 0.999, "a" * 40, "/t", "/d", in_client=False)
    monkeypatch.setattr(inbox, "execute_run", fake_run)
    monkeypatch.setattr(inbox, "POLL_SECONDS", 3600)
    app.start_inbox()
    t = box / "Show.S01.720p-GRPA.torrent"
    t.write_bytes(make_torrent("Show.S01.720p-GRPA", {"a.mkv": b"x" * 40000}))
    old = time.time() - 60
    os.utime(t, (old, old))
    try:
        app.inbox.poll()
        wait(lambda: runs and not app.inbox.running)
        first = next(j for j in app.jobs.values() if j.kind == "auto")
        wait(lambda: first.status == "failed")
        assert first.extra["settled"][0]["label"] == "S01E03"
        status, r = call(url + "/api/jobs/whole_posts", *AUTH, body={"id": first.id})
        assert status == 200 and first.retried_as == r["id"]
        wait(lambda: len(runs) == 2 and not app.inbox.running)
        assert runs == [False, True]
        again = app.jobs[r["id"]]
        wait(lambda: again.status == "failed")
        h = app.inbox.items()[0]["infohash"]
        app.inbox.retry(h)                                  # a plain Try again
        wait(lambda: len(runs) == 3)
        assert runs[-1] is False
    finally:
        clients.SEARCH_GATE = None


def _aged_inbox(tmp_path, monkeypatch, published, **cfg):
    import dataclasses
    from nzb2seed import lookup
    cfgfile = tmp_path / "nzb2seed.toml"
    cfgfile.write_text("")
    app = gui.App(str(cfgfile))
    box = tmp_path / "inbox"
    box.mkdir()
    app.cfg = dataclasses.replace(app.cfg, auto_enabled=True, auto_folder=str(box), auto_retry_at=list(range(0, 48 * 60 + 1, 60)), **cfg)
    searched, built = [], []
    monkeypatch.setattr(inbox.lookup_mod, "find", lambda *a, **k: searched.append(1) or (
        lookup.Info(published=published) if published is not None else None))
    monkeypatch.setattr(inbox, "execute_run", lambda *a, **k: built.append(1) or {"result": "done"})
    monkeypatch.setattr(inbox, "POLL_SECONDS", 3600)
    app.start_inbox()
    t = box / "Film.2019.1080p.BluRay-GRPA.torrent"
    t.write_bytes(make_torrent("Film.2019.1080p.BluRay-GRPA", {"a.mkv": b"x" * 40000}))
    old = time.time() - 60
    os.utime(t, (old, old))
    app.inbox.poll()
    return app, searched, built


def test_an_automatic_build_skips_what_the_tracker_posted_long_ago(tmp_path, monkeypatch):
    app, searched, built = _aged_inbox(tmp_path, monkeypatch, time.time() - 782 * 86400)
    try:
        wait(lambda: app.inbox.items()[0]["status"] == "skipped")
        why = app.inbox.items()[0]["why"]
        assert "782 days ago" in why and "2-day limit" in why and built == []
        job = next(j for j in app.jobs.values() if j.kind == "auto")
        wait(lambda: job.status == "done")
        assert job.result.startswith("skipped - ")              # a grey dot on the Jobs tab
    finally:
        clients.SEARCH_GATE = None


def test_a_new_one_or_one_of_unknown_age_is_built(tmp_path, monkeypatch):
    for published in (time.time() - 3600, None):                  # an hour old; age not found
        sub = tmp_path / ("new" if published else "unknown")
        sub.mkdir()
        app, searched, built = _aged_inbox(sub, monkeypatch, published)
        try:
            wait(lambda: built)
            assert searched == [1]
        finally:
            clients.SEARCH_GATE = None


def test_a_release_the_rules_turn_down_costs_no_search(tmp_path, monkeypatch):
    """The age limit asks Prowlarr - but only after what costs nothing has passed."""
    block = [{"enabled": True, "name": "no films", "types": ["movie"]}]
    app, searched, built = _aged_inbox(tmp_path, monkeypatch, time.time() - 3600, demand_block=block)
    try:
        wait(lambda: app.inbox.items()[0]["status"] == "skipped")
        assert searched == [] and built == []
    finally:
        clients.SEARCH_GATE = None


def test_a_rule_asks_the_tracker_only_once_its_free_conditions_hold(tmp_path, monkeypatch):
    """A rule wanting 15 GB and an age: a small release never matches it, so no search is
    spent on it - and a release the free conditions let through is asked about once."""
    rules = [{"enabled": True, "name": "fresh and big", "min_gb": 15, "max_age_min": 30}]
    app, searched, built = _aged_inbox(tmp_path, monkeypatch, time.time() - 3600, demand_rules=rules,
                                       auto_max_age_days=0)
    try:
        wait(lambda: built)
        assert searched == []                  # 40 KB: never 15 GB, so its age was never asked
    finally:
        clients.SEARCH_GATE = None


def test_the_rules_and_the_age_limit_share_one_search(tmp_path, monkeypatch):
    rules = [{"enabled": True, "name": "fresh", "max_age_min": 30}]
    app, searched, built = _aged_inbox(tmp_path, monkeypatch, time.time() - 3600, demand_rules=rules)
    try:
        wait(lambda: built)
        assert searched == [1]                 # asked for the rule; the age limit reused it
    finally:
        clients.SEARCH_GATE = None
