from types import SimpleNamespace

from nzb2seed import pipeline
from nzb2seed.ledger import Ledger


def test_a_few_files_trimmed_out_of_a_post_are_never_the_post(tmp_path):
    """A small download of a post's .jpg (or sample) is not offered as the post itself."""
    led = Ledger(str(tmp_path / "ledger.json"))
    led.record("whole", "Show.S01E07-GRP", "g1", "Idx", 700)
    led.record("part", "Show.S01-GRP.extras", "g1", "Idx", None, part=True)
    led.record("old", "Show.S01-GRP.extras", "g1", "Idx", None)       # recorded before the flag
    assert [j["nzo"] for j in led.matches("Show.S01E07-GRP", "g1", 700)] == ["whole"]


def test_small_downloads_go_ahead_of_whole_posts_in_sabnzbd():
    assert pipeline.small_priority(SimpleNamespace(sab_priority=0)) == 1       # High
    assert pipeline.small_priority(SimpleNamespace(sab_priority=-1)) == 1
    assert pipeline.small_priority(SimpleNamespace(sab_priority=2)) == 2       # Force stays
