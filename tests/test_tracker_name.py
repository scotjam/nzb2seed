"""A .torrent that did not come from a Prowlarr search is named by the tracker it announces
to - never by its file name ("5ca7...1d37.torrent" is not a tracker)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from nzb2seed import gui, pipeline  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
from test_gui_login import server  # noqa: E402, F401  (fixture)


class Prowlarr:
    def __init__(self):
        self.asked = 0

    def indexer_sites(self):
        self.asked += 1
        return [("TrackerOne", ["https://www.trackerone.example/"]), ("Other (API)", ["https://other.example/"])]


def setup_function():
    pipeline._sites_cache.update(at=0.0, sites=[])


def test_the_tracker_is_named_as_prowlarr_names_it():
    assert pipeline.tracker_name(["https://tracker.trackerone.example/a/secret/announce"], Prowlarr()) == "TrackerOne"
    assert pipeline.tracker_name(["https://other.example:2710/announce"], Prowlarr()) == "Other (API)"


def test_one_prowlarr_has_not_heard_of_is_named_by_its_site():
    assert pipeline.tracker_name(["https://announce.unknown.example/x"], Prowlarr()) == "unknown.example"
    assert pipeline.tracker_name(["https://announce.site.co.uk/x"], None) == "site.co.uk"


def test_no_announce_no_name():
    assert pipeline.tracker_name([], Prowlarr()) == ""


def test_prowlarr_is_asked_once_an_hour_at_most():
    p = Prowlarr()
    for _ in range(3):
        pipeline.tracker_name(["https://tracker.trackerone.example/announce"], p)
    assert p.asked == 1


def test_the_settings_list_prowlarrs_trackers(server, monkeypatch):  # noqa: F811
    from nzb2seed.clients import ApiError
    from test_gui_login import call
    _, url = server

    class Pr:
        def __init__(self, *a):
            pass

        def indexer_sites(self):
            return [("TrackerB (API)", ["https://b.example/"]), ("trackera", []), ("TrackerB (API)", [])]
    monkeypatch.setattr(gui, "Prowlarr", Pr)
    status, r = call(url + "/api/trackers", "admin", "nzb2seed")
    assert status == 200 and r == {"trackers": ["trackera", "TrackerB (API)"]}

    def down(self):
        raise ApiError("Prowlarr indexer list: HTTP 500")
    monkeypatch.setattr(Pr, "indexer_sites", down)
    status, r = call(url + "/api/trackers", "admin", "nzb2seed")
    assert status == 200 and r["trackers"] == [] and "HTTP 500" in r["error"]
