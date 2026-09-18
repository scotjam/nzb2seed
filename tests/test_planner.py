"""Finding NZBs closest match first: multi-season -> season -> episode."""
import os
import re

import pytest

from nzb2seed import assemble as asm
from nzb2seed import pipeline
from nzb2seed.clients import Release
from nzb2seed.config import Config
from nzb2seed.pipeline import Abort, Options, build_units, plan_and_fetch
from nzb2seed.torrent import parse

from helpers import make_torrent, rnd

SHOW = "Show.2020"


def ep_name(s, e, grp="GRP"):
    return f"show.2020.s{s:02d}e{e:02d}.720p.hdtv.x264-{grp.lower()}.mkv"


def multi_season(extra_group=None):
    """Seasons 1 and 2, three episodes each (+ an .nfo per season)."""
    files, n = {}, 0
    for s in (1, 2):
        for e in (1, 2, 3):
            n += 1
            grp = extra_group if (extra_group and s == 2 and e == 2) else "GRP"
            files[f"Season {s}/{ep_name(s, e, grp)}"] = rnd(40_000 + n * 1000, n)
        files[f"Season {s}/show.2020.s{s:02d}.nfo"] = b"nfo %d\r\n" % s
    return files


def rel(title, size, guid=None, grabs=0):
    return Release(title, "usenet", "idx", 1, size, guid or f"{title}|{size}", "", "", "", grabs, None, None)


class SAB:
    """Writes, for each NZB, the torrent files its title stands for (whole / season / episode);
    ``lacks`` removes files from a post, ``failing`` makes a post fail."""

    def __init__(self, root, t, files, lacks=None, failing=()):
        self.root, self.t, self.files = root, t, files
        self.lacks, self.failing = lacks or {}, set(failing)
        self.jobs, self.added = {}, []

    def add_nzb(self, nzb, name, cat, pp, prio):
        guid = nzb.decode().split("guid=")[1].split("-->")[0]
        nzo = f"nzo{len(self.jobs) + 1}"
        self.jobs[nzo] = guid
        self.added.append(guid)
        if guid in self.failing:
            return nzo
        d = os.path.join(self.root, nzo)
        os.makedirs(d)
        n = name.lower()
        ep = pipeline._EP.search(n)
        season = [s for s in (1, 2) if re.search(rf"[.-]s{s:02d}[.-]", n)]
        for rel_, data in self.files.items():
            base = rel_.split("/")[-1]
            if ep and ep.group(0) not in base:
                continue
            if not ep and len(season) == 1 and f"season {season[0]}" not in rel_.lower():
                continue
            if base in self.lacks.get(guid, ()):
                continue
            with open(os.path.join(d, base), "wb") as fh:
                fh.write(data)
        return nzo

    def ensure_pp(self, nzo, pp):
        return "+Repair"

    def status(self, nzo):
        guid = self.jobs[nzo]
        if guid in self.failing:
            return "Failed", {"fail_message": "Aborted"}
        return "Completed", {"storage": f"/dl/{nzo}"}


class PR:
    def __init__(self, results):
        self.results, self.queries = results, []

    def fetch(self, r):
        return f"<?xml version='1.0'?><nzb/><!--guid={r.guid}-->".encode()

    def search(self, query, *a):
        self.queries.append(query)
        return list(self.results)


def setup(tmp_path, files, name="Show.2020.S01-S02.720p.HDTV.x264-GRP"):
    t = parse(make_torrent(name, files))
    cfg = Config(path=str(tmp_path / "c.toml"), sab_to_local=[["/dl", str(tmp_path / "dl")]])
    return t, cfg


def season_size(files, s):
    return sum(len(v) for k, v in files.items() if k.startswith(f"Season {s}/"))


def ep_size(files, s, e):
    return len(next(v for k, v in files.items() if f"s{s:02d}e{e:02d}" in k))


# ---------------------------------------------------------------- the unit tree

def test_tree_for_each_kind_of_torrent():
    multi = parse(make_torrent("Show.2020.S01-S02.720p.HDTV.x264-GRP", multi_season()))
    root = build_units(multi, multi.name)
    assert root.level == "whole" and [c.need.label for c in root.children] == ["S01", "S02"]
    assert [c.need.label for c in root.children[1].children] == ["S02E01", "S02E02", "S02E03"]
    assert {f.name for f in root.children[0].files} >= {"show.2020.s01.nfo"}   # season extras

    season = parse(make_torrent("Show.2020.S01.720p.HDTV.x264-GRP",
                                {ep_name(1, e): rnd(40_000, e) for e in (1, 2)}))
    root = build_units(season, season.name)
    assert root.level == "season" and len(root.children) == 2

    episode = parse(make_torrent("Show.2020.S01E01.720p.HDTV.x264-GRP", {ep_name(1, 1): rnd(40_000, 1)}))
    assert build_units(episode, episode.name).level == "episode"

    movie = parse(make_torrent("Movie.2021.1080p.BluRay.x264-GRP", {"movie.mkv": rnd(40_000, 1)}))
    root = build_units(movie, movie.name)
    assert root.level == "whole" and not root.children and root.need.show == "movie.2021."


# ---------------------------------------------------------------- closest match first

def run(tmp_path, t, cfg, sab, pr, picks=()):
    return plan_and_fetch(cfg, Options(), pr, sab, t, t.name, list(picks), 2)


def covers_all(t, dirs):
    return set(asm.find_sources(t, dirs)) == {f.relpath for f in t.real_files}


def test_multi_season_nzb_is_used_when_there_is_one(tmp_path):
    files = multi_season()
    t, cfg = setup(tmp_path, files)
    whole = rel("Show.2020.S01-S02.720p.HDTV.x264-GRP", t.total_size + 10)
    season = rel("Show.2020.S01.720p.HDTV.x264-GRP", season_size(files, 1) + 10)
    sab = SAB(str(tmp_path / "dl"), t, files)
    dirs, _, _, _ = run(tmp_path, t, cfg, sab, PR([season, whole]))
    assert sab.added == [whole.guid] and covers_all(t, dirs)


def test_seasons_then_episodes_when_nothing_bigger_exists(tmp_path):
    files = multi_season()
    t, cfg = setup(tmp_path, files)
    s1 = rel("Show.2020.S01.720p.HDTV.x264-GRP", season_size(files, 1) + 10)
    # no season-2 NZB: season 2 comes from its episodes
    eps2 = [rel(f"Show.2020.S02E0{e}.720p.HDTV.x264-GRP", ep_size(files, 2, e) + 10) for e in (1, 2, 3)]
    sab = SAB(str(tmp_path / "dl"), t, files, lacks={})
    # the season .nfo of season 2 only exists in a season post: expect it to be missing
    dirs, _, units, _ = run(tmp_path, t, cfg, sab, PR([s1] + eps2))
    assert sab.added == [s1.guid] + [e.guid for e in eps2]
    found = set(asm.find_sources(t, dirs))
    assert "Show.2020.S01-S02.720p.HDTV.x264-GRP/Season 2/show.2020.s02.nfo" not in found
    assert len(found) == len(t.real_files) - 1


def test_every_option_at_a_level_is_tried_before_going_down(tmp_path):
    files = multi_season()
    t, cfg = setup(tmp_path, files)
    size1 = season_size(files, 1)
    bad = [rel("Show.2020.S01.720p.HDTV.x264-GRP", size1 + 10 + i, f"s1-{i}") for i in range(4)]
    good = rel("Show.2020.S01.720p.HDTV.x264-GRP", size1 + 99, "s1-good")
    s2 = rel("Show.2020.S02.720p.HDTV.x264-GRP", season_size(files, 2) + 10, "s2")
    sab = SAB(str(tmp_path / "dl"), t, files, failing={b.guid for b in bad})
    dirs, _, _, _ = run(tmp_path, t, cfg, sab, PR(bad + [good, s2]))
    # four failing season-1 posts, then the good one - never an episode NZB
    assert set(sab.added) == {b.guid for b in bad} | {"s1-good", "s2"} and covers_all(t, dirs)


def test_a_short_season_post_still_supplies_its_episodes(tmp_path):
    files = multi_season()
    t, cfg = setup(tmp_path, files)
    s1 = rel("Show.2020.S01.720p.HDTV.x264-GRP", season_size(files, 1) + 10, "s1")
    s2 = rel("Show.2020.S02.720p.HDTV.x264-GRP", season_size(files, 2) + 10, "s2-short")
    e22 = rel("Show.2020.S02E02.720p.HDTV.x264-GRP", ep_size(files, 2, 2) + 10, "e22")
    sab = SAB(str(tmp_path / "dl"), t, files, lacks={"s2-short": {ep_name(2, 2)}})
    dirs, _, _, _ = run(tmp_path, t, cfg, sab, PR([s1, s2, e22]))
    # season 2 lacked E02: E01, E03 and the .nfo come from that download, only E02 separately
    assert sab.added == ["s1", "s2-short", "e22"] and covers_all(t, dirs)


def test_mixed_group_season_goes_straight_to_episodes(tmp_path):
    files = multi_season(extra_group="OTHER")
    t, cfg = setup(tmp_path, files)
    s1 = rel("Show.2020.S01.720p.HDTV.x264-GRP", season_size(files, 1) + 10, "s1")
    s2 = rel("Show.2020.S02.720p.HDTV.x264-GRP", season_size(files, 2) + 10, "s2-grp")
    eps2 = [rel(f"Show.2020.S02E0{e}.720p.HDTV.x264-{'OTHER' if e == 2 else 'GRP'}",
                ep_size(files, 2, e) + 10, f"e2{e}") for e in (1, 2, 3)]
    sab = SAB(str(tmp_path / "dl"), t, files)
    dirs, _, _, _ = run(tmp_path, t, cfg, sab, PR([s1, s2] + eps2))
    assert "s2-grp" not in sab.added                   # season 2 mixes GRP and OTHER
    assert {"e21", "e22", "e23"} <= set(sab.added)


def test_other_groups_and_resolutions_are_never_tried(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "report_ask", lambda p, c: None)
    files = {ep_name(1, 1): rnd(40_000, 1)}
    t, cfg = setup(tmp_path, files, "Show.2020.S01E01.720p.HDTV.x264-GRP")
    wrong = [rel("Show.2020.S01E01.720p.HDTV.x264-OTHER", 10**6, "other-group"),
             rel("Show.2020.S01E01.1080p.HDTV.x264-GRP", 10**6, "other-res")]
    sab = SAB(str(tmp_path / "dl"), t, files)
    with pytest.raises(Abort):
        run(tmp_path, t, cfg, sab, PR(wrong))
    assert sab.added == []


def test_a_movie_uses_movie_nzbs(tmp_path):
    files = {"movie.2021.1080p.bluray.x264-grp.mkv": rnd(60_000, 3)}
    t, cfg = setup(tmp_path, files, "Movie.2021.1080p.BluRay.x264-GRP")
    good = rel("Movie.2021.1080p.BluRay.x264-GRP", 70_000, "movie")
    other = rel("Movie.2021.2160p.BluRay.x264-GRP", 90_000, "uhd")
    sab = SAB(str(tmp_path / "dl"), t, files)
    dirs, _, _, _ = run(tmp_path, t, cfg, sab, PR([other, good]))
    assert sab.added == ["movie"] and covers_all(t, dirs)
