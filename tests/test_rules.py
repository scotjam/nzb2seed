"""Priority rules and the blocklist: what to chase first, and what never to chase."""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from nzb2seed import rules  # noqa: E402
from nzb2seed.config import Config, finalize  # noqa: E402

GB = rules.GB
NOW = time.time()


def rel(name="Some.Film.2019.2160p.UHD.BluRay.REMUX-REMUXGRP", size_gb=40, tracker="atracker.example",
        category="", minutes_old=5.0):
    return rules.Release(name=name, size=int(size_gb * GB), tracker=tracker,
                         category=category, first_seen=NOW - minutes_old * 60)


def cfg_with(tmp_path, priority=(), block=(), only=False):
    return finalize(Config(path=tmp_path / "nzb2seed.toml", demand_rules=list(priority),
                           demand_block=list(block), demand_only_rules=only))


# ---------------------------------------------------------------- one condition at a time

def test_a_rule_with_no_conditions_matches_anything():
    assert rules.matches({}, rel(), NOW) is True


@pytest.mark.parametrize("rule, hit", [
    ({"trackers": ["atracker.example"]}, True),
    ({"trackers": ["other.example"]}, False),
    ({"trackers": ["other.example", "atracker.example"]}, True),      # any of them will do
    ({"types": ["movie"]}, True),
    ({"types": ["tv season", "tv episode"]}, False),
    ({"groups": ["REMUXGRP"]}, True),
    ({"groups": ["remuxgrp"]}, True),                            # case does not matter
    ({"groups": ["OTHERGRP"]}, False),
    ({"keywords": ["remux"]}, True),
    ({"keywords": ["hdcam"]}, False),
    ({"not_keywords": ["german"]}, True),
    ({"not_keywords": ["remux"]}, False),
    ({"min_gb": 20}, True),
    ({"min_gb": 80}, False),
    ({"max_gb": 80}, True),
    ({"max_gb": 10}, False),
    ({"max_age_min": 30}, True),
    ({"max_age_min": 1}, False),
    ({"categories": ["kids tv"]}, False),
])
def test_each_condition(rule, hit):
    assert rules.matches(rule, rel(), NOW) is hit


def test_every_condition_has_to_hold_at_once():
    rule = {"trackers": ["atracker.example"], "types": ["movie"], "min_gb": 20, "max_age_min": 30}
    assert rules.matches(rule, rel(), NOW) is True
    assert rules.matches(rule, rel(size_gb=5), NOW) is False          # too small
    assert rules.matches(rule, rel(minutes_old=90), NOW) is False     # too old
    assert rules.matches(rule, rel(tracker="elsewhere"), NOW) is False


def test_the_category_comes_from_qbittorrent():
    assert rules.matches({"categories": ["kids tv"]}, rel(category="kids tv"), NOW) is True


# ---------------------------------------------------------------- deciding

FAST = {"name": "fresh remuxes", "trackers": ["atracker.example"], "min_gb": 20, "max_age_min": 30}
SLOW = {"name": "anything from that tracker", "trackers": ["atracker.example"]}


def test_the_first_matching_rule_sets_the_priority(tmp_path):
    cfg = cfg_with(tmp_path, priority=[FAST, SLOW])
    assert rules.decide(cfg, rel(), NOW).priority == 0
    assert rules.decide(cfg, rel(minutes_old=600), NOW).priority == 1      # too old for FAST


def test_something_no_rule_matches_is_still_built_but_last(tmp_path):
    cfg = cfg_with(tmp_path, priority=[FAST, SLOW])
    call = rules.decide(cfg, rel(tracker="elsewhere"), NOW)
    assert call.build is True and call.priority == 2 and call.why == ""


def test_only_what_a_rule_matches_can_be_built(tmp_path):
    cfg = cfg_with(tmp_path, priority=[FAST], only=True)
    assert rules.decide(cfg, rel(), NOW).build is True
    call = rules.decide(cfg, rel(tracker="elsewhere"), NOW)
    assert call.build is False and "only what a rule matches" in call.why


def test_only_rules_with_no_rules_at_all_still_builds(tmp_path):
    """Switching it on before writing a rule must not stop everything."""
    cfg = cfg_with(tmp_path, priority=[], only=True)
    assert rules.decide(cfg, rel(), NOW).build is True


def test_a_disabled_rule_is_ignored(tmp_path):
    cfg = cfg_with(tmp_path, priority=[{**FAST, "enabled": False}, SLOW])
    assert rules.decide(cfg, rel(), NOW).priority == 0        # SLOW is now the first one
    assert "anything from that tracker" in rules.decide(cfg, rel(), NOW).why


# ---------------------------------------------------------------- the blocklist

def test_the_blocklist_stops_a_build(tmp_path):
    cfg = cfg_with(tmp_path, block=[{"name": "no german", "not_keywords": [], "keywords": ["german"]}])
    call = rules.decide(cfg, rel(name="Some.Film.2019.GERMAN.1080p.BluRay-GRP"), NOW)
    assert call.build is False and "blocked by no german" in call.why


def test_a_block_beats_a_priority_rule(tmp_path):
    """However high the priority rule sits, the blocklist is checked first."""
    cfg = cfg_with(tmp_path, priority=[FAST], block=[{"name": "not this group",
                                                      "groups": ["REMUXGRP"]}])
    assert rules.decide(cfg, rel(), NOW).build is False


def test_the_blocklist_only_names_what_it_matches(tmp_path):
    cfg = cfg_with(tmp_path, block=[{"name": "no xxx", "types": ["xxx"]}])
    assert rules.decide(cfg, rel(), NOW).build is True


# ---------------------------------------------------------------- the queue order

def test_the_queue_is_sorted_by_priority_then_arrival(tmp_path):
    cfg = cfg_with(tmp_path, priority=[FAST])
    early_nomatch = (rules.sort_key(cfg, rel(tracker="elsewhere"), 100, NOW), "early no match")
    late_match = (rules.sort_key(cfg, rel(), 900, NOW), "late match")
    early_match = (rules.sort_key(cfg, rel(), 200, NOW), "early match")
    order = [name for _, name in sorted([early_nomatch, late_match, early_match])]
    assert order == ["early match", "late match", "early no match"]


# ---------------------------------------------------------------- saying it in words

def test_a_rule_reads_as_a_sentence():
    said = rules.describe(FAST)
    assert "fresh remuxes" in said and "on atracker.example" in said
    assert "at least 20 GB" in said and "under 30 minutes old" in said


def test_a_rule_with_no_conditions_says_so():
    assert rules.describe({"name": "everything"}) == "everything: anything"


# ---------------------------------------------------------------- proposals to chase

def test_the_best_kinds_of_release_are_proposed_as_rules():
    stats = [{"what": "type", "value": "tv season", "n": 90, "ratio": 1.41, "dead": 0},
             {"what": "type", "value": "tv episode", "n": 60, "ratio": 0.02, "dead": 70},
             {"what": "group", "value": "REMUXGRP", "n": 20, "ratio": 2.20, "dead": 5}]
    out = rules.propose(stats, least=8)
    assert [p["value"] for p in out] == ["REMUXGRP", "tv season"]         # best first
    assert out[0]["rule"]["groups"] == ["REMUXGRP"]
    assert out[1]["rule"]["types"] == ["tv season"]


def test_a_tracker_is_never_proposed():
    """Chasing the tracker that already pays spends the effort where it is least needed:
    the ratio worth building is the one on the trackers you are weakest on."""
    stats = [{"what": "tracker", "value": "good.example", "n": 90, "ratio": 1.41, "dead": 0},
             {"what": "tracker+type", "value": "good.example movie", "n": 40, "ratio": 2.0, "dead": 0}]
    assert rules.propose(stats, least=8) == []


def test_a_group_with_too_few_torrents_is_not_proposed():
    stats = [{"what": "tracker", "value": "x.example", "n": 3, "ratio": 9.0, "dead": 0}]
    assert rules.propose(stats, least=8) == []


def test_a_group_that_mostly_dies_is_not_proposed_however_high_its_median():
    stats = [{"what": "tracker", "value": "x.example", "n": 40, "ratio": 1.5, "dead": 55}]
    assert rules.propose(stats, least=8) == []


def test_a_size_proposal_becomes_a_minimum_size():
    stats = [{"what": "size", "value": ">60G", "n": 24, "ratio": 0.43, "dead": 0}]
    out = rules.propose(stats, least=8, good=0.4)
    assert out[0]["rule"]["min_gb"] == 60


def test_a_tracker_rule_covers_its_subdomains():
    """Some trackers announce from a different subdomain every time."""
    r = rel(tracker="announce7.tracker.example.org")
    assert rules.matches({"trackers": ["tracker.example.org"]}, r, NOW) is True
    assert rules.matches({"trackers": ["announce7.tracker.example.org"]}, r, NOW) is True
    assert rules.matches({"trackers": [".tracker.example.org"]}, r, NOW) is True
    assert rules.matches({"trackers": ["example.org"]}, r, NOW) is True
    assert rules.matches({"trackers": ["ker.example.org"]}, r, NOW) is False  # not a boundary
    assert rules.matches({"trackers": ["tracker.example.org"]}, rel(tracker="elsewhere.org"), NOW) is False
