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
    app.cfg = dataclasses.replace(app.cfg, auto_enabled=True, auto_folder=str(box), auto_retry_minutes=0.001,
                                  auto_wait_hours=1)
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
    wait(lambda: any(i["status"] == "done" for i in app.inbox.items()))
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
