"""Every add to the torrent client keeps 10% of a tracker's limit free: data that turns out
bad and comes down twice must not carry a torrent over the limit."""
from types import SimpleNamespace

from nzb2seed import config

CFG = SimpleNamespace(nearly_limits=[{"tracker": "TrackerB", "percent": 10, "mb": 0},
                                     {"tracker": "TrackerD", "percent": 5, "mb": 1000}])
H = config.AUTO_HEADROOM


def test_the_percentage_keeps_ten_percent_free():
    assert config.within_limit(CFG, "TrackerB", 0.095, None, 0)         # under 10%
    assert not config.within_limit(CFG, "TrackerB", 0.095, None)        # but not under 9%
    assert config.within_limit(CFG, "TrackerB", 0.089, None, H)


def test_the_mb_limit_keeps_ten_percent_free_too():
    mb = 1024 ** 2
    assert config.within_limit(CFG, "TrackerD", 0.01, 950 * mb, 0)
    assert not config.within_limit(CFG, "TrackerD", 0.01, 950 * mb, H)  # over 900 MB
    assert config.within_limit(CFG, "TrackerD", 0.01, 899 * mb, H)


def test_a_tracker_without_a_limit_still_allows_nothing():
    assert not config.within_limit(CFG, "Elsewhere", 0.0001, 1, H)


def test_the_headroom_is_the_default_for_every_check():
    """Add to torrent client can download corrupt data twice too - only Override skips it."""
    assert not config.within_limit(CFG, "TrackerB", 0.095, None)
    assert "keeping 10% of it free" in config.limit_text(CFG, "TrackerB")
