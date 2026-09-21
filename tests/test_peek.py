"""Looking inside a RAR post before downloading all of it.

A post can carry a RAR set whose posted file names say nothing about what is inside
(obfuscated, or simply the release name). Downloading 28 GB to find out it holds another
encode is the expensive mistake this avoids: only the first volume is fetched, and its
headers name and size what the set holds.
"""
import os
import sys
from dataclasses import dataclass

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from nzb2seed import nzbinfo, pipeline  # noqa: E402


@dataclass
class F:
    name: str
    length: int


def nzb(*names: str, segs: int = 1) -> bytes:
    files = "".join(
        f'<file subject="[1/9] &quot;{n}&quot; yEnc (1/{segs})">'
        f'<segments><segment bytes="1000" number="1">{n}.msg@x</segment></segments></file>'
        for n in names)
    return f'<?xml version="1.0"?><nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">{files}</nzb>'.encode()


# ---------------------------------------------------------------- first_volume

@pytest.mark.parametrize("names, want", [
    (["r.part01.rar", "r.part02.rar", "r.part03.rar"], "r.part01.rar"),
    (["r.part02.rar", "r.part01.rar"], "r.part01.rar"),
    (["r.rar", "r.r00", "r.r01"], "r.rar"),
    (["r.001", "r.002"], "r.001"),
    (["film.mkv", "film.nfo"], None),
    (["r.vol000+01.par2", "r.par2"], None),
])
def test_first_volume(names, want):
    assert nzbinfo.first_volume(nzbinfo.parse(nzb(*names))) == want


def test_first_volume_ignores_par2_that_looks_numbered():
    files = nzbinfo.parse(nzb("set.part01.rar", "set.vol001+02.par2"))
    assert nzbinfo.first_volume(files) == "set.part01.rar"


# ---------------------------------------------------------------- archive_holds

WANT = [F("The.Film.2019.mkv", 26_540_000_000), F("the.film.nfo", 4_000)]


def test_holds_when_a_size_matches_exactly():
    entries = [("obfuscated-name.mkv", 26_540_000_000)]
    assert pipeline.archive_holds(WANT, entries) is True


def test_right_name_wrong_size_is_another_encode():
    entries = [("The.Film.2019.mkv", 27_940_000_000)]
    assert pipeline.archive_holds(WANT, entries) is False


def test_a_file_as_big_as_the_one_wanted_that_is_not_it():
    entries = [("Some.Other.Remux.mkv", 27_940_000_000)]
    assert pipeline.archive_holds(WANT, entries) is False


def test_only_small_files_listed_says_nothing():
    # the rest of the set's headers live in volumes that were not fetched
    assert pipeline.archive_holds(WANT, [("readme.txt", 900)]) is None


def test_nothing_listed_says_nothing():
    assert pipeline.archive_holds(WANT, []) is None
    assert pipeline.archive_holds([], [("x.mkv", 5)]) is None


# ---------------------------------------------------------------- worth_peeking

def test_worth_peeking_only_for_rar_posts_that_name_nothing_we_want():
    assert pipeline.worth_peeking(nzb("set.part01.rar", "set.part02.rar"), WANT) is True


def test_not_worth_peeking_when_the_post_names_the_torrents_file():
    assert pipeline.worth_peeking(nzb("set.part01.rar", "The.Film.2019.mkv"), WANT) is False


def test_not_worth_peeking_without_archives():
    assert pipeline.worth_peeking(nzb("something.mkv"), WANT) is False


def test_broken_nzb_is_not_peeked():
    assert pipeline.worth_peeking(b"not xml", WANT) is False


# ---------------------------------------------------------------- peek_archive

class FakeSab:
    """Accepts the trimmed NZB, "downloads" it into a folder, remembers what it was asked."""

    def __init__(self, tmp, entries):
        self.tmp, self.entries = tmp, entries
        self.added, self.history_deleted = [], []

    def add_nzb(self, data, name, category, pp, priority=0):
        self.added.append((data, name, pp))
        d = os.path.join(self.tmp, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "set.part01.rar"), "wb") as fh:
            fh.write(b"rar" * 100)
        self.job = d
        return "nzo1"

    def status(self, nzo):
        return "Completed", {"storage": self.job}

    def delete_history(self, nzo):
        self.history_deleted.append(nzo)


@pytest.fixture
def cfg(tmp_path):
    from nzb2seed.config import Config, finalize
    return finalize(Config(path=tmp_path / "nzb2seed.toml", sab_category="nzb2seed"))


def run_peek(monkeypatch, cfg, tmp_path, entries):
    sab = FakeSab(str(tmp_path), entries)
    monkeypatch.setattr(pipeline.archives, "list_contents", lambda first: entries)
    rel = type("R", (), {"title": "The.Film.2019.REMUX-GRP", "indexer": "anIndexer",
                         "size": 27_940_000_000, "guid": "g"})()
    out = pipeline.peek_archive(cfg, None, sab, rel, nzb("set.part01.rar", "set.part02.rar"), WANT)
    return out, sab


def test_peek_rejects_another_encode(monkeypatch, cfg, tmp_path):
    out, sab = run_peek(monkeypatch, cfg, tmp_path, [("The.Film.2019.mkv", 27_940_000_000)])
    assert out is False
    trimmed, name, pp = sab.added[0]
    assert b"set.part01.rar" in trimmed and b"set.part02.rar" not in trimmed   # one volume only
    assert name.startswith(pipeline.PEEK_PREFIX)
    assert pp == pipeline.PP_REPAIR                                            # never unpack/delete


def test_peek_accepts_the_right_release(monkeypatch, cfg, tmp_path):
    out, _ = run_peek(monkeypatch, cfg, tmp_path, [("obf.mkv", 26_540_000_000)])
    assert out is True


def test_peek_cleans_up_after_itself(monkeypatch, cfg, tmp_path):
    out, sab = run_peek(monkeypatch, cfg, tmp_path, [("The.Film.2019.mkv", 27_940_000_000)])
    assert out is False
    assert not os.path.isdir(sab.job)          # the volume is not left on the disk
    assert sab.history_deleted == ["nzo1"]     # and not left in SABnzbd's history


def test_peek_gives_no_verdict_when_sabnzbd_fails(monkeypatch, cfg, tmp_path):
    sab = FakeSab(str(tmp_path), [])
    monkeypatch.setattr(sab, "status", lambda nzo: ("Failed", {"fail_message": "no articles"}))
    rel = type("R", (), {"title": "T", "indexer": "i", "size": 1, "guid": "g"})()
    assert pipeline.peek_archive(cfg, None, sab, rel, nzb("set.part01.rar"), WANT) is None


# ---------------------------------------------------------------- saying what was wrong

def test_the_peek_says_what_it_wanted_and_what_was_there(monkeypatch, cfg, tmp_path, capsys):
    """"none of the needed files are inside" does not tell you whether it was the wrong
    release or the wrong size - so both sides are named, with their sizes."""
    said = []
    monkeypatch.setattr(pipeline, "info", said.append)
    out, _ = run_peek(monkeypatch, cfg, tmp_path, [("Their.Cut.mkv", 27_940_000_000)])
    assert out is False
    line = next(x for x in said if "looking for" in x)
    assert "The.Film.2019.mkv (24.72 GB)" in line          # what the torrent needs
    assert "Their.Cut.mkv (26.02 GB)" in line              # what the archive actually holds
    assert "a different release" in line


def test_the_peek_says_what_it_found_when_it_accepts(monkeypatch, cfg, tmp_path):
    said = []
    monkeypatch.setattr(pipeline, "info", said.append)
    out, _ = run_peek(monkeypatch, cfg, tmp_path, [("obf.mkv", 26_540_000_000)])
    assert out is True
    assert any("obf.mkv (24.72 GB)" in x and "what the torrent" in x for x in said)
