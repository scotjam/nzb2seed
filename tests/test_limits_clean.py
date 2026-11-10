"""Tracker limits are checked before they are saved: from the form, a shared CSV, or any
request - nothing reaches nzb2seed.toml that could break it or be read back as something else."""
import math
import tomllib

from nzb2seed import config as c


def test_only_tracker_percent_and_mb_are_kept():
    rows = c.clean_limits([
        {"tracker": "TrackerA", "percent": 10, "evil": "x"},
        {"tracker": "Other", "mb": "200"},
    ])
    assert rows == [{"tracker": "TrackerA", "percent": 10}, {"tracker": "Other", "mb": 200}]


def test_bad_numbers_and_names_are_dropped_or_bounded():
    rows = c.clean_limits([
        {"tracker": "A", "percent": math.inf},
        {"tracker": "B", "percent": float("nan"), "mb": -5},
        {"tracker": "C", "percent": 250},
        {"tracker": "D", "mb": 10 ** 12},
        {"tracker": "E", "percent": True},
        {"tracker": "", "percent": 5},
        {"tracker": 12, "percent": 5},
        {"tracker": "x" * 5000, "percent": 5},
        {"tracker": "Bell\x00\x07\nName", "percent": 5},
        "not a row",
    ])
    by = {r["tracker"]: r for r in rows}
    assert "A" not in by and "B" not in by and "E" not in by
    assert by["C"]["percent"] == "full"                 # all of it, or more: full downloads
    assert by["D"]["mb"] == c.MAX_LIMIT_MB
    assert len(max(by, key=len)) == c.MAX_TRACKER_NAME
    assert "BellName" in by


def test_one_row_per_tracker_and_no_more_than_the_maximum():
    rows = c.clean_limits([{"tracker": "Same", "percent": 1}, {"tracker": "same (API)", "percent": 2}]
                          + [{"tracker": f"t{i}", "percent": 1} for i in range(5000)])
    assert sum(1 for r in rows if c.tracker_key(r["tracker"]) == "same") == 1
    assert len(rows) == c.MAX_TRACKERS


def test_a_saved_config_reads_back_exactly_whatever_the_names(tmp_path):
    cfg = c.Config(path=tmp_path / "n.toml")
    cfg.nearly_limits = [{"tracker": 'Quote " and ] and } and \\ ', "percent": 5},
                         {"tracker": "TrackerA", "percent": 10}]
    cfg.nearly_auto_trackers = ["TrackerA", 'x"]\n[sabnzbd]\nurl = "evil"']
    cfg = c.finalize(cfg)
    c.save(cfg)
    data = tomllib.loads((tmp_path / "n.toml").read_text(encoding="utf-8"))
    assert data["behaviour"]["nearly_limits"] == cfg.nearly_limits
    assert data["behaviour"]["nearly_auto_trackers"] == cfg.nearly_auto_trackers
    assert data.get("sabnzbd", {}).get("url") != "evil"


def test_an_odd_key_is_quoted_not_written_bare():
    assert c._toml({"ok_key": 1}) == "{ok_key = 1}"
    assert c._toml({'a = 1}\n[x]': 1}) == '{"a = 1}\\n[x]" = 1}'


def test_none_full_not_specified_and_custom():
    """Each of % and MB: None allowed (0), Full downloads allowed, Not specified (left out) or
    Custom. None allowed in either: nothing is ever downloaded, and its jobs do not ask.
    Not specified leaves it to the other; both: not set - nothing either, and its jobs ask.
    A custom % of 100 or more is the whole torrent: full downloads allowed."""
    rows = c.clean_limits([{"tracker": "None", "percent": 0, "mb": 0}, {"tracker": "PctNoneMb", "percent": 5, "mb": "0"},
                           {"tracker": "Unset", "percent": "", "mb": None},
                           {"tracker": "Pct", "percent": 5, "mb": ""}, {"tracker": "Mb", "mb": 200},
                           {"tracker": "Full", "percent": "Full"}, {"tracker": "FullMb", "percent": "full", "mb": 300},
                           {"tracker": "Hundred", "percent": 100}])
    assert rows == [{"tracker": "None", "percent": 0, "mb": 0}, {"tracker": "PctNoneMb", "percent": 5, "mb": 0},
                    {"tracker": "Pct", "percent": 5}, {"tracker": "Mb", "mb": 200},
                    {"tracker": "Full", "percent": "full"}, {"tracker": "FullMb", "percent": "full", "mb": 300},
                    {"tracker": "Hundred", "percent": "full"}]
    cfg = c.Config(path="x.toml", nearly_limits=rows)
    for name in ("None", "PctNoneMb"):
        assert c.tracker_limit(cfg, name) is None and c.limit_set(cfg, name)
        assert not c.within_limit(cfg, name, 0.000001, 1, 0)
        assert c.limit_text(cfg, name) == "nothing"
    assert not c.limit_set(cfg, "Unset") and c.tracker_limit(cfg, "Unset") is None
    assert c.tracker_limit(cfg, "Pct") == (5, 0) and c.within_limit(cfg, "Pct", 0.01, 10 ** 12, 0)
    assert c.tracker_limit(cfg, "Mb") == (0, 200) and c.within_limit(cfg, "Mb", 0.9, 100 << 20, 0)
    assert c.within_limit(cfg, "Full", 0.99, 10 ** 13) and c.limit_text(cfg, "Full").startswith("everything")
    assert c.within_limit(cfg, "FullMb", 0.99, 200 << 20) and not c.within_limit(cfg, "FullMb", 0.01, 290 << 20)
    assert c._toml(rows[4]) == '{tracker = "Full", percent = "full"}'
