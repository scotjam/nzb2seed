"""A .torrent that did not come from a Prowlarr search is named by the tracker it announces
to - never by its file name ("5ca7...1d37.torrent" is not a tracker)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from nzb2seed import pipeline  # noqa: E402


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
