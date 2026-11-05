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
    assert by["C"]["percent"] == 100
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
