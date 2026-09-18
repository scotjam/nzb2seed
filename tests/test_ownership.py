"""nzb2seed only ever removes or replaces what it added itself."""
import os

from nzb2seed import assemble as asm
from nzb2seed.torrent import parse

from helpers import BASE, NAME, make_torrent, nzb_job, scene_layout

quiet = dict(log=lambda *_: None)


def setup(tmp_path):
    t = parse(make_torrent(NAME, scene_layout()))
    job = tmp_path / "ssd" / "job"
    nzb_job(str(job))
    out = tmp_path / "hdd" / "complete"
    return t, job, out


def test_foreign_file_of_other_size_blocks_and_nothing_moves(tmp_path):
    t, job, out = setup(tmp_path)
    theirs = out / NAME / f"{BASE}.r00"
    theirs.parent.mkdir(parents=True)
    theirs.write_bytes(b"someone else's data")
    before = sorted(p.name for p in job.iterdir())
    res = asm.assemble(t, [str(job)], str(out), owned=asm.Owned(), **quiet)
    assert res.conflicts and not res.complete
    assert theirs.read_bytes() == b"someone else's data"
    assert sorted(p.name for p in job.iterdir()) == before


def test_foreign_extras_in_existing_torrent_folder_survive_cleanup(tmp_path):
    t, job, out = setup(tmp_path)
    (out / NAME).mkdir(parents=True)
    (out / NAME / "my-notes.txt").write_bytes(b"mine")
    (out / NAME / "Extras").mkdir()
    (out / NAME / "Extras" / "keep.jpg").write_bytes(b"mine too")
    owned = asm.Owned()
    res = asm.assemble(t, [str(job)], str(out), owned=owned, **quiet)
    assert res.complete
    asm.cleanup(t, res, [str(job)], str(out), owned, **quiet)
    assert (out / NAME / "my-notes.txt").read_bytes() == b"mine"
    assert (out / NAME / "Extras" / "keep.jpg").exists()
    assert not job.exists()                      # our own job folder is cleaned up


def test_foreign_file_with_right_size_is_used_untouched(tmp_path):
    t, job, out = setup(tmp_path)
    sample = next(f for f in t.real_files if "Sample" in f.relpath)
    theirs = out / sample.relpath
    theirs.parent.mkdir(parents=True)
    theirs.write_bytes((job / sample.name).read_bytes())
    os.utime(theirs, (1_000_000_000, 1_000_000_000))
    owned = asm.Owned()
    res = asm.assemble(t, [str(job)], str(out), owned=owned, **quiet)
    assert res.complete and "left untouched" in res.how[sample.relpath]
    assert theirs.stat().st_mtime == 1_000_000_000
    assert not owned.owns(str(theirs))
    asm.cleanup(t, res, [str(job)], str(out), owned, **quiet)
    assert theirs.exists() and asm.verify_all(t, res) == []


def test_assemble_mode_never_deletes_from_user_folders(tmp_path):
    t, job, out = setup(tmp_path)
    owned = asm.Owned()
    res = asm.assemble(t, [str(job)], str(out), owned=owned, **quiet)
    asm.cleanup(t, res, [], str(out), owned, **quiet)       # sources are not ours
    assert (job / f"{BASE}.par2").exists() and (job / f"{NAME}.nzb").exists()


def test_only_our_stale_files_and_part_files_are_removed(tmp_path):
    t, job, out = setup(tmp_path)
    record = tmp_path / "torrents" / "x.owned.json"
    owned = asm.Owned(str(record))
    res = asm.assemble(t, [str(job)], str(out), owned=owned, **quiet)
    stale = out / NAME / "leftover-from-earlier-run.bin"
    stale.write_bytes(b"ours")
    owned.add_file(str(stale))
    part = out / NAME / (f"{BASE}.rar" + asm.PART_SUFFIX)
    part.write_bytes(b"half")
    theirs = out / NAME / "theirs.bin"
    theirs.write_bytes(b"not ours")
    owned.save()

    again = asm.Owned(str(record))                            # survives a restart
    assert again.owns(str(stale))
    asm.cleanup(t, res, [str(job)], str(out), again, **quiet)
    assert not stale.exists() and not part.exists() and theirs.exists()


def test_rerun_after_interruption_uses_our_placed_files(tmp_path):
    t, job, out = setup(tmp_path)
    owned = asm.Owned(str(tmp_path / "rec.json"))
    asm.assemble(t, [str(job)], str(out), owned=owned, **quiet)
    owned.save()
    res = asm.assemble(t, [str(job)], str(out), owned=asm.Owned(str(tmp_path / "rec.json")), **quiet)
    assert res.complete and asm.verify_all(t, res) == []
