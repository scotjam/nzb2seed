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
    ("Spider-Man.No.Way.Home.2021.1080p.BluRay.x264-GRP", "grp"),
    ("no group here", None),
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
        rel("Show.2016.S03E02.720p.HDTV.x264-GRPA", big, "same-size-elsewhere"),   # same post
        rel("Show.2016.S03E02.720p.HDTV.x264-GRPA", big + 5, "good", grabs=9),
        rel("Show.2016.S03E02.720p.HDTV.x264-GRPA", need.min_size - 1, "too-small"),
        rel("Show.2016.S03E02.720p.iP.WEBRip.AAC2.0.H264-GRPC", big + 9, "other-group"),
        rel("Show.2016.S03E02.1080p.HDTV.x264-GRPA", big + 9, "other-res"),
        rel("Show.2016.S03E03.720p.HDTV.x264-GRPA", big + 9, "other-ep"),
    ]
    from test_usenet import FakeProwlarr
    alts = pipeline.find_alternatives(Config(path=str(tmp_path / "c.toml")), FakeProwlarr({}, results), need, [failed])
    assert [r.guid for r in alts] == ["good"]


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
    other = rel("Show.2016.S03E02.1080p.HDTV.x264-GRPA", size2 + 50, "other")   # same group, other res
    foreign = rel("Show.2016.S03E02.720p.HDTV.x264-OTHERGRP", size2 + 60, "foreign")
    asked = []

    def fake_ask(prompt, choices):
        asked.append((prompt, choices))
        return next(i for i, c in enumerate(choices) if c["title"] == other.title)
    monkeypatch.setattr(pipeline, "report_ask", fake_ask)
    sab = PackSAB(str(tmp_path), files, failing={failing})
    cfg = Config(path=str(tmp_path / "c.toml"), sab_to_local=[["/dl", str(tmp_path)]])
    dirs, _ = pipeline.usenet_multi(cfg, NZBsOK([other, foreign]), sab, t,
                                    with_others(files, [rel(failing, size2 + 100)]), 2)
    assert asked and "S03E02" in asked[0][0] and "GRPA" in asked[0][0]
    assert [c["title"] for c in asked[0][1]] == [other.title]     # other release groups are not offered
    assert asked[0][1][0]["note"] == "other resolution"
    assert other.title in sab.added and len(dirs) == 4


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
