"""A release built already for one tracker, found on another: the Build tab offers to
cross-seed it there - cross-seed's announce API adds that tracker's torrent onto the files
already seeding, so nothing is built or downloaded again."""
import base64
import dataclasses
import urllib.request
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest  # noqa: E402

from nzb2seed import clients, config as config_mod  # noqa: E402
from helpers import make_torrent  # noqa: E402
from test_gui_login import call, server  # noqa: E402, F401  (fixture)

AUTH = ("admin", "nzb2seed")
H = "b" * 40
NAME = "Some.Show.S01.1080p.WEB-DL.H.264-GrpA"


def _torrent(indexer="TrackerB (API)", title=NAME):
    return {"title": title, "protocol": "torrent", "indexer": indexer, "indexer_id": 2, "size": 5000,
            "guid": f"https://prowlarr.example/{indexer}", "download_url": "http://prowlarr.example/2/download?x",
            "info_url": "", "publish_date": "", "grabs": 0, "seeders": 5, "files": 2}


class FakeQbit:
    def __init__(self, progress):
        self.progress = progress

    def info(self, h):
        return {"progress": self.progress} if h == H else None


def _built(app, tracker="TrackerA (API)", progress=1.0, monkeypatch=None):
    job = app.start_job(NAME, "build", lambda cfg: {"result": "done"})
    end = time.time() + 5
    while job.status != "done" and time.time() < end:
        time.sleep(0.05)
    job.infohash, job.tracker = H, tracker
    monkeypatch.setattr(app, "_qbit", lambda: FakeQbit(progress))
    app.cfg = dataclasses.replace(app.cfg, cross_seed_url="http://cross-seed.example:2468", cross_seed_key="k")
    return job


def test_cross_seeds_settings_are_saved_with_the_rest(tmp_path):
    cfg = config_mod.from_sections({"cross_seed": {"url": "http://cs.example:2468", "api_key": "abc"}},
                                   str(tmp_path / "c.toml"))
    config_mod.save(cfg)
    back = config_mod.load(str(tmp_path / "c.toml"))
    assert (back.cross_seed_url, back.cross_seed_key) == ("http://cs.example:2468", "abc")


def test_a_release_built_for_another_tracker_is_offered_for_cross_seeding(server, monkeypatch):  # noqa: F811
    app, url = server
    job = _built(app, monkeypatch=monkeypatch)
    status, r = call(url + "/api/cross_seed/offer", *AUTH, body={"torrent": _torrent()})
    assert status == 200 and r["offer"] == {"job": job.id, "tracker": "TrackerA (API)", "infohash": H,
                                           "for_tracker": "TrackerB (API)"}


@pytest.mark.parametrize("what,offered", [
    ("same tracker", None),                 # built for this tracker: nothing to offer
    ("other release", None),
    ("no cross-seed", None),                # not set up in Settings
    ("not seeding", "why"),                 # built, but not at 100% in qBittorrent: said why
])
def test_nothing_is_offered_unless_it_is_seeding_for_another_tracker(server, monkeypatch, what, offered):  # noqa: F811
    app, url = server
    _built(app, progress=0.5 if what == "not seeding" else 1.0, monkeypatch=monkeypatch)
    t = _torrent(indexer="TrackerA" if what == "same tracker" else "TrackerB (API)",
                 title="Some.Show.S01.720p.WEB-DL.H.264-GrpA" if what == "other release" else NAME)
    if what == "no cross-seed":
        app.cfg = dataclasses.replace(app.cfg, cross_seed_url="")
    _, r = call(url + "/api/cross_seed/offer", *AUTH, body={"torrent": t})
    assert (r["offer"] if offered is None else set(r["offer"])) == ({"why"} if offered else None)


@pytest.mark.parametrize("code,said", [(200, "added"), (202, "incomplete"), (204, "no match")])
def test_the_torrent_is_announced_to_cross_seed_with_prowlarrs_link(server, monkeypatch, code, said):  # noqa: F811
    app, url = server
    _built(app, monkeypatch=monkeypatch)
    sent = []

    class Answer:
        status_code, text = code, ""

    def post(u, **kw):
        sent.append((u, kw["headers"], kw["json"]))
        return Answer()
    monkeypatch.setattr(clients.requests, "post", post)
    status, r = call(url + "/api/cross_seed/add", *AUTH, body={"torrent": _torrent()})
    assert status == 200 and r["result"] == said
    u, headers, body = sent[0]
    assert u == "http://cross-seed.example:2468/api/announce" and headers == {"X-Api-Key": "k"}
    assert body == {"name": NAME, "guid": "http://prowlarr.example/2/download?x",
                    "link": "http://prowlarr.example/2/download?x", "tracker": "TrackerB (API)"}


def test_nothing_is_announced_without_an_earlier_build_seeding(server, monkeypatch):  # noqa: F811
    app, url = server
    _built(app, progress=0.5, monkeypatch=monkeypatch)
    monkeypatch.setattr(clients.requests, "post", lambda *a, **k: pytest.fail("announced"))
    status, _ = call(url + "/api/cross_seed/add", *AUTH, body={"torrent": _torrent()})
    assert status == 409                    # (the helper drops an error's text: only the code is checked)


def _uploaded(url):
    data = make_torrent(NAME, {"a.mkv": b"x" * 40000})          # announces to tracker.example
    _, r = call(url + "/api/upload_torrent", *AUTH,
                body={"torrent_b64": base64.b64encode(data).decode(), "torrent_name": "mine.torrent"})
    return r["torrent"], data


def test_an_uploaded_torrent_is_offered_by_the_tracker_it_announces_to(server, monkeypatch):  # noqa: F811
    app, url = server
    job = _built(app, monkeypatch=monkeypatch)
    t, _ = _uploaded(url)
    _, r = call(url + "/api/cross_seed/offer", *AUTH, body={"torrent": t})
    assert r["offer"] == {"job": job.id, "tracker": "TrackerA (API)", "infohash": H, "for_tracker": "tracker.example"}


def test_an_uploaded_torrent_built_for_its_own_tracker_is_not_offered(server, monkeypatch):  # noqa: F811
    app, url = server
    _built(app, tracker="tracker.example", monkeypatch=monkeypatch)
    t, _ = _uploaded(url)
    _, r = call(url + "/api/cross_seed/offer", *AUTH, body={"torrent": t})
    assert r["offer"] is None


def test_cross_seed_fetches_an_uploaded_torrent_from_a_one_off_link(server, monkeypatch):  # noqa: F811
    app, url = server
    _built(app, monkeypatch=monkeypatch)
    t, data = _uploaded(url)
    sent = []

    class Answer:
        status_code, text = 200, ""

    def post(u, **kw):
        link = kw["json"]["link"]
        with urllib.request.urlopen(link) as got:                # as cross-seed would, no login
            sent.append((kw["json"], got.read()))
        return Answer()
    monkeypatch.setattr(clients.requests, "post", post)
    status, r = call(url + "/api/cross_seed/add", *AUTH, body={"torrent": t})
    assert status == 200 and r["result"] == "added"
    body, fetched = sent[0]
    assert fetched == data and body["tracker"] == "tracker.example" and body["name"] == NAME
    assert body["link"].startswith(url + "/api/cross_seed/torrent/") and body["guid"] == body["link"]
    try:                                                         # a made-up token gets nothing
        urllib.request.urlopen(url + "/api/cross_seed/torrent/madeup.torrent")
        raise AssertionError("served without a good token")
    except urllib.error.HTTPError as e:
        assert e.code == 404
