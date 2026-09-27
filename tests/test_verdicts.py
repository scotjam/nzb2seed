"""A post found not to hold what a torrent needs is not asked for again on a retry.

Only verdicts that cannot change are kept: a post whose archive holds another release, or
one that downloaded completely and still lacked the files. A fetch that failed is not
remembered - that can work next time.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest  # noqa: E402

from nzb2seed import pipeline  # noqa: E402
from nzb2seed.clients import Release  # noqa: E402

H = "a" * 40


def rel(title="Show.S01E01.1080p.WEB-GRP", size=1000, guid="g1", indexer="IndexerA"):
    return Release(title, "usenet", indexer, 1, size, guid, "", "", "", 0, None, None)


@pytest.fixture
def v(tmp_path):
    return pipeline.Verdicts(str(tmp_path / "verdicts.json"))


def test_a_post_found_wanting_is_remembered(v):
    v.mark(H, "S01E01", rel(), "its archive holds a different release")
    assert v.seen(H, "S01E01", rel()) == "its archive holds a different release"


def test_it_is_remembered_across_a_restart(tmp_path):
    pipeline.Verdicts(str(tmp_path / "v.json")).mark(H, "S01E01", rel(), "no good")
    assert pipeline.Verdicts(str(tmp_path / "v.json")).seen(H, "S01E01", rel()) == "no good"


def test_the_same_post_on_another_indexer_is_recognised(v):
    """Same name, same exact size: a post that had the missing file would be bigger."""
    v.mark(H, "S01E01", rel(guid="g1", indexer="IndexerA"), "no good")
    assert v.seen(H, "S01E01", rel(guid="g2", indexer="IndexerB")) == "no good"


def test_a_bigger_post_of_the_same_name_is_not_assumed_the_same(v):
    """A repost carrying the missing file is bigger - it deserves its chance."""
    v.mark(H, "S01E01", rel(size=1000), "no good")
    assert v.seen(H, "S01E01", rel(guid="g2", size=1400)) == ""


def test_a_post_wrong_for_one_episode_may_be_right_for_another(v):
    v.mark(H, "S01E01", rel(), "no good")
    assert v.seen(H, "S01E02", rel()) == ""


def test_a_verdict_belongs_to_its_torrent(v):
    v.mark(H, "S01E01", rel(), "no good")
    assert v.seen("b" * 40, "S01E01", rel()) == ""


def test_everything_about_a_torrent_can_be_forgotten(v):
    v.mark(H, "S01E01", rel(), "no good")
    v.mark(H, "S01E02", rel(guid="g9"), "no good")
    assert v.forget(H) == 2 and v.seen(H, "S01E01", rel()) == ""


def test_marking_the_same_post_twice_keeps_one_entry(v):
    v.mark(H, "S01E01", rel(), "no good")
    v.mark(H, "S01E01", rel(), "no good")
    import json
    assert len(json.load(open(v.path))[H]["S01E01"]) == 1


def test_nothing_is_known_about_a_file_that_is_not_there(v):
    assert v.seen(H, "S01E01", rel()) == ""


# ---------------------------------------------------------------- a retry, end to end

def test_a_retry_skips_a_post_the_first_run_found_short(tmp_path):
    """E02's picked post downloads a file that is too short. The first run finds that out
    the expensive way; the retry must not even ask the indexer for its NZB again."""
    sys.path.insert(0, os.path.dirname(__file__))
    import test_retry as tr
    from nzb2seed.config import Config

    class ShortSAB(tr.SAB):
        def add_nzb(self, nzb, name, cat, pp, prio):
            nzo = super().add_nzb(nzb, name, cat, pp, prio)
            if self.jobs[nzo][1] == "pick2":                     # this post lacks bytes
                d = os.path.join(self.root, nzo)
                f = os.path.join(d, os.listdir(d)[0])
                with open(f, "r+b") as fh:
                    fh.truncate(os.path.getsize(f) - 500)
            return nzo

    class CountingPR(tr.PR):
        def __init__(self, results):
            super().__init__(results)
            self.fetched = []

        def fetch(self, r):
            self.fetched.append(r.guid)
            return super().fetch(r)

    t, raw, files = tr.pack()
    size = {i: len(next(v for k, v in files.items() if f"s03e0{i}" in k)) for i in range(1, 5)}
    grp = {i: "GRPB" if i == 4 else "GRPA" for i in range(1, 5)}
    picks = [[tr.rel(f"Show.2016.S03E0{i}.720p.HDTV.x264-{grp[i]}", size[i] + 100, f"pick{i}")]
             for i in range(1, 5)]
    others = [tr.rel(f"Show.2016.S03E02.720p.HDTV.x264-GRPA", size[2] + 300, "alt2")]
    cfg = Config(path=str(tmp_path / "c.toml"), sab_to_local=[["/dl", str(tmp_path / "dl")]],
                 torrent_dir=str(tmp_path / "torrents"))

    def one_run(n):
        pr = CountingPR(others)
        sab = ShortSAB(str(tmp_path / f"dl{n}"), files, set())
        run_cfg = Config(path=cfg.path, sab_to_local=[["/dl", str(tmp_path / f"dl{n}")]],
                         torrent_dir=cfg.torrent_dir)
        pipeline.usenet_multi(run_cfg, pr, sab, t, [list(p) for p in picks], 2, [])
        return pr

    first = one_run(1)
    assert "pick2" in first.fetched                             # found out the hard way
    reason = pipeline.verdicts_for(cfg).seen(t.infohash, "S03E02", picks[1][0])
    assert "did not hold all of" in reason

    second = one_run(2)
    assert "pick2" not in second.fetched                        # not asked for again
    assert "pick1" in second.fetched                            # the good ones still are
