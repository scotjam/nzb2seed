"""Removing builds again after a while - and, above all, removing nothing else."""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from nzb2seed import retention  # noqa: E402
from nzb2seed.config import Config, finalize  # noqa: E402

DAY = 86400


class FakeQbit:
    def __init__(self, torrents):
        self._torrents = torrents
        self.removed = []

    def torrents(self):
        return self._torrents

    def remove(self, infohash, delete_files=False):
        assert delete_files is False, "the files go from nzb2seed's own record, never by hash"
        self.removed.append(infohash)
        self._torrents = [t for t in self._torrents if t["hash"] != infohash]


@pytest.fixture
def setup(tmp_path):
    """A build: two files nzb2seed placed in a folder it created, plus a file it did not."""
    def build(infohash="a" * 40, age_days=40, enabled=True, days=30, extra=True,
              source="auto"):
        torrents = tmp_path / "torrents"
        data = tmp_path / "data" / "The.Release"
        torrents.mkdir(exist_ok=True)
        data.mkdir(parents=True, exist_ok=True)
        ours = []
        for n, size in (("film.mkv", 5000), ("film.nfo", 100)):
            (data / n).write_bytes(b"x" * size)
            ours.append(str(data / n))
        if extra:
            (data / "my-note.txt").write_bytes(b"mine")        # not nzb2seed's
        rec = torrents / f"{infohash}.owned.json"
        stamps = {f: [os.path.getsize(f), round(os.path.getmtime(f), 3)] for f in ours}
        rec.write_text(json.dumps({"files": ours, "dirs": [str(data)],
                                   "source": source, "stamps": stamps}))
        (torrents / f"{infohash}.torrent").write_bytes(b"d4:infod4:name1:xee")
        cfg = finalize(Config(path=tmp_path / "nzb2seed.toml", torrent_dir_raw=str(torrents),
                              retention_enabled=enabled, retention_days=days))
        qb = FakeQbit([{"hash": infohash, "name": "The.Release",
                        "added_on": time.time() - age_days * DAY}])
        return cfg, qb, data, rec
    return build


# ---------------------------------------------------------------- what is due

def test_a_build_past_the_age_is_due(setup):
    cfg, qb, _, _ = setup(age_days=40, days=30)
    assert [b.name for b in retention.due(cfg, qb)] == ["The.Release"]


def test_a_younger_build_is_not(setup):
    cfg, qb, _, _ = setup(age_days=5, days=30)
    assert retention.due(cfg, qb) == []


def test_nothing_is_due_while_retention_is_off(setup):
    cfg, qb, _, _ = setup(age_days=400, enabled=False)
    assert retention.due(cfg, qb) == []


def test_zero_days_means_every_automatic_build(setup):
    """Asking for 0 is how you see the whole list, so it answers with everything."""
    cfg, qb, _, _ = setup(age_days=0, days=0)
    assert [b.name for b in retention.due(cfg, qb)] == ["The.Release"]
    assert [b.name for b in retention.due(cfg, qb, force=True, days=0)] == ["The.Release"]


def test_nothing_is_due_with_a_nonsense_age(setup):
    cfg, qb, _, _ = setup(age_days=400, days=-1)
    assert retention.due(cfg, qb) == []


def test_age_falls_back_to_the_record_when_qbittorrent_has_forgotten_it(setup, tmp_path):
    cfg, _, _, rec = setup(age_days=40)
    old = time.time() - 90 * DAY
    os.utime(rec, (old, old))
    (b,) = retention.builds(cfg, FakeQbit([]))
    assert 89 < b.age_days() < 91


# ---------------------------------------------------------------- what is removed

def test_it_removes_the_torrent_and_only_the_files_it_placed(setup):
    cfg, qb, data, rec = setup()
    out = retention.sweep(cfg, qb, log=lambda *_: None)
    assert qb.removed == ["a" * 40]
    assert not (data / "film.mkv").exists() and not (data / "film.nfo").exists()
    assert (data / "my-note.txt").read_bytes() == b"mine"     # untouched
    assert data.exists()                                       # kept: it is not empty
    assert not rec.exists()                                    # its record goes with it
    assert out["bytes"] == 5100 and out["removed"][0]["files"] == 2


def test_the_folder_goes_when_nothing_of_yours_is_left_in_it(setup):
    cfg, qb, data, _ = setup(extra=False)
    retention.sweep(cfg, qb, log=lambda *_: None)
    assert not data.exists()


def test_a_dry_run_changes_nothing(setup):
    cfg, qb, data, rec = setup()
    out = retention.sweep(cfg, qb, log=lambda *_: None, dry_run=True)
    assert out["dry_run"] and out["bytes"] == 5100
    assert qb.removed == [] and (data / "film.mkv").exists() and rec.exists()


def test_files_are_kept_when_qbittorrent_will_not_let_go(setup):
    """If the torrent cannot be removed it is still seeding - so its files stay."""
    cfg, qb, data, rec = setup()

    def boom(infohash, delete_files=False):
        from nzb2seed.clients import ApiError
        raise ApiError("qBittorrent said no")

    qb.remove = boom
    retention.sweep(cfg, qb, log=lambda *_: None)
    assert (data / "film.mkv").exists() and rec.exists()


def test_a_build_qbittorrent_no_longer_has_is_still_cleaned_up(setup):
    cfg, _, data, rec = setup()
    qb = FakeQbit([])                       # removed from qBittorrent by hand at some point
    old = time.time() - 90 * DAY            # then it is dated by its own record
    os.utime(rec, (old, old))
    retention.sweep(cfg, qb, log=lambda *_: None)
    assert not (data / "film.mkv").exists() and not rec.exists()
    assert qb.removed == []                 # nothing to remove there


def test_torrents_without_a_record_are_never_touched(setup):
    """Only builds nzb2seed made have an .owned.json - everything else is invisible to it."""
    cfg, qb, _, rec = setup()
    rec.unlink()
    qb._torrents.append({"hash": "b" * 40, "name": "Yours", "added_on": time.time() - 900 * DAY})
    assert retention.due(cfg, qb) == []
    retention.sweep(cfg, qb, log=lambda *_: None)
    assert qb.removed == []


def test_a_missing_file_is_counted_not_fatal(setup):
    cfg, qb, data, _ = setup()
    (data / "film.mkv").unlink()            # you moved it yourself
    out = retention.sweep(cfg, qb, log=lambda *_: None)
    assert out["removed"][0]["missing"] == 1 and out["removed"][0]["files"] == 1
    assert qb.removed == ["a" * 40]


# ---------------------------------------------------------------- what the page shows

def test_state_reports_what_is_held(setup):
    cfg, qb, _, _ = setup(age_days=40, days=30)
    st = retention.state(cfg, qb)
    assert st["enabled"] and st["days"] == 30 and st["builds"] == 1 and st["due"] == 1
    assert 39 < st["oldest_days"] < 41 and st["next_name"] == "The.Release"


def test_state_without_qbittorrent_still_counts_the_records(setup):
    cfg, _, _, _ = setup()
    assert retention.state(cfg, None)["builds"] == 1


def test_defaults_are_off_and_thirty_days(tmp_path):
    cfg = finalize(Config(path=tmp_path / "nzb2seed.toml"))
    assert cfg.retention_enabled is False and cfg.retention_days == 30


# ---------------------------------------------------------------- files that changed under us

def test_a_file_bittorrent_re_downloaded_stops_the_whole_removal(setup):
    """A changed file means the torrent really downloaded data. Removing it then could
    leave a hit-and-run, so nothing of that build is touched."""
    cfg, qb, data, rec = setup()
    (data / "film.mkv").write_bytes(b"y" * 5000)         # same size, written again later
    later = time.time() + 3600
    os.utime(data / "film.mkv", (later, later))
    out = retention.sweep(cfg, qb, log=lambda *_: None)
    assert out["removed"] == [] and len(out["kept"]) == 1
    assert "hit-and-run" in out["kept"][0]["skipped"]
    assert qb.removed == []                              # still seeding
    assert (data / "film.mkv").exists() and (data / "film.nfo").exists() and rec.exists()


def test_a_file_that_changed_size_stops_it_too(setup):
    cfg, qb, data, _ = setup()
    st = (data / "film.nfo").stat()
    (data / "film.nfo").write_bytes(b"z" * 999)
    os.utime(data / "film.nfo", (st.st_mtime, st.st_mtime))    # same time, different size
    out = retention.sweep(cfg, qb, log=lambda *_: None)
    assert out["removed"] == [] and qb.removed == []


def test_a_second_of_clock_drift_is_not_a_change(setup):
    cfg, qb, data, _ = setup()
    st = (data / "film.mkv").stat()
    os.utime(data / "film.mkv", (st.st_mtime + 1, st.st_mtime + 1))
    out = retention.sweep(cfg, qb, log=lambda *_: None)
    assert len(out["removed"]) == 1 and qb.removed == ["a" * 40]


def test_records_written_before_stamps_are_left_alone(setup):
    """An old record cannot say whether BitTorrent has been at the files. It is only
    stamped when qBittorrent can vouch that nothing was downloaded (see the stamping
    tests); when it cannot, the build is kept."""
    cfg, qb, data, rec = setup()
    d = json.loads(rec.read_text())
    del d["stamps"]
    rec.write_text(json.dumps(d))
    qb._torrents[0]["downloaded"] = 4096            # BitTorrent has been at it
    out = retention.sweep(cfg, qb, log=lambda *_: None)
    assert out["removed"] == [] and "by hand" in out["kept"][0]["skipped"]
    assert (data / "film.mkv").exists() and qb.removed == []


def test_a_file_already_gone_does_not_block_the_rest(setup):
    cfg, qb, data, _ = setup()
    (data / "film.nfo").unlink()
    out = retention.sweep(cfg, qb, log=lambda *_: None)
    assert len(out["removed"]) == 1 and not (data / "film.mkv").exists()


# ---------------------------------------------------------------- previewing while it is off

def test_a_preview_answers_while_retention_is_off(setup):
    """Seeing what would go is how you decide whether to switch it on."""
    cfg, qb, data, _ = setup(age_days=40, days=30, enabled=False)
    assert retention.due(cfg, qb) == []                       # the sweep itself stays idle
    assert [b.name for b in retention.due(cfg, qb, force=True)] == ["The.Release"]
    out = retention.sweep(cfg, qb, log=lambda *_: None, dry_run=True, force=True)
    assert len(out["removed"]) == 1 and out["was_off"] is True
    assert (data / "film.mkv").exists() and qb.removed == []  # a preview changes nothing


def test_a_preview_can_try_an_age_that_is_not_saved(setup):
    cfg, qb, _, _ = setup(age_days=40, days=30)
    assert retention.sweep(cfg, qb, log=lambda *_: None, dry_run=True, days=90)["removed"] == []
    out = retention.sweep(cfg, qb, log=lambda *_: None, dry_run=True, days=7)
    assert len(out["removed"]) == 1 and out["days"] == 7
    assert cfg.retention_days == 30                           # the saved age is untouched


def test_state_counts_what_is_due_even_while_off(setup):
    cfg, qb, _, _ = setup(age_days=40, days=30, enabled=False)
    st = retention.state(cfg, qb)
    assert st["enabled"] is False and st["due"] == 1
    assert retention.state(cfg, qb, days=90)["due"] == 0


# ---------------------------------------------------------------- the qBittorrent category

def test_builds_are_filed_under_their_own_category_by_default(tmp_path):
    cfg = finalize(Config(path=tmp_path / "nzb2seed.toml"))
    assert cfg.qbit_category == "nzb2seed"


def test_an_empty_category_still_means_nzb2seed(tmp_path):
    """Configs written before this - category = "" - get it too."""
    cfg = finalize(Config(path=tmp_path / "nzb2seed.toml", qbit_category=""))
    assert cfg.qbit_category == "nzb2seed"


def test_a_category_you_chose_is_kept(tmp_path):
    cfg = finalize(Config(path=tmp_path / "nzb2seed.toml", qbit_category="films"))
    assert cfg.qbit_category == "films"


def test_a_single_dash_means_no_category_at_all(tmp_path):
    cfg = finalize(Config(path=tmp_path / "nzb2seed.toml", qbit_category="-"))
    assert cfg.qbit_category == ""


# ---------------------------------------------------------------- data that moved away

def test_a_build_whose_files_moved_is_left_alone(setup):
    """Its data was moved into a library, so qBittorrent seeds it from elsewhere. Removing
    the torrent would stop a healthy seed and free nothing."""
    cfg, qb, data, rec = setup()
    for n in ("film.mkv", "film.nfo", "my-note.txt"):
        (data / n).unlink()
    qb._torrents[0]["content_path"] = "/library/Films/The.Release"
    out = retention.sweep(cfg, qb, log=lambda *_: None)
    assert out["removed"] == [] and qb.removed == [] and rec.exists()
    assert "/library/Films/The.Release" in out["kept"][0]["skipped"]
    assert "free nothing" in out["kept"][0]["skipped"]


def test_a_record_listing_no_files_is_left_alone(setup):
    cfg, qb, _, rec = setup()
    rec.write_text(json.dumps({"files": [], "dirs": [], "source": "auto", "stamps": {}}))
    out = retention.sweep(cfg, qb, log=lambda *_: None)
    assert out["removed"] == [] and qb.removed == []
    assert "lists no files" in out["kept"][0]["skipped"]


# ---------------------------------------------------------------- only automatic builds

def _source(rec, value):
    d = json.loads(rec.read_text())
    if value is None:
        d.pop("source", None)
    else:
        d["source"] = value
    rec.write_text(json.dumps(d))


def test_only_builds_the_automatic_tab_made_are_removed(setup):
    cfg, qb, data, rec = setup()
    _source(rec, "auto")
    out = retention.sweep(cfg, qb, log=lambda *_: None)
    assert len(out["removed"]) == 1 and not (data / "film.mkv").exists()


def test_a_build_you_asked_for_yourself_is_never_removed(setup):
    """run, season and assemble write "manual": retention does not see them at all."""
    cfg, qb, data, rec = setup(age_days=9999)
    _source(rec, "manual")
    assert retention.builds(cfg, qb) == [] and retention.due(cfg, qb, force=True) == []
    retention.sweep(cfg, qb, log=lambda *_: None)
    assert (data / "film.mkv").exists() and qb.removed == [] and rec.exists()


def test_an_older_record_counts_as_automatic_only_if_the_inbox_built_it(setup, tmp_path):
    cfg, qb, data, rec = setup()
    _source(rec, None)                                   # written before source was recorded
    assert retention.builds(cfg, qb) == []               # unknown: left out
    (tmp_path / "inbox.json").write_text(json.dumps({"items": {"a" * 40: {"name": "The.Release"}}}))
    assert [b.infohash for b in retention.builds(cfg, qb)] == ["a" * 40]


def test_state_counts_only_automatic_builds(setup):
    cfg, qb, _, rec = setup()
    _source(rec, "manual")
    assert retention.state(cfg, qb)["builds"] == 0


# ---------------------------------------------------------------- stamping older builds

def _unstamp(rec):
    d = json.loads(rec.read_text())
    d.pop("stamps", None)
    rec.write_text(json.dumps(d))


def test_a_build_bittorrent_never_downloaded_gets_stamped_and_can_go(setup):
    """Nothing came over BitTorrent, so what is on disk is what nzb2seed wrote."""
    cfg, qb, data, rec = setup()
    _unstamp(rec)
    qb._torrents[0]["downloaded"] = 0
    out = retention.sweep(cfg, qb, log=lambda *_: None)
    assert len(out["removed"]) == 1 and not (data / "film.mkv").exists()


def test_a_build_with_any_bittorrent_download_is_never_stamped(setup):
    cfg, qb, data, rec = setup()
    _unstamp(rec)
    qb._torrents[0]["downloaded"] = 1          # one byte is enough to disqualify it
    out = retention.sweep(cfg, qb, log=lambda *_: None)
    assert out["removed"] == [] and qb.removed == []
    assert (data / "film.mkv").exists()
    assert "stamps" not in json.loads(rec.read_text()) or not json.loads(rec.read_text())["stamps"]


def test_stamping_happens_on_a_preview_too_but_deletes_nothing(setup):
    cfg, qb, data, rec = setup()
    _unstamp(rec)
    qb._torrents[0]["downloaded"] = 0
    out = retention.sweep(cfg, qb, log=lambda *_: None, dry_run=True)
    assert len(out["removed"]) == 1                          # now judged, not guessed at
    assert (data / "film.mkv").exists() and qb.removed == []  # and nothing was touched
    assert json.loads(rec.read_text())["stamps"]              # the record learned the sizes


def test_a_build_qbittorrent_does_not_know_is_not_stamped(setup):
    cfg, qb, _, rec = setup()
    _unstamp(rec)
    out = retention.sweep(cfg, FakeQbit([]), log=lambda *_: None)
    assert out["removed"] == [] and not json.loads(rec.read_text()).get("stamps")


def test_stamping_leaves_an_already_stamped_record_alone(setup):
    cfg, qb, data, rec = setup()
    before = json.loads(rec.read_text())["stamps"]
    qb._torrents[0]["downloaded"] = 999                      # would disqualify it if re-stamped
    retention.backfill(cfg, qb, retention.builds(cfg, qb), log=lambda *_: None)
    assert json.loads(rec.read_text())["stamps"] == before
