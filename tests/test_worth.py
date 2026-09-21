"""Only building what pays for the disk it takes - decided by rules, not by a score.

The point of the rules is that each one can be read, checked against the numbers behind
it, and turned off again. Nothing is predicted about a release nobody has seeded.
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from nzb2seed import worth  # noqa: E402
from nzb2seed.config import Config, finalize  # noqa: E402

GB = worth.GB
OLD = time.time() - 30 * 86400


def t(name, tracker, size_gb, up_gb, added=OLD, path=None):
    return {"name": name, "tracker": f"https://{tracker}/announce", "size": int(size_gb * GB),
            "uploaded": int(up_gb * GB), "added_on": added,
            "content_path": path or f"/data/{tracker}/{name}"}


# ---------------------------------------------------------------- what a release is

@pytest.mark.parametrize("name, kind", [
    ("Show.S01E02.1080p.WEB-DL.x264-GRP", "tv episode"),
    ("Show.S01.1080p.WEB-DL.x264-GRP", "tv season"),
    ("Some.Film.2019.1080p.BluRay.x264-GRP", "movie"),
    ("Some.Film.2019.2160p.UHD.BluRay.REMUX-GRP", "movie"),
    ("Artist.-.Album.Name.2019.FLAC.24bit", "music"),
    ("Artist.Discography.1975-2019.FLAC", "discography"),
    ("Some.Book.2019.RETAIL.EPUB-GRP", "ebook"),
    ("Some.Book.2019.AUDIOBOOK.M4B-GRP", "audiobook"),
    ("Some.Game.2019.REPACK-FITGIRL", "game"),
    ("EPL.2026.09.20.Team.vs.Team.1080p.WEB.h264-GRP", "sport"),
    ("Some.Title.XXX.1080p.WEB-GRP", "xxx"),
    ("Something.Unlabelled", "other"),
])
def test_it_tells_the_kinds_of_release_apart(name, kind):
    assert worth.kind_of(name) == kind


def test_a_film_with_album_in_its_name_is_still_a_film():
    """"cd", "ep" and "album" turn up inside film names, so video wins when it looks video."""
    assert worth.kind_of("The.Album.2019.1080p.BluRay.x264-GRP") == "movie"


def test_the_release_group_comes_off_the_end():
    assert worth.group_of("Some.Film.2019.1080p.BluRay.x264-GRP") == "GRP"
    assert worth.group_of("Some Film 2019 1080p BluRay x264-ReMuXGrP") == "REMUXGRP"
    assert worth.group_of("Some.Film.2019.1080p.BluRay.x264-GRP.mkv") == "GRP"
    assert worth.group_of("no group here") == ""


def test_a_release_is_matched_by_every_axis_at_once():
    keys = worth.facet_keys("x.org", 20 * GB, "Show.S01.1080p.WEB-DL.x264-GRP")
    assert "tracker:x.org" in keys and "type:tv season" in keys
    assert "size:15-30G" in keys and "group:GRP" in keys
    assert "tracker+type:x.org tv season" in keys


# ---------------------------------------------------------------- which rules are offered

def dead_set(tracker, n=10, size_gb=20, name="Film.{i}.2019.1080p.BluRay.x264-GRP"):
    return [t(name.format(i=i), tracker, size_gb, 0) for i in range(n)]


def live_set(tracker, n=10, size_gb=20, ratio=2.0, name="Film.{i}.2019.1080p.BluRay.x264-OK"):
    return [t(name.format(i=i), tracker, size_gb, size_gb * ratio) for i in range(n)]


def test_a_rule_is_offered_for_a_kind_of_release_that_never_uploads():
    offered = worth.rules(dead_set("dead.example"), least=3)
    row = next(r for r in offered if r["rule"] == "group:GRP")
    assert row["n"] == 10 and row["ratio"] == 0.0 and row["dead"] == 100
    assert row["saves_gb"] == 200.0 and row["costs_gb"] == 0.0


def test_a_whole_tracker_is_never_offered_for_blocking():
    """The ratio worth building is the one on the trackers you are weakest on, so giving
    up on a weak tracker is giving up on the point of the exercise."""
    offered = worth.rules(dead_set("dead.example"), least=3)
    assert [r for r in offered if r["what"] in ("tracker", "tracker+type")] == []


def test_no_rule_is_offered_for_a_kind_of_release_that_pays():
    assert [r for r in worth.rules(live_set("good.example"), least=3) if r["what"] == "group"] == []


def test_a_group_too_small_to_judge_is_never_offered():
    assert worth.rules(dead_set("tiny.example", n=2), least=8) == []


def test_a_group_that_is_poor_but_alive_is_not_offered():
    """Returning little is not the same as returning nothing: most must be dead too."""
    rows = [t(f"Film.{i}.2019.1080p.BluRay-GRP", "meh.example", 20, 0.4) for i in range(10)]
    assert [r for r in worth.rules(rows, least=3) if r["what"] == "tracker"] == []


def test_rules_are_ordered_by_the_disk_they_would_save():
    rows = (dead_set("a.example", n=10, size_gb=2, name="Small.{i}.2019.1080p.BluRay-SMALLGRP")
            + dead_set("a.example", n=10, size_gb=50, name="Big.{i}.2019.1080p.BluRay-BIGGRP"))
    offered = [r for r in worth.rules(rows, least=3) if r["what"] == "group"]
    assert [r["value"] for r in offered] == ["BIGGRP", "SMALLGRP"]


def test_young_torrents_do_not_count():
    fresh = [t(f"Film.{i}.2019.1080p.BluRay-GRP", "new.example", 20, 0, added=time.time() - 3600)
             for i in range(9)]
    assert worth.rules(fresh, age_days=7, least=3) == []


def test_the_evidence_behind_a_rule_can_be_looked_up():
    stats = worth.rule_stats(dead_set("dead.example"))
    assert stats["rule:tracker:dead.example"] == [10, 0.0]


# ---------------------------------------------------------------- the dashboard

def test_the_overview_adds_up():
    rows = live_set("a.example", n=5, size_gb=10, ratio=2.0) + dead_set("b.example", n=5, size_gb=10)
    o = worth.overview(rows, age_days=7, least=3)
    assert o["torrents"] == 10 and o["stored_gb"] == 100.0 and o["uploaded_gb"] == 100.0
    assert o["overall_ratio"] == 1.0 and o["dead"] == 5 and o["dead_gb"] == 50.0
    by_tracker = next(g for g in o["by"] if g["what"] == "tracker")
    assert [r["where"] for r in by_tracker["rows"]] == ["b.example", "a.example"]   # worst first


def test_the_overview_offers_rules_with_their_cost():
    rows = live_set("a.example", n=5, size_gb=10) + dead_set("b.example", n=5, size_gb=10)
    o = worth.overview(rows, age_days=7, least=3)
    rule = next(r for r in o["rules"] if r["rule"] == "group:GRP")      # the dead set's group
    assert rule["saves_gb"] == 50.0 and rule["costs_gb"] == 0.0 and rule["dead"] == 100


def test_the_overview_breaks_down_by_every_axis():
    rows = live_set("a.example") + dead_set("b.example")
    assert [g["what"] for g in worth.overview(rows, age_days=7, least=3)["by"]] == \
        ["tracker", "type", "size", "group", "category"]


def test_the_overview_survives_an_empty_library():
    o = worth.overview([], age_days=7)
    assert o["torrents"] == 0 and o["overall_ratio"] == 0 and o["rules"] == []


# ---------------------------------------------------------------- cross-seeds

def copy_of(row, tracker, up_gb, added=None):
    """The same files registered with a second tracker: no extra disk is used."""
    return {**row, "tracker": f"https://{tracker}/announce", "uploaded": int(up_gb * GB),
            "added_on": added or (row["added_on"] + 3600)}


def test_a_cross_seed_costs_no_extra_disk():
    first = t("Film.2019.1080p.BluRay-GRP", "a.example", 10, 5)
    rows = [first, copy_of(first, "b.example", 3)]
    o = worth.overview(rows, age_days=7, least=1)
    assert o["torrents"] == 1                       # one lot of data, not two
    assert o["stored_gb"] == 10.0                   # counted once
    assert o["uploaded_gb"] == 8.0                  # 5 + 3: every copy's upload counts
    assert o["cross_seeds"] == 1 and o["cross_uploaded_gb"] == 3.0


def test_the_upload_is_credited_to_whoever_brought_the_data_in():
    """The cross-seed only exists because the first grab did, so the first tracker gets it."""
    first = t("Film.2019.1080p.BluRay-GRP", "brought.it.in", 10, 0)
    rows = [first, copy_of(first, "second.example", 12)]
    by_tracker = next(g for g in worth.overview(rows, age_days=7, least=1)["by"]
                      if g["what"] == "tracker")
    assert [r["where"] for r in by_tracker["rows"]] == ["brought.it.in"]
    assert by_tracker["rows"][0]["ratio"] == 1.2    # 12 GB up against its 10 GB


def test_a_cross_seed_that_uploads_nothing_is_not_counted_as_dead():
    """Its data is seeding perfectly well under the torrent that brought it in."""
    first = t("Film.2019.1080p.BluRay-GRP", "a.example", 10, 20)
    rows = [first, copy_of(first, "quiet.example", 0)]
    o = worth.overview(rows, age_days=7, least=1)
    assert o["dead"] == 0 and o["dead_gb"] == 0.0


def test_a_tracker_of_nothing_but_cross_seeds_offers_no_rule():
    """It cannot be judged on data it did not bring in - there is nothing of its own."""
    rows = []
    for i in range(10):
        first = t(f"Film.{i}.2019.1080p.BluRay-GRP", "source.example", 10, 30)
        rows += [first, copy_of(first, "mirror.example", 0)]
    offered = [r["value"] for r in worth.rules(rows, least=3) if r["what"] == "tracker"]
    assert "mirror.example" not in offered


def test_the_same_release_in_two_places_is_still_one_lot_of_data():
    """cross-seed hard-links into a folder of its own, so the paths differ while the bytes
    are shared - the name and the exact byte count are what give it away."""
    a = t("Film.2019.1080p.BluRay-GRP", "a.example", 10, 1, path="/data/complete/Film")
    b = t("Film.2019.1080p.BluRay-GRP", "b.example", 10, 1, path="/data/cross-seed/b/Film")
    assert len(worth.collapse([a, b])) == 1


def test_two_different_releases_are_never_merged():
    a = t("Film.2019.1080p.BluRay-GRP", "a.example", 10, 1)
    b = t("Film.2019.2160p.BluRay-GRP", "a.example", 10, 1)          # a different encode
    c = t("Film.2019.1080p.BluRay-GRP", "a.example", 11, 1)          # a different size
    assert len(worth.collapse([a, b, c])) == 3
