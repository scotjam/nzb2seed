import os

from nzb2seed import assemble as asm
from nzb2seed.torrent import parse

from helpers import BASE, NAME, NFO_CRLF, make_torrent, nzb_job, scene_layout


def _tree(root):
    out = set()
    for d, _, names in os.walk(root):
        for n in names:
            out.add(os.path.relpath(os.path.join(d, n), root).replace("\\", "/"))
    return out


def test_flat_nzb_becomes_torrent_layout(tmp_path):
    t = parse(make_torrent(NAME, scene_layout()))
    job = tmp_path / "complete" / "Some.Job.Name"
    nzb_job(str(job))
    out = str(tmp_path / "complete")

    res = asm.assemble(t, [str(job)], out, log=lambda *_: None)
    assert res.complete, (res.missing, res.wrong_size, res.notes)
    assert asm.verify_all(t, res) == []

    root = os.path.join(out, NAME)
    for sub in ("Sample", "Proof", "Subs"):
        assert os.path.isdir(os.path.join(root, sub))
    with open(os.path.join(root, f"{BASE}.nfo"), "rb") as fh:
        assert fh.read() == NFO_CRLF
    assert "CRLF" in res.how[f"{NAME}/{BASE}.nfo"]

    removed = asm.cleanup(t, res, [str(job)], out, log=lambda *_: None)
    names = {os.path.basename(p) for p in removed}
    assert {f"{BASE}.par2", f"{NAME}.nzb", "readme_from_poster.txt"} <= names
    assert not job.exists()
    assert _tree(out) == {f"{NAME}/{rel}" for rel in scene_layout()}


def test_job_folder_already_named_like_torrent(tmp_path):
    t = parse(make_torrent(NAME, scene_layout()))
    out = tmp_path / "complete"
    job = out / NAME
    nzb_job(str(job))
    res = asm.assemble(t, [str(job)], str(out), log=lambda *_: None)
    assert res.complete
    asm.cleanup(t, res, [str(job)], str(out), log=lambda *_: None)
    assert _tree(str(out)) == {f"{NAME}/{rel}" for rel in scene_layout()}
    assert asm.verify_all(t, res) == []


def test_obfuscated_same_size_files_resolved_by_hash(tmp_path):
    t = parse(make_torrent(NAME, scene_layout()))
    job = tmp_path / "complete" / "job"
    nzb_job(str(job), obfuscate=[f"{BASE}.r00", f"{BASE}.r01"])
    res = asm.assemble(t, [str(job)], str(tmp_path / "complete"), log=lambda *_: None)
    assert res.complete, res.notes
    assert asm.verify_all(t, res) == []
    assert "piece hash" in res.how[f"{NAME}/{BASE}.r00"]


def test_missing_file_reported_and_nothing_cleaned(tmp_path):
    t = parse(make_torrent(NAME, scene_layout()))
    job = tmp_path / "complete" / "job"
    nzb_job(str(job))
    os.remove(job / f"{BASE}-sample.mkv")
    res = asm.assemble(t, [str(job)], str(tmp_path / "complete"), log=lambda *_: None)
    assert not res.complete
    assert [f.relpath for f in res.missing] == [f"{NAME}/Sample/{BASE}-sample.mkv"]


def test_nfo_that_cannot_fit_is_reported(tmp_path):
    t = parse(make_torrent(NAME, scene_layout()))
    job = tmp_path / "complete" / "job"
    nzb_job(str(job))
    with open(job / f"{BASE}.nfo", "wb") as fh:
        fh.write(b"totally different nfo\n")
    res = asm.assemble(t, [str(job)], str(tmp_path / "complete"), log=lambda *_: None)
    assert any("no line-ending variant" in n for n in res.notes)
    assert not res.complete  # wrong size


def test_rerun_is_idempotent(tmp_path):
    t = parse(make_torrent(NAME, scene_layout()))
    job = tmp_path / "complete" / "job"
    nzb_job(str(job))
    out = str(tmp_path / "complete")
    asm.assemble(t, [str(job)], out, log=lambda *_: None)
    res = asm.assemble(t, [str(job)], out, log=lambda *_: None)
    assert res.complete
    assert asm.verify_all(t, res) == []


def test_eol_variants():
    v = asm.eol_variants(b"a\r\nb\r\n")
    assert b"a\nb\n" in v and b"a\rb\r" in v and b"a\r\nb" in v


def test_single_file_torrent(tmp_path):
    from helpers import rnd
    data = rnd(70_000, 9)
    import hashlib
    from nzb2seed import bencode
    pieces = b"".join(hashlib.sha1(data[i:i + 16384]).digest() for i in range(0, len(data), 16384))
    raw = bencode.encode({"info": {"name": "movie.mkv", "length": len(data),
                                   "piece length": 16384, "pieces": pieces}})
    t = parse(raw)
    job = tmp_path / "complete" / "Movie.Job"
    job.mkdir(parents=True)
    (job / "obfuscated123.mkv").write_bytes(data)
    (job / "movie.par2").write_bytes(b"x")
    res = asm.assemble(t, [str(job)], str(tmp_path / "complete"), log=lambda *_: None)
    assert res.complete
    asm.cleanup(t, res, [str(job)], str(tmp_path / "complete"), log=lambda *_: None)
    assert _tree(str(tmp_path / "complete")) == {"movie.mkv"}


# ---------------------------------------------------------------- text files: verify before touching

def _pack(nfo_endings):
    """Season pack; each episode's nfo uses the given line ending in the torrent."""
    from helpers import rnd
    files = {}
    for i, eol in enumerate(nfo_endings, 1):
        ep = f"show.s01e0{i}.1080p.bluray.x264-grp"
        files[f"{ep}/{ep}.rar"] = rnd(30_000, 10 + i)
        files[f"{ep}/{ep}.nfo"] = (b"GRP\n\nShow E0%d\n" % i).replace(b"\n", eol)
        files[f"{ep}/{ep}.sfv"] = (b"; sfv\n%s.rar 0000000%d\n" % (ep.encode(), i)).replace(b"\n", eol)
    return files


def _flat_lf(job, files):
    job.mkdir(parents=True)
    for rel, data in files.items():
        if rel.endswith((".nfo", ".sfv")):
            data = data.replace(b"\r\n", b"\n")
        (job / rel.split("/")[-1]).write_bytes(data)


def test_lf_nfo_in_torrent_is_never_touched_while_crlf_one_is_fixed(tmp_path):
    files = _pack([b"\r\n", b"\n", b"\r\n", b"\n"])  # E04 is LF-only by design
    t = parse(make_torrent("Show.S01.1080p.BluRay.x264-GRP", files, piece_length=16384))
    job = tmp_path / "complete" / "job"
    _flat_lf(job, files)
    e04 = job / "show.s01e04.1080p.bluray.x264-grp.nfo"
    mtime = e04.stat().st_mtime_ns

    res = asm.assemble(t, [str(job)], str(tmp_path / "complete"), log=lambda *_: None)
    assert res.complete, res.notes
    assert asm.verify_all(t, res) == []
    rel = "Show.S01.1080p.BluRay.x264-GRP/show.s01e04.1080p.bluray.x264-grp/show.s01e04.1080p.bluray.x264-grp.nfo"
    assert "unchanged" in res.how[rel]
    assert os.stat(res.placed[rel]).st_mtime_ns == mtime  # moved, never rewritten
    rel1 = rel.replace("e04", "e01")
    assert "LF -> CRLF" in res.how[rel1] and "piece hash" in res.how[rel1]


def test_nfo_with_right_size_but_wrong_content_is_left_alone(tmp_path):
    t = parse(make_torrent(NAME, scene_layout()))
    job = tmp_path / "complete" / "job"
    nzb_job(str(job))
    bogus = NFO_CRLF.replace(b"GRP", b"XYZ")  # same size as the torrent's nfo, wrong bytes
    (job / f"{BASE}.nfo").write_bytes(bogus)
    res = asm.assemble(t, [str(job)], str(tmp_path / "complete"), log=lambda *_: None)
    assert (tmp_path / "complete" / NAME / f"{BASE}.nfo").read_bytes() == bogus
    assert any("left as downloaded" in n for n in res.notes)


def test_nfo_not_rewritten_when_neighbour_missing(tmp_path):
    t = parse(make_torrent(NAME, scene_layout()))
    job = tmp_path / "complete" / "job"
    nzb_job(str(job))
    os.remove(job / f"{BASE}.r02")  # the file sharing a piece with the nfo
    lf = (job / f"{BASE}.nfo").read_bytes()
    res = asm.assemble(t, [str(job)], str(tmp_path / "complete"), log=lambda *_: None)
    assert (tmp_path / "complete" / NAME / f"{BASE}.nfo").read_bytes() == lf
    assert not res.complete


def test_dry_run_decides_without_writing(tmp_path):
    t = parse(make_torrent(NAME, scene_layout()))
    job = tmp_path / "complete" / "job"
    nzb_job(str(job))
    before = {p.name: p.read_bytes() for p in job.iterdir()}
    res = asm.assemble(t, [str(job)], str(tmp_path / "complete"), dry_run=True, log=lambda *_: None)
    assert "LF -> CRLF" in res.how[f"{NAME}/{BASE}.nfo"]
    assert {p.name: p.read_bytes() for p in job.iterdir()} == before


def test_cross_disk_move_uses_part_file(tmp_path, monkeypatch):
    import errno
    real_replace = os.replace
    seen = []

    def fake_replace(src, dst):
        # simulate two filesystems: renames out of the job folder fail with EXDEV
        if "job" in str(src) and "job" not in str(dst):
            raise OSError(errno.EXDEV, "cross-device link")
        seen.append(str(src))
        return real_replace(src, dst)
    monkeypatch.setattr(asm.os, "replace", fake_replace)

    t = parse(make_torrent(NAME, scene_layout()))
    job = tmp_path / "ssd" / "job"
    nzb_job(str(job))
    out = str(tmp_path / "hdd" / "complete")
    copied = []
    res = asm.assemble(t, [str(job)], out, log=lambda *_: None,
                       progress=lambda n, d, tot: copied.append((n, d, tot)))
    assert res.complete and asm.verify_all(t, res) == []
    assert any(s.endswith(asm.PART_SUFFIX) for s in seen)       # renamed into place from .part
    assert copied and all(d <= tot for _, d, tot in copied)
    asm.cleanup(t, res, [str(job)], out, log=lambda *_: None)
    assert not job.exists()
    assert not any(p.name.endswith(asm.PART_SUFFIX) for p in (tmp_path / "hdd").rglob("*"))
