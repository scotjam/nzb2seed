"""Pieces that fail: download other posts until the right copies are found (today's real case:
a repost whose video had different first bytes but exactly the right size)."""
import os

import pytest

from nzb2seed import pipeline
from nzb2seed.clients import Release
from nzb2seed.config import Config
from nzb2seed.pipeline import Abort, Options, Retry
from nzb2seed.torrent import parse

from helpers import make_torrent, rnd

PACK = "Show.2016.S03.720p.HDTV.AAC2.0.x264-Scene [NO RAR]"


def pack():
    files = {f"show.2016.s03e0{i}.720p.hdtv.x264-{'grpb' if i == 4 else 'grpa'}.mkv":
             rnd(41_000 + i * 1000, i) for i in range(1, 5)}
    raw = make_torrent(PACK, files)
    return parse(raw), raw, files


def rel(title, size, guid, grabs=0):
    return Release(title, "usenet", "idx", 1, size, guid, "", "", "", grabs, None, None)


class SAB:
    """Completes every NZB; posts listed in ``remuxed`` carry a copy with different first bytes."""

    def __init__(self, root, files, remuxed):
        self.root, self.files, self.remuxed = root, files, remuxed
        self.jobs, self.added = {}, []

    def add_nzb(self, nzb, name, cat, pp, prio):
        nzo = f"nzo{len(self.jobs) + 1}"
        guid = nzb.decode().split("guid=")[1].split("-->")[0]
        self.jobs[nzo] = (name, guid)
        self.added.append(guid)
        d = os.path.join(self.root, nzo)
        os.makedirs(d)
        ep = pipeline._EP.search(name.lower()).group(0)
        fname = next(f for f in self.files if ep in f)
        data = self.files[fname]
        if guid in self.remuxed:
            data = b"REMUXED-HEADER!!" + data[16:]           # same size, different start
        with open(os.path.join(d, name + ".mkv"), "wb") as fh:
            fh.write(data)
        return nzo

    def ensure_pp(self, nzo, pp):
        return "+Repair/Unpack"

    def status(self, nzo):
        return "Completed", {"storage": f"/dl/{nzo}"}


class PR:
    def __init__(self, results):
        self.results = results

    def fetch(self, r):
        return f"<?xml version='1.0'?><nzb/><!--guid={r.guid}-->".encode()

    def search(self, query, *a):
        ep = pipeline._EP.search(query.replace(" ", ".")).group(0)
        return [r for r in self.results if ep in r.title.lower()]


def setup(tmp_path, remuxed):
    t, raw, files = pack()
    tfile = tmp_path / "pack.torrent"
    tfile.write_bytes(raw)
    size = {i: len(next(v for k, v in files.items() if f"s03e0{i}" in k)) for i in range(1, 5)}
    grp = {i: "GRPB" if i == 4 else "GRPA" for i in range(1, 5)}
    picks = [[rel(f"Show.2016.S03E0{i}.720p.HDTV.x264-{grp[i]}", size[i] + 100, f"pick{i}")]
             for i in range(1, 5)]
    others = [rel(f"Show.2016.S03E0{i}.720p.HDTV.x264-{grp[i]}", size[i] + 200 + n, f"alt{i}{n}", grabs=9 - n)
              for i in (2, 4) for n in range(2)]
    cfg = Config(path=str(tmp_path / "c.toml"), sab_to_local=[["/dl", str(tmp_path / "dl")]],
                 torrent_dir=str(tmp_path / "torrents"))
    sab = SAB(str(tmp_path / "dl"), files, remuxed)
    pr = PR(others)
    slots = []
    dirs, _ = pipeline.usenet_multi(cfg, pr, sab, t, picks, 2, slots)
    return t, str(tfile), cfg, sab, pr, slots, dirs, files


def test_bad_copies_are_replaced_until_every_piece_verifies(tmp_path):
    # the picked E02 and E04 are re-muxed, and so is the first alternative for E02
    t, tfile, cfg, sab, pr, slots, dirs, files = setup(tmp_path, {"pick2", "pick4", "alt20"})
    out = str(tmp_path / "complete")
    result = pipeline.finish(cfg, Options(no_qbit=True), t, tfile, dirs, out, None, None,
                             retry=Retry(cfg, pr, sab, 2, slots))
    assert result["result"] == "files ready"
    for name, data in files.items():                      # byte-exact now
        with open(os.path.join(out, PACK, name), "rb") as fh:
            assert fh.read() == data
    # only the suspects were re-downloaded: E02 twice (first alternative was bad too), E04 once
    assert sab.added[4:] == ["alt20", "alt21", "alt40"]


def test_retry_switched_off_stops_the_build(tmp_path):
    t, tfile, cfg, sab, pr, slots, dirs, _ = setup(tmp_path, {"pick2"})
    with pytest.raises(Abort, match="pieces fail"):
        pipeline.finish(cfg, Options(no_qbit=True, retry_bad=False, local_verify=True), t, tfile, dirs,
                        str(tmp_path / "out"), None, None, retry=Retry(cfg, pr, sab, 2, slots))
    assert len(sab.added) == 4                             # nothing else downloaded


def test_gives_up_when_no_post_has_the_right_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "report_ask", lambda p, c: None)   # person declines to pick
    t, tfile, cfg, sab, pr, slots, dirs, _ = setup(tmp_path, {"pick2", "alt20", "alt21"})
    with pytest.raises(Abort, match="still fail"):
        pipeline.finish(cfg, Options(no_qbit=True), t, tfile, dirs,
                        str(tmp_path / "out"), None, None, retry=Retry(cfg, pr, sab, 2, slots))


def test_rerun_resumes_from_placed_files_and_only_fixes_the_bad_ones(tmp_path):
    """First run places everything but E02 is a remux; a rerun downloads nothing it already
    placed, skips the post that supplied the bad E02, and fetches only a replacement."""
    t, tfile, cfg, sab, pr, slots, dirs, files = setup(tmp_path, {"pick2"})
    out = str(tmp_path / "complete")
    cfg.output_dir = out
    with pytest.raises(Abort):                        # first run, retry off: stops at the check
        pipeline.finish(cfg, Options(no_qbit=True, retry_bad=False, local_verify=True), t, tfile,
                        dirs, out, None, None, retry=Retry(cfg, pr, sab, 2, slots))
    before = list(sab.added)
    assert pipeline.placed_before(cfg, Options(), t) == {f.relpath for f in t.real_files}
    picks = [[rel(f"Show.2016.S03E0{i}.720p.HDTV.x264-{'GRPB' if i == 4 else 'GRPA'}", 10**6, f"pick{i}")]
             for i in range(1, 5)]
    slots2 = []
    dirs2, _ = pipeline.usenet_multi(cfg, pr, sab, t, picks, 2, slots2, pipeline.placed_before(cfg, Options(), t))
    assert dirs2 == [] and sab.added == before       # nothing downloaded again
    pipeline.finish(cfg, Options(no_qbit=True), t, tfile, dirs2, out, None, None,
                    retry=Retry(cfg, pr, sab, 2, slots2))
    assert sab.added[len(before):] == ["alt20"]       # only one replacement, for E02
    for name, data in files.items():
        with open(os.path.join(out, PACK, name), "rb") as fh:
            assert fh.read() == data


def test_success_also_cleans_earlier_downloads_of_the_same_torrent(tmp_path):
    t, tfile, cfg, sab, pr, slots, dirs, files = setup(tmp_path, set())
    # an earlier attempt at this torrent left a RAR set behind; another torrent's download too
    for nzo, torrent in (("nzoOLD", t.infohash), ("nzoOTHER", "f" * 40)):
        d = tmp_path / "dl" / nzo
        d.mkdir(parents=True)
        (d / "leftover.part01.rar").write_bytes(b"x" * 100)
        pipeline.ledger_for(cfg).record(nzo, f"Show.2016.S03E01.{nzo}", "", "idx", 1, torrent)
        sab.jobs[nzo] = (f"Show.2016.S03E01.{nzo}", nzo)
    out = str(tmp_path / "complete")
    earlier = pipeline.earlier_dirs(cfg, sab, t)
    assert [os.path.basename(d) for d in earlier] == ["nzoOLD"]
    pipeline.finish(cfg, Options(no_qbit=True), t, tfile, dirs, out, None, None,
                    retry=Retry(cfg, pr, sab, 2, slots), earlier_dirs=earlier)
    assert not (tmp_path / "dl" / "nzoOLD").exists()
    assert (tmp_path / "dl" / "nzoOTHER" / "leftover.part01.rar").exists()
