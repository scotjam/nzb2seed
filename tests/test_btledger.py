"""Every start of an incomplete torrent counts against its tracker's limit, in total."""
from types import SimpleNamespace

from nzb2seed import btledger
from nzb2seed.gui import App

MB = 1024 ** 2


def test_what_earlier_starts_took_is_counted(tmp_path):
    cfg = SimpleNamespace(path=str(tmp_path / "nzb2seed.toml"))
    first = {"added_on": 100, "downloaded": 0, "total_size": 1000 * MB}
    btledger.record_start(cfg, "h", "TrackerA", first, 30 * MB)           # 3% short, started
    btledger.observe(cfg, [{"hash": "h", "added_on": 100, "downloaded": 31 * MB}])
    # still the same entry in qBittorrent: what it downloaded so far
    assert btledger.taken(cfg, "h", {"added_on": 100, "downloaded": 31 * MB}) == 31 * MB
    # removed and added again: the old entry counts in full, the new one from zero
    assert btledger.taken(cfg, "h", {"added_on": 200, "downloaded": 0}) == 31 * MB
    # an entry nobody saw finish counts what it was let download
    btledger.record_start(cfg, "h", "TrackerA", {"added_on": 200, "downloaded": 0, "total_size": 1000 * MB}, 40 * MB)
    assert btledger.taken(cfg, "h", {"added_on": 300, "downloaded": 0}) == 71 * MB
    assert btledger.taken(cfg, "other", None) == 0


def test_the_limit_is_checked_against_the_total(tmp_path):
    # 4% short now, 3% taken before: 7% against a 7.2% limit (8% less 10% headroom)
    share, size = App.counting_earlier(0.96, 40 * MB, 1000 * MB, 30 * MB)
    assert round(share, 4) == 0.07 and size == 70 * MB
    cfg = SimpleNamespace(nearly_limits=[{"tracker": "TrackerA", "percent": 8, "mb": None}])
    from nzb2seed.config import within_limit
    assert within_limit(cfg, "TrackerA", share, size)
    share, size = App.counting_earlier(0.95, 50 * MB, 1000 * MB, 30 * MB)   # 8% in total: over
    assert not within_limit(cfg, "TrackerA", share, size)
    assert App.counting_earlier(0.97, 30 * MB, 1000 * MB, 0) == (0.030000000000000027, 30 * MB)
