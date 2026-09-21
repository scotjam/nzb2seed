"""Asking the tracker (through Prowlarr) when a release was posted and who wants it.

Every lookup costs an indexer search, so the point of most of these tests is that it is
asked for once and not again.
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from nzb2seed import lookup, rules  # noqa: E402
from nzb2seed.config import Config, finalize  # noqa: E402

NAME = "Some.Film.2019.1080p.BluRay.x264-GRP"


class Hit:
    def __init__(self, title=NAME, when="2026-09-20T22:12:11Z", seeders=11, grabs=12,
                 indexer="ATracker"):
        self.title, self.publish_date = title, when
        self.seeders, self.grabs, self.indexer = seeders, grabs, indexer


class FakeProwlarr:
    def __init__(self, hits=None):
        self.hits = hits if hits is not None else [Hit()]
        self.searches = 0

    def search(self, query, ids, cats):
        self.searches += 1
        return list(self.hits)


@pytest.fixture
def cache(tmp_path):
    return lookup.Cache(str(tmp_path / "tracker-info.json"))


# ---------------------------------------------------------------- asking

def test_it_reads_the_posting_time_and_the_counts(cache):
    pr = FakeProwlarr()
    info = lookup.find(pr, cache, NAME)
    assert info.published == lookup.when("2026-09-20T22:12:11Z")
    assert info.seeders == 11 and info.grabs == 12 and info.indexer == "ATracker"


def test_it_only_takes_a_result_with_the_same_name(cache):
    pr = FakeProwlarr([Hit(title="Some.Other.Film.2019.1080p-GRP")])
    info = lookup.find(pr, cache, NAME)
    assert info.published == 0 and info.seeders == -1          # a miss, not a wrong answer


def test_the_busiest_result_wins_when_several_trackers_have_it(cache):
    pr = FakeProwlarr([Hit(seeders=2, grabs=1, indexer="quiet"),
                       Hit(seeders=40, grabs=9, indexer="busy")])
    assert lookup.find(pr, cache, NAME).indexer == "busy"


# ---------------------------------------------------------------- not asking twice

def test_the_answer_is_cached(cache):
    pr = FakeProwlarr()
    lookup.find(pr, cache, NAME)
    lookup.find(pr, cache, NAME)
    assert pr.searches == 1


def test_a_miss_is_cached_too(cache):
    """Not finding it is an answer: asking again would cost another search for nothing."""
    pr = FakeProwlarr([])
    lookup.find(pr, cache, NAME)
    lookup.find(pr, cache, NAME)
    assert pr.searches == 1


def test_the_posting_time_is_never_asked_for_twice(cache, monkeypatch):
    """It cannot change, so even a stale entry is good enough when only the date matters."""
    pr = FakeProwlarr()
    lookup.find(pr, cache, NAME)
    cache.get(NAME).asked = time.time() - 9999            # long stale
    lookup.find(pr, cache, NAME, want_counts=False)
    assert pr.searches == 1


def test_the_counts_are_asked_for_again_once_stale(cache):
    pr = FakeProwlarr()
    lookup.find(pr, cache, NAME, want_counts=True)
    cache._load()[lookup._norm(NAME)].asked = time.time() - lookup.FRESH_FOR - 1
    lookup.find(pr, cache, NAME, want_counts=True)
    assert pr.searches == 2


def test_the_cache_survives_a_restart(tmp_path):
    pr = FakeProwlarr()
    lookup.find(pr, lookup.Cache(str(tmp_path / "c.json")), NAME)
    again = lookup.Cache(str(tmp_path / "c.json"))         # a new process
    assert again.get(NAME).seeders == 11
    lookup.find(pr, again, NAME)
    assert pr.searches == 1


def test_the_cache_can_be_emptied(cache):
    lookup.find(FakeProwlarr(), cache, NAME)
    assert cache.clear() == 1 and cache.get(NAME) is None


def test_a_broken_prowlarr_never_stops_a_build(cache):
    class Broken:
        def search(self, *a):
            raise RuntimeError("down")

    assert lookup.find(Broken(), cache, NAME) is None


# ---------------------------------------------------------------- what the rules do with it

def cfg_with(tmp_path, priority=(), block=()):
    return finalize(Config(path=tmp_path / "nzb2seed.toml", demand_rules=list(priority),
                           demand_block=list(block)))


def test_nothing_is_looked_up_unless_a_rule_asks(tmp_path):
    plain = cfg_with(tmp_path, priority=[{"trackers": ["x.org"], "min_gb": 5}])
    assert rules.needs_lookup(plain) is False
    asks = cfg_with(tmp_path, priority=[{"max_age_min": 30}])
    assert rules.needs_lookup(asks) is True
    assert rules.wants_counts(asks) is False           # the date alone: no repeat searches
    counts = cfg_with(tmp_path, priority=[{"min_seeders": 5}])
    assert rules.needs_lookup(counts) and rules.wants_counts(counts)


def test_a_disabled_rule_does_not_make_us_look_anything_up(tmp_path):
    cfg = cfg_with(tmp_path, priority=[{"max_age_min": 30, "enabled": False}])
    assert rules.needs_lookup(cfg) is False


def test_age_prefers_what_the_tracker_says(tmp_path):
    now = time.time()
    rel = rules.Release(name=NAME, size=1, first_seen=now - 60,      # reached us a minute ago
                        published=now - 7200)                        # posted two hours ago
    assert rules.matches({"max_age_min": 30}, rel, now) is False
    rel.published = 0                                                # nothing looked up
    assert rules.matches({"max_age_min": 30}, rel, now) is True


def test_seeders_and_grabs_as_conditions(tmp_path):
    rel = rules.Release(name=NAME, size=1, seeders=11, grabs=12)
    assert rules.matches({"min_seeders": 5}, rel) is True
    assert rules.matches({"min_seeders": 20}, rel) is False
    assert rules.matches({"min_grabs": 12}, rel) is True
    assert rules.matches({"min_grabs": 13}, rel) is False


def test_an_unknown_count_never_refuses_a_release(tmp_path):
    """If Prowlarr could not say, the rule must not quietly stop the build."""
    rel = rules.Release(name=NAME, size=1)                 # seeders and grabs unknown
    assert rules.matches({"min_seeders": 50}, rel) is True
    assert rules.matches({"min_grabs": 50}, rel) is True


# ---------------------------------------------------------------- at least / at most

def counted(seeders=-1, leechers=-1, grabs=-1):
    return rules.Release(name=NAME, size=1, seeders=seeders, leechers=leechers, grabs=grabs)


@pytest.mark.parametrize("rule, hit", [
    ({"min_seeders": 5}, True),
    ({"min_seeders": 30}, False),
    ({"max_seeders": 30}, True),
    ({"max_seeders": 5}, False),
    ({"min_seeders": 5, "max_seeders": 30}, True),        # a band, not just a floor
    ({"min_leechers": 2}, True),
    ({"max_leechers": 2}, False),
    ({"min_grabs": 10}, True),
    ({"max_grabs": 10}, False),
    ({"min_grabs": 5, "max_grabs": 10}, False),           # 12 grabs is over the top of it
])
def test_a_band_on_each_count(rule, hit):
    assert rules.matches(rule, counted(seeders=11, leechers=3, grabs=12)) is hit


def test_a_count_that_is_not_known_never_refuses_anything():
    """Prowlarr could not say, so the rule must not quietly stop the build."""
    for rule in ({"min_seeders": 99}, {"max_seeders": 1}, {"min_leechers": 99},
                 {"max_leechers": 1}, {"min_grabs": 99}, {"max_grabs": 1}):
        assert rules.matches(rule, counted()) is True


def test_a_band_reads_as_a_sentence():
    said = rules.describe({"name": "busy but not flooded", "min_seeders": 5, "max_seeders": 40})
    assert "5 to 40 seeders" in said
    assert "at most 3 leechers" in rules.describe({"max_leechers": 3})


def test_an_upper_bound_also_asks_the_tracker(tmp_path):
    cfg = cfg_with(tmp_path, priority=[{"max_seeders": 40}])
    assert rules.needs_lookup(cfg) and rules.wants_counts(cfg)


def test_the_lookup_keeps_the_leecher_count(cache):
    class WithLeechers(Hit):
        def __init__(self):
            super().__init__()
            self.leechers = 4

    info = lookup.find(FakeProwlarr([WithLeechers()]), cache, NAME)
    assert info.leechers == 4
