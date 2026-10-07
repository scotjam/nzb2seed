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
    # named the way Sonarr/Radarr do: the group is the last bracketed word
    ("Show (2004) (74543) - S04E01 - The Title - [Bluray-1080p] [GRPD]", "grpd"),
    ("Show (2020) - S01E01 - Pilot - [WEBDL-1080p][x264] [ENG]", None),
    ("Show (2020) - S01E01 - Pilot - [Bluray-1080p] [x265]", None),
    # named without the scene dash, after the tech tags
    ("Show.2022.S03E03.Title.1080p.AMZN.WEB-DL.x265.Grpg", "grpg"),
    ("Show.2022.S03E03.Title.1080p.AMZN.Webrip.x265.10bit.EAC3.5.1.Atmos.GRPZ", "grpz"),
    ("Show.2022.S03E03.Title.1080p.AMZN.Webrip.x265.10bit.EAC3.5.1.Atmos.GRPZ]", "grpz"),
    ("show.s03e03.title.1080p.web.dl.hevc.x265.grpteam", "grpteam"),
    ("Show (2022) S03E03 Title (1080p AMZN Webrip x265 10bit EAC3 5.1 Atmos - ENC)[GRPQ]", "grpq"),
    ("Show (2022) S03E03 (1080p DS4K AMZN Webrip DV HDR10+ DDP5.1 x265) - Grpv", "grpv"),
    ("Show (2016) - S03E03 - Title (1080p BluRay x265 Grps)", "grps"),
    ("Show (2022) - S03E03 - Title (1080p AMZN WEB-DL x265 Grpg)", "grpg"),
    ("Show.S03E03.Title.1080p.Blu-ray", None),                              # no group named
    ("Show.S03E03.Title.1080p.AMZN.WEB-DL.DDP5.1.x265", None),              # ends in a tag
    ("Show.S03E03.The.Long.Road.Home", None),                               # a title, not a group
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


def test_a_missing_file_is_fetched_on_its_own_whatever_its_size():
    """A 57 MB sample was refused by a fixed 50 MB cap. Everything Usenet can supply is
    fetched; what is left is weighed against the nearly-complete limit only at the end."""
    import inspect
    src = inspect.getsource(pipeline.plan_and_fetch)
    body = src[src.index("def fetch_small"):src.index("def split(")]
    assert "<< 20" not in body and "nearly_complete_mb" not in body


def test_what_was_settled_for_is_recorded_and_whole_posts_can_be_tried(tmp_path):
    """A small file missing: normally the build settles after the first post and says so;
    asked to try whole posts, it downloads the others first - and settles only after."""
    film = "Film 2022 1080p BluRay REMUX-GRP"
    video = rnd(200_000, 7)
    t = parse(make_torrent(film, {f"{film}.mkv": video, f"{film}.mkv.nfo": b"n" * 800}))
    posts = [rel(film.replace(" ", "."), len(video) + 900 + i, f"post{i}", indexer=f"idx{i}") for i in range(3)]
    (tmp_path / "a").mkdir()
    cfg = Config(path=str(tmp_path / "a" / "c.toml"), sab_to_local=[["/dl", str(tmp_path / "a")]])
    sab = VideoOnlySAB(str(tmp_path / "a"), video)
    pipeline._this_build.settled = []
    pipeline.usenet_single(cfg, pipeline.Options(unattended=True), NoOtherPosts(), sab, t, posts, 2)
    assert len(sab.added) == 1
    assert [s["label"] for s in pipeline._this_build.settled] == [film]
    assert pipeline.Incomplete("x", 0.99, "h", "", "", False).details()["settled"][0]["missing"] == 800

    (tmp_path / "b").mkdir()
    cfg = Config(path=str(tmp_path / "b" / "c.toml"), sab_to_local=[["/dl", str(tmp_path / "b")]])
    sab = VideoOnlySAB(str(tmp_path / "b"), video)
    pipeline._this_build.settled = []
    dirs, _ = pipeline.usenet_single(cfg, pipeline.Options(unattended=True, whole_posts=True),
                                     NoOtherPosts(), sab, t, posts, 2)
    assert len(sab.added) == 3                             # every whole post tried
    assert dirs and pipeline._this_build.settled == []     # then settled: nothing more to offer


def test_a_build_that_stops_short_says_why_and_what_next(tmp_path, monkeypatch):
    """Every missing file gets a reason in the log, and the build says what can be done -
    naming the tracker the rest would come from."""
    from nzb2seed import metadata
    monkeypatch.setattr(metadata, "srrdb_details", lambda r: None)          # a P2P release
    film = "Film 2022 1080p BluRay REMUX-GRP"
    t = parse(make_torrent(film, {f"{film}.mkv": rnd(200_000, 7), f"{film}.mkv.nfo": b"n" * 800}))
    nfo = next(f for f in t.real_files if f.name.endswith(".nfo"))
    cfg = Config(path=str(tmp_path / "c.toml"), nearly_limits=[{"tracker": "TrackerOne", "percent": 5, "mb": 200}])
    pipeline._this_build.tracker, pipeline._this_build.seeders, pipeline._this_build.settled = "TrackerOne", 3, []
    lines = pipeline.explain_missing(cfg, t, [nfo], [], 800, 200_800)
    assert "made by the group for this upload" in lines[0] and "in no pre database" in lines[0]
    assert lines[-1].startswith("Next: Add to torrent client downloads just the missing") and "TrackerOne" in lines[-1]
    pipeline._this_build.seeders = 0
    assert "no seeders on TrackerOne" in pipeline.explain_missing(cfg, t, [nfo], [], 800, 200_800)[-1]
    pipeline._this_build.seeders, pipeline._this_build.settled = 3, [{"label": film, "missing": 800, "post": 1}]
    assert "Try whole posts first" in pipeline.explain_missing(cfg, t, [nfo], [], 800, 200_800)[-1]
    big = pipeline.explain_missing(cfg, t, [nfo], [], 150_000, 200_800)[-1]
    assert "more than TrackerOne allows" in big and "Override" in big
    pipeline._this_build.tracker = "OtherTracker"                 # no limit set for it
    assert "you have not set how much OtherTracker lets you download" in \
        pipeline.explain_missing(cfg, t, [nfo], [], 800, 200_800)[-1]


def test_a_single_file_torrent_named_after_its_file_finds_its_posts():
    """A tracker that names a one-file torrent after the file ("...-GRP.mkv"): Usenet posts
    never carry the extension, so searching with it found nothing at all."""
    name = "Film.2012.HDR.UHD.BluRay.2160p.TrueHD.Atmos.7.1.HEVC.REMUX-GRP"
    t = parse(make_torrent(name + ".mkv", {name + ".mkv": b"x" * 1000}))
    need = pipeline.build_units(t, t.name).need
    assert "mkv" not in need.query and need.label == name
    assert pipeline._fits(need, rel(name, 2000, "p")) is None
    assert pipeline.need_for(t, t.name, True).label == name



def test_the_question_says_what_stopping_would_mean(tmp_path, monkeypatch):
    t, files = pack_torrent()
    need = need_for(t, "Show.2016.S03E02.720p.HDTV.x264-GRPA", False)
    asked = []
    monkeypatch.setattr(pipeline, "report_ask", lambda prompt, choices: asked.append(prompt))
    from test_usenet import FakeProwlarr
    pipeline.ask_for_post(Config(path=str(tmp_path / "c.toml")),
                          FakeProwlarr({}, [rel("Show.2016.S03E02.720p.HDTV.x264-GRPA", need.min_size + 5, "x")]),
                          need, [], note="Or stop the build here: about 472.0000 MB (4.8%) would be missing.")
    assert asked and asked[0].endswith("Or stop the build here: about 472.0000 MB (4.8%) would be missing.")


# ---------------------------------------------------------------- posts missing articles

class HoledSAB(VideoOnlySAB):
    """Like VideoOnlySAB, but the posts named in ``holed`` come down with an article missing
    (zeros where it was) - and none of them had repair data."""

    def __init__(self, root, video, holed):
        super().__init__(root, video)
        self.holed = set(holed)

    def add_nzb(self, nzb, name, cat, pp, prio):
        nzo = super().add_nzb(nzb, name, cat, pp, prio)
        if self.n in self.holed:
            p = os.path.join(self.root, f"{name}.{self.n}", name.replace(" ", ".") + ".mkv")
            data = bytearray(open(p, "rb").read())
            data[100_000:100_000 + 300_000] = b"\0" * 300_000
            open(p, "wb").write(data)
        return nzo

    def history_slot(self, nzo):
        return {"stage_log": [{"name": "Source", "actions": ["No par2 sets"]}]}


def holed_film(tmp_path):
    film = "Film 2022 1080p BluRay REMUX-GRP"
    video = rnd(2_000_000, 7)
    t = parse(make_torrent(film, {f"{film}.mkv": video}))
    cfg = Config(path=str(tmp_path / "c.toml"), sab_to_local=[["/dl", str(tmp_path)]])
    return film, video, t, cfg


def test_a_post_missing_articles_is_passed_over_for_a_complete_one(tmp_path):
    film, video, t, cfg = holed_film(tmp_path)
    posts = [rel(film.replace(" ", "."), len(video) + 900, "holed", grabs=9, indexer="idx1"),
             rel(film.replace(" ", ".") + "-xpost", len(video) + 950, "whole", indexer="idx2")]
    sab = HoledSAB(str(tmp_path), video, holed={1})
    dirs, _ = pipeline.usenet_single(cfg, pipeline.Options(unattended=True), NoOtherPosts(), sab, t, posts, 2)
    assert len(sab.added) == 2 and dirs == [os.path.join(str(tmp_path), f"{sab.added[1]}.2")]


def test_with_no_other_post_the_incomplete_one_is_still_used(tmp_path):
    film, video, t, cfg = holed_film(tmp_path)
    posts = [rel(film.replace(" ", "."), len(video) + 900, "holed", indexer="idx1")]
    sab = HoledSAB(str(tmp_path), video, holed={1})
    dirs, _ = pipeline.usenet_single(cfg, pipeline.Options(unattended=True), NoOtherPosts(), sab, t, posts, 2)
    assert dirs == [os.path.join(str(tmp_path), f"{sab.added[0]}.1")]      # the piece check does the rest


def test_a_download_its_repair_data_checked_is_not_scanned(tmp_path):
    assert pipeline.repaired({"stage_log": [{"name": "Repair", "actions": ["[x] Quick Check OK"]}]})
    assert not pipeline.repaired({"stage_log": [{"name": "Source", "actions": ["No par2 sets"]}]})
    d = tmp_path / "dl"
    d.mkdir()
    data = bytearray(rnd(3_000_000, 3))
    data[10:10 + 700_000] = b"\0" * 700_000
    data[2_000_000:2_000_100] = b"\0" * 100                   # short zeros are ordinary data
    (d / "x.mkv").write_bytes(bytes(data))
    assert pipeline.article_gaps(str(d)) == (1, 700_000)


@pytest.mark.parametrize("post,same", [
    ("show.s06.bluray.1080p.x265-grpi", True),         # the group posted it without the year
    ("show.2004.s06.1080p.web-grpi", True),            # the same year, without brackets
    ("show.(2004).s06.1080p-grpi", True),
    ("show.1987.s06.1080p-grpi", False),               # another year: another show
    ("other.show.s06.1080p-grpi", False),
])
def test_a_year_the_tracker_added_does_not_hide_the_groups_posts(post, same):
    assert pipeline.same_show(post, "show.(2004).") is same


def test_a_season_with_a_year_in_its_name_searches_without_it():
    t = parse(make_torrent("Show (2004) S06 Bluray 1080p x265-GRPI",
                           {f"Show.S06E0{i}.1080p.Bluray.x265-GRPI.mkv": b"x" * (1000 + i) for i in (1, 2)}))
    root = pipeline.build_units(t, t.name)
    assert root.need.query == "show  s06" and "2004" not in root.children[0].need.query
    assert pipeline.drop_year("1923.") == "1923."                 # a title that is a year stays



def test_an_encoder_and_group_run_together_is_read_as_the_group(tmp_path, monkeypatch):
    """'(... - Enc)[Grpq]' teaches that a dotted repost's 'encgrpq' is Grpq - kept on disk;
    nothing is merged before a name showing both parts has been seen."""
    from nzb2seed import pipeline
    monkeypatch.setattr(pipeline, "_JOINED", {})
    pipeline.configure_groups(str(tmp_path / "nzb2seed.toml"))
    dotted = "Show.2022.S03E03.Title.1080p.AMZN.Webrip.x265.10bit.EAC3.5.1.Atmos.EncGrpq"
    assert group_of(dotted) == "encgrpq"
    assert group_of("Show (2022) S03E03 Title (1080p AMZN Webrip x265 10bit EAC3 5.1 Atmos - Enc)[Grpq]") == "grpq"
    assert group_of(dotted) == "grpq"
    assert group_of(dotted + "]") == "grpq"
    assert group_of("Show.S03E03.1080p.WEB.x265-Grpq") == "grpq"              # the group itself is unchanged
    monkeypatch.setattr(pipeline, "_JOINED", {})                            # a restart: read back from disk
    pipeline.configure_groups(str(tmp_path / "nzb2seed.toml"))
    assert group_of(dotted) == "grpq"
