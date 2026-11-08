"""Matching a torrent's files to files on disk (assemble.find_sources)."""
import os
import time

from nzb2seed import assemble as asm
from nzb2seed.torrent import parse

from helpers import make_torrent, rnd

VIDEO = rnd(200_000, 1)
PROOF = rnd(3_000, 2)             # far smaller than a piece: cannot be hash-checked on its own


def _torrent():
    return parse(make_torrent("Rel", {"rel.mkv": VIDEO, "Proof/rel-proof.jpg": PROOF}, piece_length=65536))


def _put(d, name, data):
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, name), "wb") as fh:
        fh.write(data)


def test_a_small_file_downloaded_several_times_is_matched(tmp_path):
    """The same proof picture in several download folders - one file, nothing to choose."""
    t = _torrent()
    for i in range(3):
        _put(tmp_path / f"dl{i}", "rel-proof.jpg", PROOF)
    _put(tmp_path / "dl0", "rel.mkv", VIDEO)
    got = asm.find_sources(t, [str(tmp_path / f"dl{i}") for i in range(3)])
    assert set(got) == {f.relpath for f in t.real_files}


def test_differing_copies_of_a_small_file_are_not_guessed_between(tmp_path):
    t = _torrent()
    _put(tmp_path / "a", "rel-proof.jpg", PROOF)
    _put(tmp_path / "b", "rel-proof.jpg", rnd(3_000, 9))                    # same size, other bytes
    got = asm.find_sources(t, [str(tmp_path / "a"), str(tmp_path / "b")])
    assert not any(r.endswith("rel-proof.jpg") for r in got)


def test_matching_against_many_folders_stays_quick(tmp_path):
    """A season gathered over many runs: hundreds of folders, thousands of files."""
    files = {f"rel.e{i:03}.mkv": rnd(1_000 + i, i) for i in range(300)}
    t = parse(make_torrent("Rel", files, piece_length=65536))
    dirs = []
    for d in range(200):
        p = tmp_path / f"dl{d}"
        for j in range(20):
            _put(p, f"other{j}.bin", b"x" * (50_000 + d * 20 + j))             # sizes no torrent file has
        dirs.append(str(p))
    _put(tmp_path / "dl0", "rel.e000.mkv", files["rel.e000.mkv"])
    start = time.monotonic()
    got = asm.find_sources(t, dirs)
    assert list(got) == [f.relpath for f in t.real_files if f.name == "rel.e000.mkv"]
    assert time.monotonic() - start < 5


def test_a_copy_moved_away_while_matching_does_not_stop_it(tmp_path, monkeypatch):
    """Another build lays out its files meanwhile: a copy found a moment ago is gone."""
    t = _torrent()
    for i in range(2):
        _put(tmp_path / f"dl{i}", "rel-proof.jpg", PROOF)
    real_open = open

    def gone(p, *a, **kw):
        if str(p).endswith("rel-proof.jpg") and "dl1" in str(p):
            raise FileNotFoundError(p)
        return real_open(p, *a, **kw)
    monkeypatch.setattr("builtins.open", gone)
    got = asm.find_sources(t, [str(tmp_path / "dl0"), str(tmp_path / "dl1")])
    assert not any(r.endswith("rel-proof.jpg") for r in got)      # left unmatched, no crash


def test_the_record_of_placed_files_is_written_while_laying_out(tmp_path, monkeypatch):
    """Stopped part-way (a restart runs no clean-up): what was moved already is on record."""
    t = parse(make_torrent("Rel", {"a.mkv": rnd(70_000, 3), "b.mkv": rnd(80_000, 4)}, piece_length=65536))
    src = tmp_path / "dl"
    _put(src, "a.mkv", rnd(70_000, 3))
    _put(src, "b.mkv", rnd(80_000, 4))
    monkeypatch.setattr(asm, "SAVE_EVERY", 0)
    real_move = asm._move
    moved = []

    def move(a, b, owned, progress=None):
        if moved:
            raise SystemExit("restart")
        moved.append(b)
        real_move(a, b, owned, progress)
    monkeypatch.setattr(asm, "_move", move)
    rec = str(tmp_path / "rec.owned.json")
    try:
        asm.assemble(t, [str(src)], str(tmp_path / "out"), log=lambda *_: None, owned=asm.Owned(rec))
    except SystemExit:
        pass
    assert asm.Owned(rec).owns(moved[0])
