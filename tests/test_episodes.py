"""Episode lists from several sources, the longest per season, cached on disk."""
import gzip
import os

import pytest

from nzb2seed import episodes, metadata

NULL = chr(92) + "N"                    # IMDb's "no value"
SHOW = {"id": 7, "name": "Tom & Jess", "premiered": "2020-09-28", "imdb": "tt0000007", "tvdb": 70}

TMDB_SEARCH = ('<a href="/tv/555-tom-jess-winter">x</a><span class="release_date">1 september 2022</span>'
               '<a href="/tv/111-tom-jess">x</a><span class="release_date">28 september 2020</span>')
TMDB_SEASONS = '<a href="/tv/111-tom-jess/season/1">Season 1</a>'
TMDB_S1 = "".join(f'<h3><a class="no_click" data-episode-number="{n}" data-season-number="1" href="#">Ep &amp; {n}</a></h3>'
                  for n in range(1, 6))
TVDB_DEREF = '<a href="/series/tom-jess">x</a><a href="/series/tom-jess/allseasons">y</a><a href="/series/create">z</a>'
TVDB_ALL = "".join(f'<span class="text-muted episode-label">S{s:02d}E{n:02d}</span> <a href="#"> Name {s}.{n} </a>'
                   f'</h4><ul><li>September {n}, 2020</li></ul>' for s, k in ((1, 5), (2, 3)) for n in range(1, k + 1))


@pytest.fixture
def web(tmp_path, monkeypatch):
    pages = {"/search/tv": TMDB_SEARCH, "/tv/111/seasons": TMDB_SEASONS, "/tv/111/season/1": TMDB_S1,
             "/dereferrer/series/70": TVDB_DEREF, "/series/tom-jess/allseasons/official": TVDB_ALL}
    asked = []

    def get_text(url):
        asked.append(url)
        for k, v in pages.items():
            if k in url:
                return v, False, None
        return None, False, "not found"
    monkeypatch.setattr(metadata, "get_text", get_text)
    monkeypatch.setattr(metadata, "tvmaze_seasons", lambda i: [{"number": 1}])
    monkeypatch.setattr(metadata, "tvmaze_episodes", lambda i, s: [{"number": 1, "name": "One", "airdate": "2020-09-28"}])
    episodes.configure_cache(str(tmp_path / "nzb2seed.toml"))
    dataset = os.path.join(episodes.CACHE_DIR, "imdb-title.episode.tsv.gz")
    os.makedirs(episodes.CACHE_DIR)
    with gzip.open(dataset, "wt", encoding="utf-8") as fh:
        fh.write("tconst\tparentTconst\tseasonNumber\tepisodeNumber\n")
        for n in range(1, 4):
            fh.write(f"tt10{n}\ttt0000007\t2\t{n}\n")
        fh.write("tt200\ttt0000007\t" + NULL + "\t" + NULL + "\n")     # an episode without a number
        fh.write("tt300\ttt9999999\t1\t1\n")
    return asked


def test_each_source(web):
    assert [e["number"] for e in episodes.tvmaze(SHOW)[1]] == [1]
    tm = episodes.tmdb(SHOW)
    assert list(tm) == [1] and len(tm[1]) == 5 and tm[1][0]["name"] == "Ep & 1"
    tv = episodes.tvdb(SHOW)
    assert {s: len(v) for s, v in tv.items()} == {1: 5, 2: 3}
    assert tv[1][1] == {"number": 2, "name": "Name 1.2", "airdate": "2020-09-02"}
    assert {s: len(v) for s, v in episodes.imdb(SHOW).items()} == {2: 3}


def test_all_sources_take_the_longest_list_per_season(web):
    lists, used = episodes.episode_lists(SHOW, "all")
    assert {s: len(v) for s, v in lists.items()} == {1: 5, 2: 3}
    assert used[1] == "tvdb"            # 5 each from TMDB and TheTVDB: the one with air dates
    assert used[2] == "tvdb"            # a tie with IMDb's 3: TheTVDB comes first


def test_lookups_are_cached_and_can_be_cleared(web):
    episodes.episode_lists(SHOW, "all")
    n = len(web)
    episodes.episode_lists(SHOW, "all")
    assert len(web) == n                 # nothing asked twice
    assert episodes.clear_cache() > 0
    assert not os.path.exists(episodes.CACHE_DIR)
    assert episodes.clear_cache() == 0


def test_a_source_that_cannot_answer_is_reported_not_fatal(web, monkeypatch):
    monkeypatch.setattr(metadata, "get_text", lambda url: (None, False, "Cloudflare bot check"))
    notes = []
    lists, used = episodes.episode_lists({**SHOW, "id": 8}, "all", log=notes.append)
    assert used == {1: "tvmaze", 2: "imdb"}
    assert any("TMDB" in x for x in notes) and any("TheTVDB" in x for x in notes)
