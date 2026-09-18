"""Grabbing a whole season: one group, one resolution, every episode, checked and presented."""
import json
import os
import zlib

import pytest

from nzb2seed import metadata, scenerar, season
from nzb2seed.clients import Release
from nzb2seed.config import Config
from nzb2seed.season import episode_numbers, scene_name, season_release_name

from helpers import rnd

SHOW = {"id": 7, "name": "Test Show", "premiered": "2016-01-01", "imdb": "tt0000007", "tvdb": 77,
        "url": "https://www.tvmaze.com/shows/7/test-show"}
EPS = [{"number": n, "name": f"Episode {n}", "airdate": f"2016-01-0{n}"} for n in (1, 2, 3)]


def ep_title(e, grp="GRP", suffix=""):
    return f"Test.Show.2016.S01E0{e}.720p.HDTV.x264-{grp}{suffix}"


def video(e):
    return rnd(30_000 + e * 1000, e)


def rel(title, guid, grabs=0):
    size = 10**6 + sum(map(ord, guid))       # separate posts differ in size
    return Release(title, "usenet", "idx", 1, size, guid, "", "", "", grabs, None, None)


# ---------------------------------------------------------------- naming

def test_names():
    assert scene_name("Test.Show.2016.S01E01.720p.HDTV.x264-GRP-xpost") == "Test.Show.2016.S01E01.720p.HDTV.x264-GRP"
    assert season_release_name("Test.Show.2016.S01E01.720p.HDTV.x264-GRP-xpost") == "Test.Show.2016.S01.720p.HDTV.x264-GRP"
    assert episode_numbers("test.show.s01e02.720p.mkv", 1) == {2}
    assert episode_numbers("Test.Show.S01E01-E03.720p", 1) == {1, 2, 3}
    assert episode_numbers("test.show.s02e02.mkv", 1) == set()


# ---------------------------------------------------------------- options

class PR:
    def __init__(self, results):
        self.results, self.queries = results, []

    def search(self, q, *a):
        self.queries.append(q)
        return list(self.results)

    def fetch(self, r):
        return f"<?xml version='1.0'?><nzb/><!--guid={r.guid}-->".encode()


def test_options_per_group_and_resolution():
    results = [rel(ep_title(1), "a1"), rel(ep_title(2), "a2"), rel(ep_title(3), "a3"),
               rel(ep_title(1, "OTHER"), "b1"),
               rel("Test.Show.2016.S01.1080p.WEB.h264-WEBGRP", "s1"),
               rel("Test.Show.S01E01.720p.HDTV.x264-OLD", "old"),          # the 1990s show, other prefix
               rel("Test.Show.2016.S02E01.720p.HDTV.x264-GRP", "s2"),      # other season
               rel("Test.Show.Behind.The.Scenes.S01E01.720p.HDTV.x264-GRP", "bts"),  # another show
               rel("Test.Show.US.S01E01.720p.HDTV.x264-USGRP", "us")]
    opts = season.find_options(Config(path="x"), PR(results), "Test Show", 1, [1, 2, 3])
    by = {(o.group, o.res): o.summary([1, 2, 3]) for o in opts}
    assert by[("grp", "720p")]["complete"] and by[("grp", "720p")]["episodes_found"] == [1, 2, 3]
    assert by[("webgrp", "1080p")]["season_nzbs"] == 1
    assert by[("other", "720p")]["episodes_missing"] == [2, 3]
    assert by[("old", "720p")]["prefix"] == "test.show."
    assert all(o.summary([1, 2, 3])["prefix"] != "test.show.2016.s02" for o in opts)
    assert ("usgrp", "720p") in by and all("behind" not in o.prefix for o in opts)


# ---------------------------------------------------------------- a whole grab

class SAB:
    """Each NZB guid maps to what the post holds: {'files': {name: bytes}} or 'fail'."""

    def __init__(self, root, posts):
        self.root, self.posts, self.jobs, self.added = root, posts, {}, []

    def config(self):
        return {"misc": {"complete_dir": "/dl"}}

    def add_nzb(self, nzb, name, cat, pp, prio):
        guid = nzb.decode().split("guid=")[1].split("-->")[0]
        nzo = f"nzo{len(self.jobs) + 1}"
        self.jobs[nzo] = (guid, name)
        self.added.append(guid)
        post = self.posts[guid]
        if post != "fail":
            d = os.path.join(self.root, name)
            os.makedirs(d, exist_ok=True)
            for n, data in post.items():
                os.makedirs(os.path.dirname(os.path.join(d, n)), exist_ok=True)
                with open(os.path.join(d, n), "wb") as fh:
                    fh.write(data)
        return nzo

    def ensure_pp(self, nzo, pp):
        return "+Repair/Unpack"

    def status(self, nzo):
        guid, name = self.jobs[nzo]
        if self.posts[guid] == "fail":
            return "Failed", {"fail_message": "Aborted"}
        return "Completed", {"storage": f"/dl/{name}"}


NFO = b"GRP presents\r\nTest Show\r\n"


def srrdb_for(e):
    base = f"test.show.2016.s01e0{e}.720p.hdtv.x264-grp"
    return {"name": ep_title(e), "files": [
        {"name": f"{base}.nfo", "size": len(NFO), "crc": "0"},
        {"name": f"{base}.sfv", "size": 20, "crc": "0"},
        {"name": f"Sample/{base}-sample.mkv", "size": 5000, "crc": "0"},
        {"name": f"{base}.rar", "size": 50000, "crc": "0"}],
        "archived-files": [{"name": f"{base}.mkv", "size": len(video(e)),
                            "crc": f"{zlib.crc32(video(e)) & 0xFFFFFFFF:08X}"}]}


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(metadata, "tvmaze_show", lambda i: SHOW)
    monkeypatch.setattr(metadata, "tvmaze_episodes", lambda i, s: EPS)
    details = {ep_title(e): srrdb_for(e) for e in (1, 2, 3)}
    monkeypatch.setattr(metadata, "srrdb_details", lambda r: details.get(r))
    stored = {"sfv": b"; sfv file here!\r\n\r\n"}      # 20 bytes, as srrDB lists it
    monkeypatch.setattr(metadata, "srrdb_file", lambda r, p: NFO if p.endswith(".nfo") else
                        (stored["sfv"] if p.endswith(".sfv") else None))
    monkeypatch.setattr(metadata, "predb", lambda r: {"pretime": 1500000000, "section": "TV-HD", "group": "GRP",
                                                      "nuked": False, "reason": "", "url": None})
    monkeypatch.setattr(metadata, "predb_me", lambda r: {"available": False, "why": "predb.me answered with a Cloudflare bot check"})
    monkeypatch.setattr(metadata, "xrel", lambda r: {"available": True, "found": False})
    from nzb2seed import scenerar
    # no network in tests: rebuilding scene RARs "fails" unless a test says otherwise
    monkeypatch.setattr(scenerar, "rebuild", lambda *a: (False, "srrDB has no .srr for this release"))
    monkeypatch.setattr(metadata, "mediainfo", lambda p: f"General\nComplete name : {os.path.basename(p)}\n")

    def shots(p, out, n=4):
        os.makedirs(out, exist_ok=True)
        paths = []
        for i in range(n):
            q = os.path.join(out, f"screen{i + 1:02d}.png")
            open(q, "wb").write(b"png")
            paths.append(q)
        return paths
    monkeypatch.setattr(metadata, "screenshots", shots)
    cfg = Config(path=str(tmp_path / "c.toml"), sab_to_local=[["/dl", str(tmp_path / "dl")]])
    return tmp_path, cfg


def post(e, nfo=NFO, vid=None, extra=None):
    base = f"test.show.2016.s01e0{e}.720p.hdtv.x264-grp"
    files = {f"{base}.mkv": vid if vid is not None else video(e), f"{base}.par2": b"par2",
             f"Test.Show.S01E0{e}.nzb": b"<nzb/>"}
    if nfo is not None:
        files[f"{base}.nfo"] = nfo
    files.update(extra or {})
    return files


def test_grab_checks_every_episode_and_writes_the_sidecar(world, monkeypatch):
    tmp_path, cfg = world
    remux = b"REMUXED!" + video(2)[8:]                        # same size, different bytes
    posts = {"e1": post(1),
             "e2-remux": post(2, vid=remux), "e2-good": post(2, nfo=None),     # good one lacks its .nfo
             "e3-fail": "fail", "e3": post(3, extra={"Sample/test.show.2016.s01e03.720p.hdtv.x264-grp-sample.mkv": b"s" * 5000})}
    results = [rel(ep_title(1), "e1"), rel(ep_title(2, suffix="-xpost"), "e2-remux", grabs=9),
               rel(ep_title(2), "e2-good"), rel(ep_title(3), "e3-fail", grabs=9), rel(ep_title(3), "e3")]
    sab = SAB(str(tmp_path / "dl"), posts)
    monkeypatch.setattr(season, "Prowlarr", lambda *a: PR(results))
    monkeypatch.setattr(season, "SABnzbd", lambda *a: sab)
    key = next(o.key for o in season.find_options(cfg, PR(results), "Test Show", 1, [1, 2, 3]))

    out = season.grab_season(cfg, 7, 1, key)
    assert out["result"] == "3/3 episodes"
    assert sab.added == ["e1", "e2-remux", "e3-fail", "e2-good", "e3"]     # remux and failure replaced

    dest = tmp_path / "dl" / "Test.Show.2016.S01.720p.HDTV.x264-GRP"
    side = tmp_path / "dl" / "Test.Show.2016.S01.720p.HDTV.x264-GRP-metadata"
    e2 = dest / "Test.Show.2016.S01E02.720p.HDTV.x264-GRP"
    assert (e2 / "test.show.2016.s01e02.720p.hdtv.x264-grp.nfo").read_bytes() == NFO   # fetched from srrDB
    assert (e2 / "test.show.2016.s01e02.720p.hdtv.x264-grp.sfv").exists()
    everything = {p.name for p in dest.rglob("*") if p.is_file()}
    assert not any(n.endswith((".par2", ".nzb")) for n in everything)            # junk removed
    assert not (dest / "report.json").exists() and not (dest / "mediainfo.txt").exists()

    r = json.loads((side / "report.json").read_text())
    assert [e["present"] for e in r["episodes"]] == [True, True, True]
    assert r["links"]["IMDb"] == "https://www.imdb.com/title/tt0000007/"
    assert (side / "mediainfo.txt").exists() and len(r["screenshots"]) == 4
    assert (side / "report.html").exists()
    states = {f["name"].split("/")[-1]: f["state"] for c in r["releases"] for f in c["files"]
              if "s01e01" in f["name"]}
    assert states["test.show.2016.s01e01.720p.hdtv.x264-grp-sample.mkv"] == "missing - not available on srrDB"
    assert all(v["genuine"] for c in r["releases"] for v in c["video"])


def test_an_existing_folder_that_is_not_ours_is_left_alone(world, monkeypatch):
    tmp_path, cfg = world
    dest = tmp_path / "dl" / "Test.Show.2016.S01.720p.HDTV.x264-GRP"
    dest.mkdir(parents=True)
    (dest / "mine.txt").write_text("keep")
    results = [rel(ep_title(e), f"e{e}") for e in (1, 2, 3)]
    sab = SAB(str(tmp_path / "dl"), {f"e{e}": post(e) for e in (1, 2, 3)})
    monkeypatch.setattr(season, "Prowlarr", lambda *a: PR(results))
    monkeypatch.setattr(season, "SABnzbd", lambda *a: sab)
    key = season.find_options(cfg, PR(results), "Test Show", 1, [1, 2, 3])[0].key
    with pytest.raises(season.Abort):
        season.grab_season(cfg, 7, 1, key)
    assert (dest / "mine.txt").read_text() == "keep" and sab.added == []


def test_captures_as_much_as_possible_when_the_season_is_incomplete(world, monkeypatch):
    tmp_path, cfg = world
    results = [rel(ep_title(1), "e1"), rel(ep_title(2), "e2")]              # nobody posted E03
    sab = SAB(str(tmp_path / "dl"), {"e1": post(1), "e2": post(2)})
    monkeypatch.setattr(season, "Prowlarr", lambda *a: PR(results))
    monkeypatch.setattr(season, "SABnzbd", lambda *a: sab)
    real_check = season.check_release

    def flaky(srr, release, *a):                                            # srrDB check blows up for E02
        if "S01E02" in release:
            raise RuntimeError("srrDB timed out")
        return real_check(srr, release, *a)
    monkeypatch.setattr(season, "check_release", flaky)
    key = season.find_options(cfg, PR(results), "Test Show", 1, [1, 2, 3])[0].key
    out = season.grab_season(cfg, 7, 1, key)
    assert out["result"] == "2/3 episodes - missing E03"
    side = tmp_path / "dl" / "Test.Show.2016.S01.720p.HDTV.x264-GRP-metadata"
    r = json.loads((side / "report.json").read_text())
    assert [e["present"] for e in r["episodes"]] == [True, True, False]
    assert any("E03" in n for n in r["notes"]) and any("srrDB timed out" in n for n in r["notes"])
    assert (side / "mediainfo.txt").exists() and r["screenshots"]


def test_nothing_available_creates_nothing(world, monkeypatch):
    tmp_path, cfg = world
    results = [rel(ep_title(1), "e1")]
    sab = SAB(str(tmp_path / "dl"), {"e1": "fail"})
    monkeypatch.setattr(season, "Prowlarr", lambda *a: PR(results))
    monkeypatch.setattr(season, "SABnzbd", lambda *a: sab)
    key = season.find_options(cfg, PR(results), "Test Show", 1, [1, 2, 3])[0].key
    with pytest.raises(season.Abort, match="nothing was created"):
        season.grab_season(cfg, 7, 1, key)
    assert not (tmp_path / "dl" / "Test.Show.2016.S01.720p.HDTV.x264-GRP").exists()


# ---------------------------------------------------------------- one missing file from another post

NZB_WITH_SAMPLE = b"""<?xml version="1.0"?><nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">
<head><meta type="title">x</meta></head>
<file subject="[1/3] - &quot;release.mkv&quot; yEnc (1/2)"><groups><group>a.b</group></groups><segments><segment bytes="9" number="1">a@b</segment></segments></file>
<file subject="[PRiVATE]-[x]-[test.show.2016.s01e01.720p.hdtv.x264-grp-sample.mkv]-[2/3] - &quot;&quot; yEnc (1/1)"><groups><group>a.b</group></groups><segments><segment bytes="5" number="1">c@d</segment></segments></file>
<file subject="[3/3] - &quot;release.par2&quot; yEnc (1/1)"><groups><group>a.b</group></groups><segments><segment bytes="1" number="1">e@f</segment></segments></file>
</nzb>"""


def test_trim_keeps_only_the_wanted_file():
    from nzb2seed import nzbinfo
    out = nzbinfo.trim(NZB_WITH_SAMPLE, "test.show.2016.s01e01.720p.hdtv.x264-grp-sample.mkv")
    files = nzbinfo.parse(out)
    assert len(files) == 1 and "sample" in files[0].name
    assert b'xmlns="http://www.newzbin.com/DTD/2003/nzb"' in out and b"<head>" in out
    assert nzbinfo.trim(NZB_WITH_SAMPLE, "nothing-like-this.mkv") is None


def test_missing_sample_is_fetched_from_another_post(world, monkeypatch, tmp_path):
    _, cfg = world
    sample = b"s" * 5000
    good_crc = f"{zlib.crc32(sample) & 0xFFFFFFFF:08X}"
    posts = [rel(ep_title(1, suffix="-xpost"), "bad", grabs=5), rel(ep_title(1), "good")]

    class SamplePR(PR):
        def fetch(self, r):
            return NZB_WITH_SAMPLE

    class SampleSAB:
        def __init__(self):
            self.n = 0

        def add_nzb(self, nzb, name, *a):
            self.n += 1
            d = tmp_path / "dl" / f"job{self.n}"
            d.mkdir(parents=True)
            # the first post's copy is different (same size), the second is the original
            (d / "test.show.2016.s01e01.720p.hdtv.x264-grp-sample.mkv").write_bytes(
                (b"x" * 5000) if self.n == 1 else sample)
            return f"nzo{self.n}"

        def status(self, nzo):
            return "Completed", {"storage": f"/dl/job{nzo[3:]}"}
    fetch = season.FileFetcher(cfg, SamplePR(posts), SampleSAB(), set())
    dest = tmp_path / "rel" / "Sample" / "test.show.2016.s01e01.720p.hdtv.x264-grp-sample.mkv"
    ok = fetch(ep_title(1), "Sample/test.show.2016.s01e01.720p.hdtv.x264-grp-sample.mkv", 5000, good_crc, str(dest))
    assert ok and dest.read_bytes() == sample
    assert not (tmp_path / "dl" / "job1").exists() and not (tmp_path / "dl" / "job2").exists()


def test_a_second_grab_resumes_from_the_season_folder(world, monkeypatch):
    tmp_path, cfg = world
    results = [rel(ep_title(e), f"e{e}") for e in (1, 2, 3)]
    sab = SAB(str(tmp_path / "dl"), {f"e{e}": post(e) for e in (1, 2, 3)})
    monkeypatch.setattr(season, "Prowlarr", lambda *a: PR(results))
    monkeypatch.setattr(season, "SABnzbd", lambda *a: sab)
    key = season.find_options(cfg, PR(results), "Test Show", 1, [1, 2, 3])[0].key
    assert season.grab_season(cfg, 7, 1, key)["result"] == "3/3 episodes"
    sab.added.clear()
    assert season.grab_season(cfg, 7, 1, key)["result"] == "3/3 episodes"
    assert sab.added == []                                  # nothing downloaded again
    dest = tmp_path / "dl" / "Test.Show.2016.S01.720p.HDTV.x264-GRP"
    assert sorted(p.name for p in dest.iterdir()) == [ep_title(e) for e in (1, 2, 3)]


def test_sample_fetch_when_sab_reports_the_file_itself(world, tmp_path):
    _, cfg = world
    sample = b"s" * 5000
    crc = f"{zlib.crc32(sample) & 0xFFFFFFFF:08X}"
    name = "test.show.2016.s01e01.720p.hdtv.x264-grp-sample.mkv"

    class P(PR):
        def fetch(self, r):
            return NZB_WITH_SAMPLE

    class OneFileSAB:
        def add_nzb(self, nzb, job, *a):
            self.job = job
            d = tmp_path / "dl" / job
            d.mkdir(parents=True)
            (d / name).write_bytes(sample)
            return "nzo1"

        def status(self, nzo):
            return "Completed", {"storage": f"/dl/{self.job}/{name}"}      # the file, not the folder
    dest = tmp_path / "rel" / "Sample" / name
    assert season.FileFetcher(cfg, P([rel(ep_title(1), "p")]), OneFileSAB(), set())(
        ep_title(1), "Sample/" + name, 5000, crc, str(dest))
    assert dest.read_bytes() == sample and not any((tmp_path / "dl").iterdir())


# ---------------------------------------------------------------- keep / rebuild the scene RARs

VOL = b"R" * 50000                                        # the one RAR volume srrdb_for() lists


def with_volume(e):
    d = srrdb_for(e)
    d["files"][3]["crc"] = f"{zlib.crc32(VOL) & 0xFFFFFFFF:08X}"
    return d


def grab_one(world, monkeypatch, posts, details, packing=None):
    tmp_path, cfg = world
    monkeypatch.setattr(metadata, "srrdb_details", lambda r: details.get(r))
    results = [rel(ep_title(e), f"e{e}") for e in (1, 2, 3)]
    sab = SAB(str(tmp_path / "dl"), posts)
    monkeypatch.setattr(season, "Prowlarr", lambda *a: PR(results))
    monkeypatch.setattr(season, "SABnzbd", lambda *a: sab)
    key = season.find_options(cfg, PR(results), "Test Show", 1, [1, 2, 3])[0].key
    out = season.grab_season(cfg, 7, 1, key, packing)
    dest = tmp_path / "dl" / "Test.Show.2016.S01.720p.HDTV.x264-GRP"
    r = json.loads((tmp_path / "dl" / "Test.Show.2016.S01.720p.HDTV.x264-GRP-metadata" / "report.json").read_text())
    return out, dest, r


def files_of(dest, e):
    return sorted(p.name for p in (dest / ep_title(e)).rglob("*") if p.is_file())


def test_posts_that_are_the_scene_rars_keep_them(world, monkeypatch):
    base = lambda e: f"test.show.2016.s01e0{e}.720p.hdtv.x264-grp"
    posts = {f"e{e}": post(e, extra={f"{base(e)}.rar": VOL}) for e in (1, 2, 3)}
    out, dest, r = grab_one(world, monkeypatch, posts, {ep_title(e): with_volume(e) for e in (1, 2, 3)})
    assert out["result"] == "3/3 episodes"
    assert files_of(dest, 1) == [f"{base(1)}.nfo", f"{base(1)}.rar", f"{base(1)}.sfv"]    # no unpacked .mkv
    assert all("CRC matches" in c["rars"] for c in r["releases"])
    assert [e["present"] for e in r["episodes"]] == [True, True, True] and r["packing"] == "Keep scene RARs"


def test_obfuscated_posts_get_their_scene_rars_rebuilt(world, monkeypatch):
    base = lambda e: f"test.show.2016.s01e0{e}.720p.hdtv.x264-grp"
    rebuilt = []

    def fake_rebuild(release, det, video, out_folder):
        rebuilt.append(release)
        with open(os.path.join(out_folder, os.path.basename(det["files"][3]["name"])), "wb") as fh:
            fh.write(VOL)
        return True, "rebuilt 1 scene RAR volume(s) from srrDB's .srr; every CRC matches"
    monkeypatch.setattr(scenerar, "rebuild", fake_rebuild)
    posts = {f"e{e}": post(e, extra={"abc123obfuscated.part01.rar": b"junk" * 100}) for e in (1, 2, 3)}
    out, dest, r = grab_one(world, monkeypatch, posts, {ep_title(e): with_volume(e) for e in (1, 2, 3)})
    assert len(rebuilt) == 3
    assert files_of(dest, 2) == [f"{base(2)}.nfo", f"{base(2)}.rar", f"{base(2)}.sfv"]    # obfuscated set gone
    assert all(c["rars"].startswith("rebuilt") for c in r["releases"])
    assert (dest.parent / (dest.name + "-metadata") / "mediainfo.txt").exists()          # taken before the RARs


def test_rebuild_impossible_stays_unpacked_and_is_flagged(world, monkeypatch):
    posts = {f"e{e}": post(e) for e in (1, 2, 3)}
    out, dest, r = grab_one(world, monkeypatch, posts, {ep_title(e): with_volume(e) for e in (1, 2, 3)})
    assert out["result"] == "3/3 episodes"
    assert any(n.endswith(".mkv") for n in files_of(dest, 1))
    assert all(c["rars"].startswith("left unpacked") for c in r["releases"])


def test_unpack_all_never_keeps_rars(world, monkeypatch):
    base = lambda e: f"test.show.2016.s01e0{e}.720p.hdtv.x264-grp"
    posts = {f"e{e}": post(e, extra={f"{base(e)}.rar": VOL}) for e in (1, 2, 3)}
    out, dest, r = grab_one(world, monkeypatch, posts, {ep_title(e): with_volume(e) for e in (1, 2, 3)}, "unpack")
    assert not any(n.endswith(".rar") for n in files_of(dest, 1)) and any(n.endswith(".mkv") for n in files_of(dest, 1))
    assert r["packing"] == "Unpack all"
