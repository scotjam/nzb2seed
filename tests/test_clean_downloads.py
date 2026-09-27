"""Usenet downloads are cleared once their torrent is complete in qBittorrent - and only then."""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from nzb2seed import pipeline  # noqa: E402
from nzb2seed.config import Config  # noqa: E402


class Qbit:
    def __init__(self, torrents):
        self._t = torrents

    def torrents(self):
        return self._t


class Sab:
    def __init__(self, slots):
        self.slots, self.deleted = slots, []

    def history_slot(self, nzo):
        return self.slots.get(nzo)

    def queue_slot(self, nzo):
        return None

    def _call(self, mode, **kw):
        assert mode == "history"
        return {"history": {"slots": [dict(v, nzo_id=k) for k, v in self.slots.items()]}}

    def config(self):
        return {"misc": {"complete_dir": "/downloads"}}

    def delete_history(self, nzo, files=False):
        assert not files            # SABnzbd keeps a completed job's files anyway: we remove them
        self.deleted.append(nzo)

    def incomplete_dir(self):
        return "/downloads/incomplete"


def setup(tmp_path, torrents):
    dl = tmp_path / "downloads"
    cfg = Config(path=str(tmp_path / "c.toml"), sab_to_local=[["/downloads", str(dl)]],
                 local_to_qbit=[[str(tmp_path / "out"), "/data"]])
    led = pipeline.ledger_for(cfg)
    slots = {}
    for nzo, torrent in (("done1", "a" * 40), ("half1", "b" * 40), ("seedhere", "c" * 40)):
        d = dl / nzo
        d.mkdir(parents=True)
        (d / "x.mkv").write_bytes(b"x" * 1000)
        led.record(nzo, nzo, torrent=torrent)
        slots[nzo] = {"storage": f"/downloads/{nzo}"}
    return cfg, Sab(slots), Qbit(torrents), dl


TORRENTS = [{"hash": "a" * 40, "progress": 1, "content_path": "/data/A"},
            {"hash": "b" * 40, "progress": 0.97, "content_path": "/data/B"},
            {"hash": "c" * 40, "progress": 1, "content_path": None}]


def test_a_complete_torrents_download_is_cleared_and_others_kept(tmp_path):
    cfg, sab, qb, dl = setup(tmp_path, TORRENTS)
    n, freed = pipeline.clean_seeded_downloads(cfg, sab, qb, log=lambda *_: None)
    assert "done1" in sab.deleted and "half1" not in sab.deleted       # a retry may need it
    assert freed >= 1000
    assert not (dl / "done1").exists() and (dl / "half1").exists()     # the folder itself went


def test_a_folder_a_torrent_seeds_from_is_kept(tmp_path):
    torrents = [dict(t) for t in TORRENTS]
    torrents[2]["content_path"] = str(tmp_path / "downloads" / "seedhere")   # seeding from $downloads
    cfg, sab, qb, dl = setup(tmp_path, torrents)
    cfg.local_to_qbit = []
    pipeline.clean_seeded_downloads(cfg, sab, qb, log=lambda *_: None)
    assert "seedhere" not in sab.deleted


def test_a_folder_a_running_build_uses_is_kept_until_it_ends(tmp_path):
    cfg, sab, qb, dl = setup(tmp_path, TORRENTS)
    using, finish = threading.Event(), threading.Event()

    def build():                                    # reusing done1's download for another torrent
        pipeline.claim(str(dl / "done1"))
        using.set()
        finish.wait(5)
    th = threading.Thread(target=build)
    th.start()
    using.wait(5)
    pipeline.clean_seeded_downloads(cfg, sab, qb, log=lambda *_: None)
    assert "done1" not in sab.deleted
    finish.set()
    th.join()
    pipeline.clean_seeded_downloads(cfg, sab, qb, log=lambda *_: None)
    assert "done1" in sab.deleted                   # its claim went with the build


def test_without_qbittorrent_nothing_is_cleared(tmp_path):
    cfg, sab, qb, dl = setup(tmp_path, TORRENTS)
    assert pipeline.clean_seeded_downloads(cfg, sab, Qbit([]), log=lambda *_: None) == (0, 0)
    assert sab.deleted == []


def test_a_folder_shared_with_a_download_still_needed_is_kept(tmp_path):
    """Two SABnzbd entries left one folder: one for a complete torrent, one for a torrent
    that is not complete. A retry of the second may need it, so it stays."""
    cfg, sab, qb, dl = setup(tmp_path, TORRENTS)
    sab.slots["half1"]["storage"] = "/downloads/done1"
    pipeline.clean_seeded_downloads(cfg, sab, qb, log=lambda *_: None)
    assert "done1" not in sab.deleted and "half1" not in sab.deleted


def test_a_folder_shared_by_two_complete_downloads_goes_once(tmp_path):
    torrents = [dict(t) for t in TORRENTS]
    torrents[1]["progress"] = 1
    cfg, sab, qb, dl = setup(tmp_path, torrents)
    sab.slots["half1"]["storage"] = "/downloads/done1"
    n, freed = pipeline.clean_seeded_downloads(cfg, sab, qb, log=lambda *_: None)
    assert sorted(sab.deleted) == ["done1", "half1", "seedhere"] and n == 2   # two folders


def test_a_folder_that_cannot_be_removed_keeps_its_history_entry(tmp_path, monkeypatch):
    """So a later run still finds it, rather than losing track of it as happened when
    SABnzbd was trusted to delete a completed job's files."""
    cfg, sab, qb, dl = setup(tmp_path, TORRENTS)

    def fail(p):
        raise OSError("busy")
    monkeypatch.setattr(pipeline.shutil, "rmtree", fail)
    pipeline.clean_seeded_downloads(cfg, sab, qb, log=lambda *_: None)
    assert "done1" not in sab.deleted and (dl / "done1").exists()


def test_removing_a_finished_download_removes_its_folder(tmp_path):
    """Abandon uses this: SABnzbd would drop the history entry and keep the files."""
    cfg, sab, qb, dl = setup(tmp_path, TORRENTS)
    sab.queue_slot = lambda nzo: None
    assert pipeline.remove_download(cfg, sab, "half1") is True
    assert not (dl / "half1").exists() and sab.deleted == ["half1"]


def test_sabnzbds_own_folder_is_never_removed(tmp_path):
    cfg, sab, qb, dl = setup(tmp_path, TORRENTS)
    sab.slots["half1"]["storage"] = "/downloads"
    pipeline.remove_download(cfg, sab, "half1")
    assert dl.exists() and (dl / "done1").exists()


# ---------------------------------------------------------------- remove and delete

class QueueSab(Sab):
    def __init__(self, slots, queued=()):
        super().__init__(slots)
        self.queued, self.queue_deleted = set(queued), []

    def queue_slot(self, nzo):
        return {"nzo_id": nzo} if nzo in self.queued else None

    def queue_do(self, action, nzo):
        assert action == "delete"
        self.queue_deleted.append(nzo)


def test_a_jobs_downloads_are_deleted_when_nothing_depends_on_them(tmp_path):
    cfg, _, qb, dl = setup(tmp_path, TORRENTS)
    sab = QueueSab(setup(tmp_path / "x", TORRENTS)[1].slots, queued={"part"})
    sab.slots = {k: v for k, v in sab.slots.items()}
    pipeline.ledger_for(cfg).record("part", "part", torrent="b" * 40)
    out = pipeline.delete_downloads_of(cfg, sab, Qbit([]), {"b" * 40})
    assert not (dl / "half1").exists() and "half1" in sab.deleted             # finished download
    assert sab.queue_deleted == ["part"]                                      # part download
    assert (dl / "done1").exists() and out["deleted"] == 2                    # another torrent's: kept


def test_a_download_a_torrent_uses_is_never_deleted(tmp_path):
    cfg, sab, qb, dl = setup(tmp_path, TORRENTS)
    cfg.local_to_qbit = []
    uses = [{"hash": "z" * 40, "progress": 1, "content_path": str(dl / "half1")}]
    out = pipeline.delete_downloads_of(cfg, sab, Qbit(uses), {"b" * 40})
    assert (dl / "half1").exists() and "uses it" in out["kept"][0]


def test_a_folder_shared_with_another_download_is_kept(tmp_path):
    cfg, sab, qb, dl = setup(tmp_path, TORRENTS)
    sab.slots["done1"]["storage"] = "/downloads/half1"
    out = pipeline.delete_downloads_of(cfg, sab, Qbit([]), {"b" * 40})
    assert (dl / "half1").exists() and "shared" in out["kept"][0]


def test_without_qbittorrent_nothing_is_deleted(tmp_path):
    import pytest
    cfg, sab, qb, dl = setup(tmp_path, TORRENTS)
    with pytest.raises(pipeline.Abort):
        pipeline.delete_downloads_of(cfg, sab, None, {"b" * 40})
    assert (dl / "half1").exists()


# ---------------------------------------------------------------- nothing can use it

def old(path, hours=2):
    import time as _t
    t = _t.time() - hours * 3600
    os.utime(path, (t, t))


def sweep(cfg, sab, qb, keep=(), live=(), names=()):
    return pipeline.clean_unused_downloads(cfg, sab, qb, set(keep), set(live), set(names), log=lambda *_: None)


def test_a_download_nothing_can_use_is_cleared(tmp_path):
    cfg, sab, qb, dl = setup(tmp_path, [])            # no torrent in qBittorrent, no job keeps any
    for d in ("done1", "half1", "seedhere"):
        old(dl / d)
    n, _ = sweep(cfg, sab, Qbit([]))
    assert n == 3 and not (dl / "half1").exists()


def test_a_job_that_could_still_use_it_keeps_it(tmp_path):
    cfg, sab, qb, dl = setup(tmp_path, [])
    old(dl / "half1")
    sweep(cfg, sab, Qbit([]), keep={"b" * 40})         # e.g. a failed build that can be tried again
    assert (dl / "half1").exists()


def test_a_build_running_for_it_keeps_it(tmp_path):
    cfg, sab, qb, dl = setup(tmp_path, [])
    old(dl / "half1")
    sweep(cfg, sab, Qbit([]), live={"b" * 40})
    assert (dl / "half1").exists()


def test_a_torrent_still_downloading_in_qbittorrent_keeps_it(tmp_path):
    cfg, sab, qb, dl = setup(tmp_path, [])
    old(dl / "half1")
    sweep(cfg, sab, Qbit([{"hash": "b" * 40, "progress": 0.5, "content_path": None}]))
    assert (dl / "half1").exists()


def test_a_recent_download_is_left_alone(tmp_path):
    cfg, sab, qb, dl = setup(tmp_path, [])
    sweep(cfg, sab, Qbit([]))                          # everything was just written
    assert (dl / "half1").exists() and sab.deleted == []


def test_an_old_record_of_a_release_still_wanted_is_kept(tmp_path):
    """Before each build filed its own downloads, a record may name another build's
    torrent - so an old one of the same release as a build still wanted stays."""
    import json
    cfg, sab, qb, dl = setup(tmp_path, [])
    led = pipeline.ledger_for(cfg)
    data = json.load(open(led.path))
    for e in data["jobs"]:
        e.pop("own", None)                             # an old record
        if e["nzo"] == "half1":
            e["title"] = "Show (2022) S01E02 1080p WEBDL H264 GRPX"
    json.dump(data, open(led.path, "w"))
    old(dl / "half1")
    sweep(cfg, sab, Qbit([]), names={pipeline.release_key("Show 2022 S01 1080p WEB-DL-GRPY")})
    assert (dl / "half1").exists()                     # the same show and season is still wanted
    sweep(cfg, sab, Qbit([]), names={pipeline.release_key("Show 2022 S02 1080p WEB-DL-GRPY")})
    assert not (dl / "half1").exists()


def test_a_release_key_ignores_group_resolution_and_punctuation():
    k = pipeline.release_key
    assert k("Show (2022) S01E02 1080p   WEBDL H264 Atmos GRPX") == k("Show 2022 S01 1080p WEB-DL-GRPY")         == k("Show.(2022).S01E01.(1080p.WEB-DL.English.-.GRPZ).mkv-xpost") == "show|s1"
    assert k("Film 2020 BluRay 1080p REMUX-GRP") == k("Film.2020.BluRay.1080p.REMUX-GRP.2") == "film|2020"
    assert k("Show S01 1080p") != k("Show S02 1080p")


def test_a_season_grabs_download_is_never_judged_here(tmp_path):
    cfg, sab, qb, dl = setup(tmp_path, [])
    pipeline.ledger_for(cfg).record("season1", "season1", torrent="")
    (dl / "season1").mkdir()
    sab.slots["season1"] = {"storage": "/downloads/season1"}
    old(dl / "season1")
    sweep(cfg, sab, Qbit([]))
    assert (dl / "season1").exists()


def test_without_qbittorrent_the_sweep_does_nothing(tmp_path):
    cfg, sab, qb, dl = setup(tmp_path, [])
    old(dl / "half1")
    assert sweep(cfg, sab, None) == (0, 0) and (dl / "half1").exists()
