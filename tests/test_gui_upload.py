"""A .torrent uploaded on the Build tab is paired and built like a search result."""
import base64
import threading

from nzb2seed import gui
from nzb2seed.clients import Release

from helpers import make_torrent
from test_gui_login import call, server  # noqa: F401  (fixture)

AUTH = ("admin", "nzb2seed")


def test_upload_becomes_a_buildable_result(server, monkeypatch):  # noqa: F811
    app, url = server
    data = make_torrent("Show.2016.S01E01.720p.HDTV.x264-GRPA",
                        {"a.mkv": b"x" * 40000, "a.nfo": b"hello\r\n"})
    status, r = call(url + "/api/upload_torrent", *AUTH,
                     body={"torrent_b64": base64.b64encode(data).decode(), "torrent_name": "mine.torrent"})
    assert status == 200
    t = r["torrent"]
    assert t["title"] == "Show.2016.S01E01.720p.HDTV.x264-GRPA" and t["size"] == 40007
    assert t["guid"].startswith("upload:") and t["indexer"] == "mine.torrent" and t["files"] == 2

    seen = {}
    gate = threading.Event()

    def fake_run(cfg, opts, tor_rel, groups, torrent_data=None):
        seen.update(rel=tor_rel, data=torrent_data)
        gate.set()
        return {"result": "ok"}
    monkeypatch.setattr(gui, "execute_run", fake_run)
    status, r = call(url + "/api/build", *AUTH, body={"torrent": t, "nzbs": [], "options": {}})
    assert status == 200 and gate.wait(5)
    assert seen["data"] == data and seen["rel"].title == t["title"]


def test_junk_and_forged_uploads_are_refused(server):  # noqa: F811
    app, url = server
    status, _ = call(url + "/api/upload_torrent", *AUTH,
                     body={"torrent_b64": base64.b64encode(b"not a torrent").decode(), "torrent_name": "x.torrent"})
    assert status == 400
    forged = Release("X", "torrent", "x", 0, 1, "upload:../../etc/passwd", "", "", "", None, None, None)
    try:
        gui.uploaded_data(app.cfg, forged)
        raise AssertionError("accepted a forged path")
    except ValueError:
        pass
