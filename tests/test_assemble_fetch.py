"""assemble: files in none of the folders are downloaded - only those; your folders stay as
they are, and only nzb2seed's own downloads are cleaned up."""
import os

from nzb2seed import pipeline
from nzb2seed.config import Config
from nzb2seed.pipeline import Options
from nzb2seed.torrent import parse

from helpers import make_torrent
from test_gui_login import call, server  # noqa: F401  (fixture)

AUTH = ("admin", "nzb2seed")

FILES = {"Show.S01E01.mkv": b"a" * 40000, "Show.S01E02.mkv": b"b" * 41000}


def test_missing_files_come_from_usenet_and_nothing_of_yours_moves(tmp_path, monkeypatch):
    t_bytes = make_torrent("Show.S01.1080p.WEB.h264-GRPA", FILES)
    tfile = tmp_path / "x.torrent"
    tfile.write_bytes(t_bytes)
    lib = tmp_path / "library"
    lib.mkdir()
    (lib / "Show.S01E01.mkv").write_bytes(FILES["Show.S01E01.mkv"])
    (lib / "notes.txt").write_text("mine")
    asked = {}

    class Sab:
        def __init__(self, *a):
            pass

        def config(self):
            return {"misc": {}}

    def fake_fetch(cfg, opts, pr, sab, t, title, picks, pp, placed):
        asked["placed"] = placed
        d = tmp_path / "downloads" / "Show.S01E02"
        d.mkdir(parents=True)
        (d / "Show.S01E02.mkv").write_bytes(FILES["Show.S01E02.mkv"])
        (d / "Show.S01E02.par2").write_bytes(b"par")
        return [str(d)], ["nzo1"], [], []
    monkeypatch.setattr(pipeline, "SABnzbd", Sab)
    monkeypatch.setattr(pipeline, "plan_and_fetch", fake_fetch)
    out = tmp_path / "out"
    cfg = Config(path=str(tmp_path / "c.toml"), output_dir=str(out), torrent_dir=str(tmp_path / "torrents"))
    res = pipeline.execute_assemble(cfg, Options(no_qbit=True, fetch_missing=True), str(tfile), [str(lib)])

    assert asked["placed"] == {"Show.S01.1080p.WEB.h264-GRPA/Show.S01E01.mkv"}     # only E02 fetched
    t = parse(t_bytes)
    for f in t.real_files:
        assert (out / f.relpath).read_bytes() == FILES[f.name]
    assert sorted(os.listdir(lib)) == ["Show.S01E01.mkv", "notes.txt"]              # your folder: untouched
    assert not (tmp_path / "downloads" / "Show.S01E02" / "Show.S01E02.par2").exists()  # ours: tidied
    assert res["result"]


def test_with_no_folders_everything_comes_from_usenet(tmp_path, monkeypatch):
    """The folders are optional: a .torrent alone is assembled from Usenet entirely."""
    import pytest
    tfile = tmp_path / "x.torrent"
    tfile.write_bytes(make_torrent("Show.S01.1080p.WEB.h264-GRPA", FILES))
    asked = {}

    class Sab:
        def __init__(self, *a):
            pass

        def config(self):
            return {"misc": {}}

    def fake_fetch(cfg, opts, pr, sab, t, title, picks, pp, placed):
        asked["placed"] = placed
        d = tmp_path / "downloads" / "Show.S01"
        d.mkdir(parents=True)
        for n, b in FILES.items():
            (d / n).write_bytes(b)
        return [str(d)], ["nzo1"], [], []
    monkeypatch.setattr(pipeline, "SABnzbd", Sab)
    monkeypatch.setattr(pipeline, "plan_and_fetch", fake_fetch)
    out = tmp_path / "out"
    cfg = Config(path=str(tmp_path / "c.toml"), output_dir=str(out), torrent_dir=str(tmp_path / "torrents"))
    with pytest.raises(pipeline.Abort, match="switched off"):
        pipeline.execute_assemble(cfg, Options(no_qbit=True), str(tfile), [])
    pipeline.execute_assemble(cfg, Options(no_qbit=True, fetch_missing=True), str(tfile), [])
    assert asked["placed"] == set()                                   # every file fetched
    for n, b in FILES.items():
        assert (out / "Show.S01.1080p.WEB.h264-GRPA" / n).read_bytes() == b


def test_the_assemble_form_needs_folders_only_without_downloading(server, tmp_path, monkeypatch):  # noqa: F811
    import threading
    from nzb2seed import gui
    app, url = server
    started = threading.Event()
    monkeypatch.setattr(gui, "execute_assemble", lambda cfg, opts, tpath, sources: started.set() or {"result": "ok"})
    tfile = tmp_path / "x.torrent"
    tfile.write_bytes(make_torrent("Show.S01.1080p.WEB.h264-GRPA", FILES))
    body = {"sources": [""], "torrent_path": str(tfile), "options": {"output_dir": str(tmp_path / "out")}}
    status, r = call(url + "/api/assemble", *AUTH, body=body)
    assert status == 400                                           # no folders, no downloading
    body["options"]["fetch_missing"] = True
    assert call(url + "/api/assemble", *AUTH, body=body)[0] == 200 and started.wait(5)


class LedgerSab:
    """SABnzbd as far as finding earlier downloads goes: each job is finished in a folder."""

    def __init__(self, root):
        self.root = root

    def status(self, nzo):
        return "Completed", {"storage": f"/dl/{nzo}"}

    def config(self):
        return {"misc": {}}


def test_earlier_downloads_of_the_release_are_found_before_any_search(tmp_path, monkeypatch):
    """Built from another tracker (another torrent, another infohash): the download made for
    the first one holds the files - found on disk, by the release's name, not searched for."""
    name = "Show.S01.1080p.WEB.h264-GRPA"
    t = parse(make_torrent(name + ".mkv", {name + ".mkv": FILES["Show.S01E01.mkv"]}))   # a one-file torrent
    d = tmp_path / "dl" / "nzo-first"
    d.mkdir(parents=True)
    (d / "x8f3.mkv").write_bytes(FILES["Show.S01E01.mkv"])
    cfg = Config(path=str(tmp_path / "c.toml"), sab_to_local=[["/dl", str(tmp_path / "dl")]])
    pipeline.ledger_for(cfg).record("nzo-first", name + "-xpost", "g", "IndexerA", None, "f" * 40)
    dirs, have = pipeline.found_on_disk(cfg, LedgerSab(str(tmp_path)), t)
    assert dirs == [str(d)] and have == {f.relpath for f in t.real_files}
    other = Config(path=str(tmp_path / "other" / "c.toml"), sab_to_local=[["/dl", str(tmp_path / "dl")]])
    assert pipeline.found_on_disk(other, LedgerSab(str(tmp_path)), t) == ([], set())     # nothing recorded


def test_assemble_takes_what_earlier_downloads_hold_before_searching(tmp_path, monkeypatch):
    t_bytes = make_torrent("Show.S01.1080p.WEB.h264-GRPA", FILES)
    tfile = tmp_path / "x.torrent"
    tfile.write_bytes(t_bytes)
    t = parse(t_bytes)
    lib = tmp_path / "library"
    lib.mkdir()
    (lib / "Show.S01E01.mkv").write_bytes(FILES["Show.S01E01.mkv"])
    d = tmp_path / "dl" / "nzo-e02"
    d.mkdir(parents=True)
    (d / "Show.S01E02.mkv").write_bytes(FILES["Show.S01E02.mkv"])
    out = tmp_path / "out"
    cfg = Config(path=str(tmp_path / "c.toml"), output_dir=str(out), torrent_dir=str(tmp_path / "torrents"),
                 sab_to_local=[["/dl", str(tmp_path / "dl")]])
    pipeline.ledger_for(cfg).record("nzo-e02", "Show.S01E02.1080p.WEB.h264-GRPA", "g", "IndexerA", None, t.infohash)
    monkeypatch.setattr(pipeline, "SABnzbd", lambda *a: LedgerSab(str(tmp_path)))
    monkeypatch.setattr(pipeline, "plan_and_fetch", lambda *a, **k: (_ for _ in ()).throw(AssertionError("searched")))
    pipeline.execute_assemble(cfg, Options(no_qbit=True, fetch_missing=True), str(tfile), [str(lib)])
    for f in t.real_files:
        assert (out / f.relpath).read_bytes() == FILES[f.name]
