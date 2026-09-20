"""Posts and files named only by episode title are placed by the episode's name."""
from nzb2seed import matching, season, series
from nzb2seed.clients import Release
from nzb2seed.config import Config

NAMES = {(1, 14): {"De Dierenwinkel"}, (1, 15): {"De Kermis"}, (2, 3): {"De Kermis 2"},
         (1, 16): {"Het Café"}, (1, 17): {"Pech"},                     # too short to trust
         (2, 1): {"De Brand"}, (3, 1): {"De Brand"}}                   # one name, two episodes
SHOW = matching.norm("Tom & Jess")


def idx():
    return season.NameIndex(NAMES)


def test_titles_are_placed_by_the_episode_name():
    n = idx()
    assert n.match("Tom En Jess De Dierenwinkel FLEMISH - GRPA", SHOW) == (1, 14)
    assert n.match("Tom.And.Jess.Het.Cafe.DUTCH.XviD-GRPA", SHOW) == (1, 16)    # accents do not matter
    assert n.match("Tom En Jess De Kermis 2 FLEMISH - GRPA", SHOW) == (2, 3)    # the longest name wins
    assert n.match("Tom En Jess De Kermis FLEMISH - GRPA", SHOW) == (1, 15)


def test_what_is_not_matched():
    n = idx()
    assert n.match("Tom En Jess De Brand FLEMISH - GRPA", SHOW) is None         # two episodes of that name
    assert n.match("Tom En Jess Pech FLEMISH - GRPA", SHOW) is None             # too short
    assert n.match("Tom.En.Jess.S01E14.De.Dierenwinkel.720p-GRPA", SHOW) is None  # numbered: the usual way
    assert n.match("Other Show De Dierenwinkel - GRPA", SHOW) is None
    assert season.named_group("Tom En Jess De Dierenwinkel FLEMISH - GRPA") == "grpa"


def rel(title, guid):
    return Release(title, "usenet", "IndexerA", 1, 10**6, guid, "", "", "", 1, None, None)


class PR:
    def __init__(self, results):
        self.results = results

    def search(self, q, *a):
        return list(self.results)


def test_named_posts_become_a_release_of_their_season():
    results = [rel("Tom En Jess De Dierenwinkel FLEMISH - GRPA", "a"),
               rel("Tom En Jess De Kermis FLEMISH - GRPA", "b"),
               rel("Tom En Jess De Kermis 2 FLEMISH - GRPA", "c"),
               rel("Tom.En.Jess.S01E01.720p.WEB.h264-GRPB", "d")]
    opts = season.find_options(Config(path="x"), PR(results), "Tom & Jess", 1, list(range(1, 20)), idx())
    by = {o.group: o for o in opts}
    assert sorted(by["grpa"].episodes) == [14, 15] and sorted(by["grpb"].episodes) == [1]
    assert by["grpa"].prefix == "tom.en.jess."
    # without the index, the named posts are not placed at all
    assert {o.group for o in season.find_options(Config(path="x"), PR(results), "Tom & Jess", 1,
                                                 list(range(1, 20)))} == {"grpb"}


def test_library_files_named_after_the_episode_count_as_yours(tmp_path):
    (tmp_path / "Tom En Jess - De Dierenwinkel.avi").write_bytes(b"x")
    (tmp_path / "Tom.En.Jess.S02E01.mkv").write_bytes(b"x")
    have, _ = series.library_episodes(str(tmp_path), SHOW, None, idx())
    assert have == {1: {14}, 2: {1}}


def test_name_matching_is_an_opt_in_setting(monkeypatch):
    from nzb2seed import episodes
    monkeypatch.setattr(episodes, "all_names", lambda show: NAMES)
    cfg = Config(path="x")
    assert cfg.match_episode_names is False and season.names_for(cfg, {"id": 1}) is None
    cfg.match_episode_names = True
    assert season.names_for(cfg, {"id": 1}).match("Tom En Jess De Dierenwinkel - GRPA", SHOW) == (1, 14)
