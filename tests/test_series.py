"""Whole-series grabs: what you already have, and which group each season uses."""
import os

from nzb2seed import gui, series
from nzb2seed.clients import Release
from nzb2seed.config import Config
from nzb2seed.season import Option, same_show


def test_episode_names():
    assert series.episodes_named("Show.2016.S02E03.720p.HDTV.x264-GRPA") == {(2, 3)}
    assert series.episodes_named("show.s01e01e02.mkv") == {(1, 1), (1, 2)}
    assert series.episodes_named("Show.S01E01-E03.1080p") == {(1, 1), (1, 2), (1, 3)}
    assert series.episodes_named("Show.S02.720p.HDTV.x264-GRPA") == set()     # a season pack name
    assert series.episodes_named("Season 9") == set()


def test_the_library_folder_counts_whatever_its_layout(tmp_path):
    rel = tmp_path / "Season 9" / "Show.2016.S02E03.720p.HDTV.x264-GRPA"
    rel.mkdir(parents=True)
    (rel / "show.2016.s02e03.720p.hdtv.x264-grpa.r00").write_bytes(b"x")
    (tmp_path / "Specials").mkdir()
    (tmp_path / "Specials" / "show.s01e05.mkv").write_bytes(b"x")
    have, where = series.library_episodes(str(tmp_path))
    assert have == {2: {3}, 1: {5}}


class QB:
    def torrents(self):
        return [{"name": "Show.2016.S02E04.720p.HDTV.x264-GRPA", "hash": "a"},
                {"name": "Show.2016.S03.1080p.WEB.h264-GRPB", "hash": "b"},
                {"name": "Show.1998.S02E05.576p.x264-OLD", "hash": "c"},       # the other show of that name
                {"name": "Show.Extra.S02E06.720p-GRPA", "hash": "d"},          # a longer title
                {"name": "Other.S02E07.720p-GRPA", "hash": "e"}]

    def files(self, h):
        assert h == "b"
        return [{"name": "Show.2016.S03.1080p.WEB.h264-GRPB/show.2016.s03e01.mkv"},
                {"name": "Show.2016.S03.1080p.WEB.h264-GRPB/show.2016.s03e02.mkv"}]


def test_qbittorrent_counts_this_show_only():
    have, _ = series.qbit_episodes(QB(), "show", 2016)
    assert have == {2: {4}, 3: {1, 2}}


def test_same_show():
    assert same_show("Show.2016.S01E01.720p-GRPA", "show", 2016)
    assert same_show("Show.S01E01.720p-GRPA", "show", 2016)        # no year: counts
    assert not same_show("Show.1998.S01E01.720p-GRPA", "show", 2016)
    assert not same_show("Show.Extra.S01E01.720p-GRPA", "show")


def rel(title):
    return Release(title, "usenet", "IndexerA", 1, 100, title, "", "", "", 1, None, None)


def opt(group, res, eps=(), season_nzb=False):
    o = Option("show.2016.", group, res)
    if season_nzb:
        o.seasons.append(rel(f"Show.2016.S01.{res}-{group}"))
    for e in eps:
        o.episodes[e] = [rel(f"Show.2016.S01E{e:02d}.{res}-{group}")]
    return o


def test_one_group_for_the_series_and_a_same_resolution_fallback():
    s1 = series.SeasonPlan(1, [1, 2, 3], {1})
    s1.options = [opt("grpa", "720p", season_nzb=True), opt("grpb", "1080p", [2, 3])]
    s2 = series.SeasonPlan(2, [1, 2], set())
    s2.options = [opt("grpc", "720p", [1, 2]), opt("grpb", "1080p", [1])]
    s3 = series.SeasonPlan(3, [1, 2], {1, 2})                       # nothing missing
    s4 = series.SeasonPlan(4, [1], set())
    s4.options = [opt("grpb", "1080p", [1])]                        # nothing at 720p
    best = series.choose([s1, s2, s3, s4])
    assert best == ("grpa", "720p") or best == ("grpb", "1080p")
    best = series.choose([s1, s2, s3, s4], prefer_res="720p")
    assert best == ("grpa", "720p")
    assert s1.choice.group == "grpa"
    assert s2.choice.group == "grpc" and "most complete" in s2.why
    assert s3.choice is None and s3.why == "nothing missing"
    assert s4.choice is None and "nothing at 720p" in s4.why
    s5 = series.SeasonPlan(5, [1, 2], set())                        # no options at all
    series.choose([s1, s2, s5], prefer_res="720p")
    assert s5.choice is None and s5.why == "not on Usenet"


def test_the_folder_chooser_stays_inside_the_data_folders(tmp_path):
    data = tmp_path / "data"
    (data / "tv" / "Show").mkdir(parents=True)
    cfg = Config(path=str(tmp_path / "c.toml"), local_to_qbit=[[str(data), "/data"]])
    assert gui.data_roots(cfg) == [os.path.realpath(data)]
    assert gui.within_roots(cfg, str(data / "tv" / "Show"))
    assert not gui.within_roots(cfg, str(tmp_path))
    assert not gui.within_roots(cfg, str(data / "tv" / ".." / ".." ))


def test_a_same_named_older_show_in_the_folder_is_left_out(tmp_path):
    old = tmp_path / "Show (1998) S01-S07" / "Season 01"
    old.mkdir(parents=True)
    (old / "S01E01 - Heat A.mkv").write_bytes(b"x")
    (old / "S01E02 - Heat B.mkv").write_bytes(b"x")
    new = tmp_path / "Season 8" / "Show.2016.S01.720p.BluRay.x264-GRPA"
    new.mkdir(parents=True)
    (new / "Show.2016.S01E03.720p.BluRay.x264-GRPA.mkv").write_bytes(b"x")
    (tmp_path / "Show (2016)").mkdir()
    (tmp_path / "Show (2016)" / "S02E01.mkv").write_bytes(b"x")
    have, _ = series.library_episodes(str(tmp_path), "show", 2016)
    assert have == {1: {3}, 2: {1}}
    assert same_show("Show (2016) S02", "show", 2016) and not same_show("Show (1998) S01-S07", "show", 2016)


def test_the_same_named_older_show_and_nameless_resolutions_do_not_win():
    old = Option("show.", "btn", None)                       # 'Show.S01.WEB-DL-BTN': the 1998 show
    old.seasons.append(rel("Show.S01.WEB-DL-BTN"))
    s1 = series.SeasonPlan(1, [1, 2], set())
    s1.options = [old, opt("grpa", "720p", [1, 2]), opt("grpb", None, [1, 2])]
    series.this_show([s1], 2016)
    assert [o.group for o in s1.options] == ["grpa", "grpb"]
    assert series.choose([s1]) == ("grpa", "720p")


def test_and_spellings_are_one_show(tmp_path):
    from nzb2seed import matching
    from nzb2seed.season import show_names
    key = matching.norm("Tom & Jess")
    assert same_show("Tom.En.Jess.S01E14.DUBBED.1080p.WEB.h264-GRPA", key)
    assert same_show("Tom.and.Jess.S01E14.1080p-GRPA", key)
    assert not same_show("Tom.En.Jess.Winter.S01E49.DUBBED.1080p.WEB.h264-GRPA", key)   # the spin-off
    assert show_names("Tom & Jess") == ["Tom & Jess", "Tom en Jess", "Tom and Jess"]
    (tmp_path / "Tom.En.Jess.S01E14.DUBBED.1080p.WEB.h264-GRPA").mkdir()
    (tmp_path / "Tom.En.Jess.Winter.S01E49.DUBBED.1080p.WEB.h264-GRPA").mkdir()
    have, _ = series.library_episodes(str(tmp_path), key, 2020)
    assert have == {1: {14}}


def test_a_priority_list_fills_each_season_from_several_releases():
    s1 = series.SeasonPlan(1, [1, 2, 3, 4], {4})
    s1.options = [opt("grpa", "720p", [1]), opt("grpb", "480p", [1, 2]), opt("grpc", "720p", [3])]
    s2 = series.SeasonPlan(2, [1, 2], set())
    s2.options = [opt("grpb", "480p", [2])]
    series.choose_priority([s1, s2], [("grpa", "720p"), ("grpc", "720p"), ("grpb", "480p")])
    assert [o.group for o in s1.choices] == ["grpa", "grpc", "grpb"] and s1.covered() == 3
    assert [o.group for o in s2.choices] == ["grpb"] and s2.covered() == 1
    out = series.summary([s1, s2])
    grpb = next(r for r in out["options"] if r["key"] == "grpb|480p")
    assert grpb["episodes"] == {1: [1, 2], 2: [2]} and grpb["total"] == 3


def test_without_a_list_gaps_are_filled_at_the_same_resolution():
    s1 = series.SeasonPlan(1, [1, 2, 3], set())
    s1.options = [opt("grpa", "720p", [1, 2]), opt("grpc", "720p", [3]), opt("grpb", "480p", [1, 2, 3])]
    series.choose([s1], prefer_res="720p")
    assert [o.group for o in s1.choices] == ["grpa", "grpc"] and s1.covered() == 3
    assert "gaps filled" in s1.why
    assert series.parse_pair("GRPA|720p") == ("grpa", "720p") and series.parse_pair("GRPB|") == ("grpb", None)
