"""The Build tab can search only some trackers; Usenet is searched for the NZBs either way."""
from types import SimpleNamespace

import pytest

from nzb2seed import pipeline


def rel(title, protocol, indexer):
    return SimpleNamespace(title=title, protocol=protocol, indexer=indexer)


class FakePr:
    calls = []

    def __init__(self, *a):
        pass

    def indexers(self):
        return [(1, "TrackerA"), (2, "TrackerB"), (3, "trackerc (API)")]

    def usenet_ids(self):
        return [10, 11]

    def search(self, q, ids, cats):
        FakePr.calls.append(list(ids or []))
        return [rel("T", "torrent", "x"), rel("N", "usenet", "y")]


@pytest.fixture
def pr(monkeypatch):
    FakePr.calls = []
    monkeypatch.setattr(pipeline, "Prowlarr", FakePr)
    return FakePr


CFG = SimpleNamespace(prowlarr_url="", prowlarr_key="", indexer_ids=[], categories=[])


def test_chosen_trackers_only(pr):
    t, n = pipeline.search(CFG, "q", trackers=["trackera", "trackerc (API)"])
    assert pr.calls == [[1, 3], [10, 11]]            # those trackers, then the Usenet indexers
    assert [r.protocol for r in t] == ["torrent"] and [r.protocol for r in n] == ["usenet"]


def test_no_choice_searches_everything_once(pr):
    pipeline.search(CFG, "q")
    assert pr.calls == [[]]


def test_an_nzb_only_search_asks_no_tracker(pr):
    t, n = pipeline.search(CFG, "q", torrents=False)
    assert t == [] and pr.calls == [[10, 11]]


def test_trackers_prowlarr_does_not_have(pr):
    with pytest.raises(ValueError):
        pipeline.search(CFG, "q", trackers=["Gone Tracker"])


def test_usenet_is_asked_without_a_file_names_extension(monkeypatch):
    asked = []

    class Pr(FakePr):
        def search(self, q, ids, cats):
            asked.append((q, list(ids or [])))
            # nothing on Usenet is named with ".mkv"
            return [] if q.endswith(".mkv") else [SimpleNamespace(title=q, protocol="usenet", indexer="y", guid=q)]
    monkeypatch.setattr(pipeline, "Prowlarr", Pr)
    name = "Some.Film.2022.2160p.WEB-DL.DDP5.1.H.265-Grp"
    t, n = pipeline.search(CFG, name + ".mkv", torrents=False)
    # and by what makes the release (an indexer matches every word of a search)
    parts = "some film 2022 2160p grp"
    assert [r.title for r in n] == [name, parts] and asked == [(name, [10, 11]), (parts, [10, 11])]
    asked.clear()
    t, n = pipeline.search(CFG, name + ".mkv")                 # every indexer: the torrent by its file name too
    assert [r.title for r in n] == [name, parts] and asked == [(name + ".mkv", []), (name, [10, 11]),
                                                               (parts, [10, 11])]
