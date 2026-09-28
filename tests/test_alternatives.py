"""When a post cannot be completed: same group, same episode, big enough - or ask."""
import os

import pytest

from nzb2seed import pipeline, report
from nzb2seed.clients import Release
from nzb2seed.config import Config
from nzb2seed.pipeline import Abort, group_of, need_for
from nzb2seed.torrent import parse

from helpers import make_torrent, rnd

PACK = "Show.2016.S03.720p.HDTV.AAC2.0.x264-Scene [NO RAR]"


def pack_torrent():
    files = {f"show.2016.s03e0{i}.720p.hdtv.x264-{'grpb' if i == 4 else 'grpa'}.mkv": rnd(40_000 + i * 1000, i)
             for i in range(1, 5)}
    return parse(make_torrent(PACK, files)), files


def rel(title, size, guid=None, grabs=0, indexer="idx"):
    return Release(title, "usenet", indexer, 1, size, guid or title + str(size), "", "", "", grabs, None, None)


@pytest.mark.parametrize("name,group", [
    ("Show.2016.S03E02.720p.HDTV.x264-GRPA-xRePo", "grpa"),
    ("show.2016.s03e02.720p.hdtv.x264-grpa.mkv", "grpa"),
    ("Show.2016.S03E02.720p.iP.WEBRip.AAC2.0.H264-GRPC-xpost", "grpc"),
    ("Some-Title.No.Way.Out.2021.1080p.BluRay.x264-GRP", "grp"),
    ("no group here", None),
    ("Show.2016.S03E03.720p.iP.WEB-DL.AAC2.0.H.264-GRPC", "grpc"),          # WEB-DL is not a group
    ("Show S03 - Heat B", None),                                            # a description
    ("Show.S03.First.World.Championship.WEB-DL", None),
    ("Movie.2021.1080p.Blu-ray.Remux.AVC.DTS-HD.MA.5.1-GRP", "grp"),
    # the group closing a bracketed description, file or folder
    ("Show (2022) S02E01 (1080p ATVP WEB-DL H265 SDR DDP Atmos 5.1 English - GRPH).mkv", "grph"),
    ("Show (2022) S02 (1080p ATVP WEB-DL H265 SDR DDP Atmos 5.1 English - GRPH)", "grph"),
    ("Show (2022) S02 (Part 1 - Heat B) Extras", None),                     # a description inside
    # the same as posted on Usenet: dots for spaces, and the poster's -xpost after it
    ("Show.(2022).S01E01.(1080p.ATVP.WEB-DL.H265.SDR.DDP.Atmos.5.1.English.-.GRPH).mkv-xpost", "grph"),
])
def test_group_of(name, group):
    assert group_of(name) == group


def test_need_for_an_episode_of_a_pack():
    t, files = pack_torrent()
    need = need_for(t, "Show.2016.S03E02.720p.HDTV.x264-GRPA-xRePo", whole_torrent=False)
    assert need.ep == "s03e02" and need.group == "grpa" and need.res == "720p"
    assert need.min_size == len(files["show.2016.s03e02.720p.hdtv.x264-grpa.mkv"])
    assert need.query == "show 2016 s03e02"
    # E04 is GRPB in the torrent: that is the group an E04 replacement must come from
    assert need_for(t, "Show.2016.S03E04.720p.HDTV.x264-GRPA", False).group == "grpb"


def test_find_alternatives_filters(tmp_path):
    t, files = pack_torrent()
    need = need_for(t, "Show.2016.S03E02.720p.HDTV.x264-GRPA-xRePo", False)
    big = need.min_size + 10
    failed = rel("Show.2016.S03E02.720p.HDTV.x264-GRPA-xRePo", big, "failed")
    results = [
        failed,
        # the same size is not proof of the same post (one may carry a file the other lacks):
        # that is decided from the NZB's message-IDs when the post comes up
        rel("Show.2016.S03E02.720p.HDTV.x264-GRPA", big, "same-size-elsewhere"),
        rel("Show.2016.S03E02.720p.HDTV.x264-GRPA", big + 5, "good", grabs=9),
        rel("Show.2016.S03E02.720p.HDTV.x264-GRPA", need.min_size - 1, "too-small"),
        rel("Show.2016.S03E02.720p.iP.WEBRip.AAC2.0.H264-GRPC", big + 9, "other-group"),
        rel("Show.2016.S03E02.1080p.HDTV.x264-GRPA", big + 9, "other-res"),
        rel("Show.2016.S03E03.720p.HDTV.x264-GRPA", big + 9, "other-ep"),
    ]
    from test_usenet import FakeProwlarr
    alts = pipeline.find_alternatives(Config(path=str(tmp_path / "c.toml")), FakeProwlarr({}, results), need, [failed])
    assert [r.guid for r in alts] == ["good", "same-size-elsewhere"]


class PackSAB:
    """Fails the given NZB titles; completes the others by writing the episode's file."""

    def __init__(self, root, files, failing):
        self.root, self.files, self.failing = root, files, set(failing)
        self.n, self.jobs, self.added = 0, {}, []

    def add_nzb(self, nzb, name, cat, pp, prio):
        self.n += 1
        nzo = f"nzo{self.n}"
        self.jobs[nzo] = name
        self.added.append(name)
        if name not in self.failing:
            d = os.path.join(self.root, f"{name}.{self.n}")
            os.makedirs(d)
            ep = pipeline._EP.search(name.lower()).group(0)
            fname = next(f for f in self.files if ep in f)
            with open(os.path.join(d, fname), "wb") as fh:
                fh.write(self.files[fname])
        return nzo

    def ensure_pp(self, nzo, pp):
        return "+Repair/Unpack"

    def status(self, nzo):
        name = self.jobs[nzo]
        if name in self.failing:
            return "Failed", {"fail_message": "Aborted, cannot be completed"}
        return "Completed", {"storage": f"/dl/{name}.{nzo[3:]}"}


class NZBsOK:
    def __init__(self, results=()):
        self.results = list(results)

    def fetch(self, r):
        return b"<?xml version='1.0'?><nzb/>"

    def search(self, query, *a):
        return [r for r in self.results if pipeline._EP.search(query.replace(" ", ".")).group(0)
                in r.title.lower()]


def with_others(files, *picked):
    """The given picks plus working picks for every other episode of the pack."""
    eps = {pipeline._EP.search(g[0].title.lower()).group(0) for g in picked}
    sizes = {i: len(next(v for k, v in files.items() if f"s03e0{i}" in k)) for i in range(1, 5)}
    rest = [[rel(f"Show.2016.S03E0{i}.720p.HDTV.x264-{'GRPB' if i == 4 else 'GRPA'}",
                 sizes[i] + 100, f"ok{i}")] for i in range(1, 5) if f"s03e0{i}" not in eps]
    return list(picked) + rest


def test_failed_episode_is_replaced_automatically(tmp_path):
    t, files = pack_torrent()
    sizes = {i: len(next(v for k, v in files.items() if f"s03e0{i}" in k)) for i in range(1, 5)}
    picked = [[rel(f"Show.2016.S03E0{i}.720p.HDTV.x264-{'GRPB' if i == 4 else 'GRPA'}"
                   + ("-xRePo" if i == 2 else ""), sizes[i] + 100)] for i in range(1, 5)]
    alt = rel("Show.2016.S03E02.720p.HDTV.x264-GRPA", sizes[2] + 200, "alt", grabs=5)
    sab = PackSAB(str(tmp_path), files, failing={"Show.2016.S03E02.720p.HDTV.x264-GRPA-xRePo"})
    cfg = Config(path=str(tmp_path / "c.toml"), sab_to_local=[["/dl", str(tmp_path)]])
    dirs, nzos = pipeline.usenet_multi(cfg, NZBsOK([alt]), sab, t, picked, 2)
    assert len(dirs) == 4 and "Show.2016.S03E02.720p.HDTV.x264-GRPA" in sab.added


def test_asks_when_nothing_fits_and_uses_the_pick(tmp_path, monkeypatch):
    t, files = pack_torrent()
    size2 = len(next(v for k, v in files.items() if "s03e02" in k))
    failing = "Show.2016.S03E02.720p.HDTV.x264-GRPA-xRePo"
    same = rel("Show.2016.S03E02.720p.HDTV.x264-GRPA-xpost", size2 - 50, "same")  # same group and res, too small
    other = rel("Show.2016.S03E02.1080p.HDTV.x264-GRPA", size2 + 50, "other")   # same group, other res
    foreign = rel("Show.2016.S03E02.720p.HDTV.x264-OTHERGRP", size2 + 60, "foreign")
    hidden = rel("Show.2016.S03E02.HDTV", size2 + 70, "hidden")      # a name showing no group and no res
    asked = []

    def fake_ask(prompt, choices):
        asked.append((prompt, choices))
        return next(i for i, c in enumerate(choices) if c["title"] == same.title)
    monkeypatch.setattr(pipeline, "report_ask", fake_ask)
    sab = PackSAB(str(tmp_path), files, failing={failing})
    cfg = Config(path=str(tmp_path / "c.toml"), sab_to_local=[["/dl", str(tmp_path)]])
    pipeline.usenet_multi(cfg, NZBsOK([same, other, foreign, hidden]), sab, t,
                          with_others(files, [rel(failing, size2 + 100)]), 2)
    assert asked and "S03E02" in asked[0][0] and "GRPA" in asked[0][0]
    # another group or another resolution is not offered; a name that shows neither is
    assert sorted(c["title"] for c in asked[0][1]) == sorted([same.title, hidden.title])
    assert same.title in sab.added


def test_declining_the_question_stops_the_build(tmp_path, monkeypatch):
    t, files = pack_torrent()
    monkeypatch.setattr(pipeline, "report_ask", lambda p, c: None)
    failing = "Show.2016.S03E02.720p.HDTV.x264-GRPA-xRePo"
    sab = PackSAB(str(tmp_path), files, failing={failing})
    with pytest.raises(Abort):
        pipeline.usenet_multi(Config(path=str(tmp_path / "c.toml"), sab_to_local=[["/dl", str(tmp_path)]]),
                              NZBsOK([rel("Show.2016.S03E02.720p.WEB.x264-X", 10)]), sab, t,
                              [[rel(failing, 10**6)]], 2)


def test_console_does_not_ask_without_a_terminal(capsys):
    report.interactive = False
    assert report.ask("pick", [{"title": "A", "indexer": "i", "size_text": "1 MB", "grabs": 1}]) is None
    assert "not asking" in capsys.readouterr().out


# ---------------------------------------------------------------- reusing finished downloads

def picks_for(files, fail_e02=False):
    sizes = {i: len(next(v for k, v in files.items() if f"s03e0{i}" in k)) for i in range(1, 5)}
    return [[rel(f"Show.2016.S03E0{i}.720p.HDTV.x264-{'GRPB' if i == 4 else 'GRPA'}",
                 sizes[i] + 100)] for i in range(1, 5)]


def test_second_build_reuses_every_finished_download(tmp_path):
    t, files = pack_torrent()
    cfg = Config(path=str(tmp_path / "c.toml"), sab_to_local=[["/dl", str(tmp_path)]])
    sab = PackSAB(str(tmp_path), files, failing=set())
    first, _ = pipeline.usenet_multi(cfg, NZBsOK(), sab, t, picks_for(files), 2)
    assert len(sab.added) == 4
    again, nzos = pipeline.usenet_multi(cfg, NZBsOK(), sab, t, picks_for(files), 2)
    assert len(sab.added) == 4                     # nothing queued a second time
    assert sorted(again) == sorted(first) and len(nzos) == 4


def test_download_whose_files_are_gone_is_not_reused(tmp_path):
    t, files = pack_torrent()
    cfg = Config(path=str(tmp_path / "c.toml"), sab_to_local=[["/dl", str(tmp_path)]])
    sab = PackSAB(str(tmp_path), files, failing=set())
    first, _ = pipeline.usenet_multi(cfg, NZBsOK(), sab, t, picks_for(files), 2)
    e02 = next(d for d in first if "S03E02" in d)
    for f in os.listdir(e02):
        os.remove(os.path.join(e02, f))            # e.g. moved away by an earlier build
    pipeline.usenet_multi(cfg, NZBsOK(), sab, t, picks_for(files), 2)
    assert len(sab.added) == 5 and "S03E02" in sab.added[-1]


def test_a_download_still_in_progress_is_waited_for(tmp_path, monkeypatch):
    t, files = pack_torrent()
    cfg = Config(path=str(tmp_path / "c.toml"), sab_to_local=[["/dl", str(tmp_path)]])
    sab = PackSAB(str(tmp_path), files, failing=set())
    picks = picks_for(files)
    pipeline.ledger_for(cfg).record("nzoBUSY", picks[0][0].title, "", "idx", picks[0][0].size)
    polls = []

    def status(nzo, real=sab.status):
        if nzo == "nzoBUSY":
            polls.append(1)
            if len(polls) < 3:
                return "Queued:Downloading", {"percentage": "50"}
            d = os.path.join(str(tmp_path), "busy")
            os.makedirs(d, exist_ok=True)
            name = next(k for k in files if "s03e01" in k)
            with open(os.path.join(d, name), "wb") as fh:
                fh.write(files[name])
            return "Completed", {"storage": "/dl/busy"}
        return real(nzo)
    sab.status = status
    monkeypatch.setattr(pipeline.time, "sleep", lambda s: None)   # don't really wait between polls
    dirs, nzos = pipeline.usenet_multi(cfg, NZBsOK(), sab, t, picks, 2)
    assert "nzoBUSY" in nzos and any(d.endswith("busy") for d in dirs)
    assert picks[0][0].title not in sab.added            # E01 was not queued a second time


def test_a_post_that_failed_before_is_skipped_on_the_next_build(tmp_path):
    t, files = pack_torrent()
    cfg = Config(path=str(tmp_path / "c.toml"), sab_to_local=[["/dl", str(tmp_path)]])
    size2 = len(next(v for k, v in files.items() if "s03e02" in k))
    bad = rel("Show.2016.S03E02.720p.HDTV.x264-GRPA-xRePo", size2 + 100)
    good = rel("Show.2016.S03E02.720p.HDTV.x264-GRPA", size2 + 200, "alt")
    sab = PackSAB(str(tmp_path), files, failing={bad.title})
    picks = with_others(files, [bad])
    pipeline.usenet_multi(cfg, NZBsOK([good]), sab, t, picks, 2)
    assert bad.title in sab.added and good.title in sab.added
    sab.added.clear()
    pipeline.usenet_multi(cfg, NZBsOK([good]), sab, t, picks, 2)   # the rerun
    assert sab.added == []          # bad skipped (failed before), the rest reused


def test_season_nzb_plus_episode_nzb(tmp_path):
    """A season NZB that lacks one episode, plus that episode's own NZB."""
    t, files = pack_torrent()
    cfg = Config(path=str(tmp_path / "c.toml"), sab_to_local=[["/dl", str(tmp_path)]])

    class SeasonSAB(PackSAB):
        def add_nzb(self, nzb, name, cat, pp, prio):
            if pipeline._EP.search(name.lower()):
                return super().add_nzb(nzb, name, cat, pp, prio)
            self.n += 1
            nzo = f"nzo{self.n}"
            self.jobs[nzo] = name
            self.added.append(name)
            d = os.path.join(self.root, f"{name}.{self.n}")
            os.makedirs(d)
            for fname, data in files.items():
                if "s03e04" not in fname:                     # the season post lacks E04
                    with open(os.path.join(d, fname), "wb") as fh:
                        fh.write(data)
            return nzo
    sab = SeasonSAB(str(tmp_path), files, failing=set())
    size4 = len(next(v for k, v in files.items() if "s03e04" in k))
    groups = [[rel("Show.2016.S03.720p.HDTV.x264-GRPA", sum(map(len, files.values())))],
              [rel("Show.2016.S03E04.720p.HDTV.x264-GRPB", size4 + 10)]]
    slots = []
    dirs, _ = pipeline.usenet_multi(cfg, NZBsOK(), sab, t, groups, 2, slots)
    # the season NZB was tried for the whole pack first; it lacked E04, so E01-E03 came from it
    # and only E04 was downloaded on its own
    assert slots[0].level == "season" and slots[0].seeded == groups[0]
    assert sab.added == [groups[0][0].title, groups[1][0].title] and len(dirs) == 2
    from nzb2seed import assemble as asm
    assert set(asm.find_sources(t, dirs)) == {f.relpath for f in t.real_files}


# ---------------------------------------------------------------- only a small file short

class VideoOnlySAB:
    """Every post holds the film's video, under the post's own name - never its .nfo."""

    def __init__(self, root, video):
        self.root, self.video, self.n, self.jobs, self.added = root, video, 0, {}, []

    def add_nzb(self, nzb, name, cat, pp, prio):
        self.n += 1
        nzo = f"nzo{self.n}"
        self.jobs[nzo] = name
        self.added.append(name)
        d = os.path.join(self.root, f"{name}.{self.n}")
        os.makedirs(d)
        with open(os.path.join(d, name.replace(" ", ".") + ".mkv"), "wb") as fh:
            fh.write(self.video)
        return nzo

    def ensure_pp(self, nzo, pp):
        return "+Repair/Unpack"

    def status(self, nzo):
        return "Completed", {"storage": f"/dl/{self.jobs[nzo]}.{nzo[3:]}"}


class NoOtherPosts(NZBsOK):
    def search(self, query, *a):
        return []


def test_a_post_short_only_of_a_small_file_is_not_followed_by_another_whole_post(tmp_path):
    """A film: the first post held the whole 20.9 GB video and lacked only an 819-byte
    .nfo - and three more whole posts were downloaded looking for it. Now the first post
    is used and the build goes on (to stop nearly complete, the .nfo to come over
    BitTorrent) instead of downloading everything again."""
    film = "Film 2022 1080p BluRay REMUX-GRP"
    video = rnd(200_000, 7)
    t = parse(make_torrent(film, {f"{film}.mkv": video, f"{film}.mkv.nfo": b"n" * 800}))
    posts = [rel(film.replace(" ", "."), len(video) + 900, f"post{i}", indexer=f"idx{i}") for i in range(3)]
    sab = VideoOnlySAB(str(tmp_path), video)
    cfg = Config(path=str(tmp_path / "c.toml"), sab_to_local=[["/dl", str(tmp_path)]])
    dirs, nzos = pipeline.usenet_single(cfg, pipeline.Options(unattended=True), NoOtherPosts(), sab, t,
                                        posts, 2)
    assert len(sab.added) == 1                         # not three whole downloads
    assert len(dirs) == 1 and len(nzos) == 1


def test_a_post_short_of_real_content_still_moves_on(tmp_path):
    """Missing more than a small file (here: a second video) - the next post is tried."""
    film = "Film 2022 1080p BluRay REMUX-GRP"
    video = rnd(200_000, 7)
    t = parse(make_torrent(film, {f"{film}.mkv": video, f"{film}.extra.mkv": rnd(150_000, 8)}))
    posts = [rel(film.replace(" ", "."), 350_900 + i * 1000, f"post{i}", indexer=f"idx{i}") for i in range(2)]
    sab = VideoOnlySAB(str(tmp_path), video)
    cfg = Config(path=str(tmp_path / "c.toml"), sab_to_local=[["/dl", str(tmp_path)]])
    with pytest.raises(Abort):
        pipeline.usenet_single(cfg, pipeline.Options(unattended=True), NoOtherPosts(), sab, t, posts, 2)
    assert len(sab.added) == 2                         # both posts tried, as before


def test_a_retry_uses_what_the_last_run_downloaded_for_a_small_shortfall(tmp_path):
    """Tried twice, failed twice: the first run's post lacked only a sample, and the second
    skipped that post ("did not hold all of it on an earlier run") without using its
    download - so it had nothing at all. The download from before now counts."""
    film = "Film 2022 1080p BluRay REMUX-GRP"
    video = rnd(200_000, 7)
    t = parse(make_torrent(film, {f"{film}.mkv": video, f"Sample/{film}-sample.mkv": rnd(3_000, 9)}))
    posts = [rel(film.replace(" ", "."), len(video) + 3_100, f"post{i}", indexer=f"idx{i}") for i in range(2)]
    sab = VideoOnlySAB(str(tmp_path), video)
    strict = Config(path=str(tmp_path / "c.toml"), sab_to_local=[["/dl", str(tmp_path)]],
                    nearly_complete_percent=0.001)            # the sample is over this limit
    with pytest.raises(Abort):
        pipeline.usenet_single(strict, pipeline.Options(unattended=True), NoOtherPosts(), sab, t, posts, 2)
    before = list(sab.added)
    cfg = Config(path=str(tmp_path / "c.toml"), sab_to_local=[["/dl", str(tmp_path)]])
    dirs, nzos = pipeline.usenet_single(cfg, pipeline.Options(unattended=True), NoOtherPosts(), sab, t, posts, 2)
    assert sab.added == before                                # nothing downloaded again
    assert len(dirs) == 1                                     # the first run's download, used


def test_the_nearly_complete_limit_not_a_fixed_50_mb_decides_what_is_small():
    """A sample video is often more than 50 MB: within your limit it comes over BitTorrent."""
    import inspect
    src = inspect.getsource(pipeline.plan_and_fetch)
    body = src[src.index("def only_small_missing"):src.index("def fetch_small")]
    assert "50 << 20" not in body and "nearly_complete_mb" in body and "nearly_complete_percent" in body


def test_a_small_file_is_fetched_on_its_own_up_to_the_nearly_complete_limit():
    """A 57 MB sample was refused by a fixed 50 MB cap: your MB limit decides instead."""
    import inspect
    src = inspect.getsource(pipeline.plan_and_fetch)
    body = src[src.index("def fetch_small"):src.index("def split(")]
    assert "50 << 20" not in body and "nearly_complete_mb" in body
