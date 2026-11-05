"""The folder chooser: any folder on the machine, starting at /, with the configured data
folders offered as shortcuts."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from test_gui_login import call, server  # noqa: E402, F401  (fixture)

AUTH = ("admin", "nzb2seed")


def test_it_starts_at_the_top_and_goes_anywhere(server, tmp_path):  # noqa: F811
    app, url = server
    status, top = call(url + "/api/folders", *AUTH, body={"path": ""})
    assert status == 200 and top["path"] == os.path.realpath(os.path.abspath(os.sep)) and top["parent"] == ""
    (tmp_path / "media" / "tv").mkdir(parents=True)
    (tmp_path / "media" / ".hidden").mkdir()
    status, r = call(url + "/api/folders", *AUTH, body={"path": str(tmp_path / "media")})
    assert status == 200 and [d["name"] for d in r["dirs"]] == ["tv"]
    assert r["parent"] == os.path.realpath(str(tmp_path))


def test_a_path_that_is_not_a_folder_is_refused(server, tmp_path):  # noqa: F811
    app, url = server
    status, _ = call(url + "/api/folders", *AUTH, body={"path": str(tmp_path / "nope")})
    assert status == 400


def test_it_needs_the_login(server):  # noqa: F811
    app, url = server
    status, _ = call(url + "/api/folders", "admin", "wrong", body={"path": ""})
    assert status == 401


def test_limited_to_one_drive_it_starts_at_its_top_and_never_leaves_it(server, tmp_path):  # noqa: F811
    from nzb2seed import gui
    app, url = server
    lib = tmp_path / "tv"
    lib.mkdir()
    drive = gui.mount_point(str(lib))
    status, r = call(url + "/api/folders", *AUTH, body={"path": "", "within": str(lib)})
    assert status == 200 and r["path"] == drive and r["drive"] == drive and r["parent"] == ""
    status, r = call(url + "/api/folders", *AUTH, body={"path": str(tmp_path), "within": str(lib)})
    assert status == 200 and "tv" in [d["name"] for d in r["dirs"]]


def test_library_folders_must_share_the_first_ones_drive(server, tmp_path, monkeypatch):  # noqa: F811
    """Hard links do not cross drives - checked when saving, too."""
    app, url = server
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    real_stat = os.stat

    class Other:
        def __init__(self, st): self.st = st
        def __getattr__(self, k): return 99 if k == "st_dev" else getattr(self.st, k)
    monkeypatch.setattr(os, "stat", lambda p, *k, **kw: Other(real_stat(p, *k, **kw)) if str(p) == str(b) else real_stat(p, *k, **kw))
    status, r = call(url + "/api/library/save", *AUTH, body={"enabled": True, "indexer": "TL", "roots": [str(a), str(b)],
                                                             "output_dir": str(a)})
    assert status == 400                                    # "... has to be on the same drive"


def test_several_library_trackers_are_saved_in_order_and_kept_in_bounds(server, tmp_path):  # noqa: F811
    from nzb2seed import config as config_mod, library as lib
    app, url = server
    status, r = call(url + "/api/library/save", *AUTH, body={"multi": True, "order": [
        "TrackerB", lib.OWN, "  TrackerA  ", "TrackerB", 7, "x" * 500] + [f"T{i}" for i in range(200)]})
    assert status == 200 and r["settings"]["multi"] is True
    order = r["settings"]["order"]
    assert order[:4] == ["TrackerB", lib.OWN, "TrackerA", "x" * config_mod.MAX_TRACKER_NAME]
    assert len(order) <= lib.MAX_TRACKERS + 1
    assert config_mod.load(app.cfg.path).library_order == order      # in the config file, as shown
    status, _ = call(url + "/api/library/save", *AUTH, body={"enabled": True, "multi": True, "order": [lib.OWN]})
    assert status == 400                                    # "tick a tracker first"
