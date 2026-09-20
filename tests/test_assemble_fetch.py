"""assemble: files in none of the folders are downloaded - only those; your folders stay as
they are, and only nzb2seed's own downloads are cleaned up."""
import os

from nzb2seed import pipeline
from nzb2seed.config import Config
from nzb2seed.pipeline import Options
from nzb2seed.torrent import parse

from helpers import make_torrent

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
