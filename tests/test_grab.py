"""NZBs come from SABnzbd, never from nzb2seed talking to an indexer: SABnzbd is handed
Prowlarr's link as a paused job, nzb2seed reads the NZB SABnzbd saved, and the job is
either resumed (the post is wanted) or deleted (nothing downloaded)."""
import gzip
import os
import time

import pytest

from nzb2seed import grab
from nzb2seed.clients import ApiError, PP_REPAIR, Release
from nzb2seed.config import Config

NZB = b'<?xml version="1.0"?><nzb><file subject="&quot;a.mkv&quot;"><segments><segment number="1">id1@x</segment></segments></file></nzb>'


def rel(guid="g1"):
    return Release("Show.S01E01.1080p.WEB.h264-GRPA", "usenet", "IndexerA", 1, 10, guid,
                   "http://prowlarr.local:9696/1/download?link=abc", "", "", 0, None, None)


class Sab:
    """SABnzbd as far as grab.Source uses it; the incomplete folder is a real temp dir."""

    def __init__(self, root, fail=None):
        self.root, self.fail, self.jobs, self.log = root, fail, {}, []

    def add_url(self, url, name, cat, pp, priority=-2):
        assert priority == -2 and pp == PP_REPAIR       # paused, never +Delete
        nzo = f"nzo{len(self.jobs) + 1}"
        self.jobs[nzo] = {"filename": name, "status": "Paused"}
        self.log.append(("add_url", url, name))
        if not self.fail:
            d = os.path.join(self.root, name, "__ADMIN__")
            os.makedirs(d)
            with gzip.open(os.path.join(d, "posted.nzb.gz"), "wb") as fh:
                fh.write(NZB)
        return nzo

    def add_nzb(self, nzb, name, cat, pp, priority=0):
        assert nzb == NZB and pp == PP_REPAIR       # the bytes SABnzbd already fetched
        nzo = f"dl{len(self.jobs) + 1}"
        self.jobs[nzo] = {"filename": name, "status": "Queued"}
        self.log.append(("add_nzb", name, priority))
        return nzo

    def queue_slot(self, nzo):
        j = self.jobs.get(nzo)
        return None if j is None or self.fail else dict(j, nzo_id=nzo)

    def history_slot(self, nzo):
        return {"status": "Failed", "fail_message": self.fail}

    def incomplete_dir(self):
        return "/incomplete"

    def ensure_pp(self, nzo, pp):
        self.log.append(("pp", nzo, pp))

    def queue_do(self, action, nzo, value2=None):
        self.log.append((action, nzo, value2))
        if action == "delete":
            self.jobs.pop(nzo, None)


class NoIndexer:
    def fetch(self, r):
        raise AssertionError("nzb2seed fetched an NZB itself")


def source(tmp_path, **kw):
    cfg = Config(path=str(tmp_path / "c.toml"), sab_to_local=[["/incomplete", str(tmp_path)]])
    return grab.Source(cfg, NoIndexer(), Sab(str(tmp_path), **kw))


def test_the_nzb_is_read_from_the_paused_job_sabnzbd_fetched(tmp_path):
    src = source(tmp_path)
    assert src.fetch(rel()) == NZB
    kind, url, name = src.sab.log[0]
    assert (kind, url) == ("add_url", rel().download_url) and name.startswith(grab.CHECK_PREFIX)
    assert src.sab.jobs == {}                       # read, then let go: nothing is parked
    assert src.fetch(rel()) == NZB                  # kept here, so the indexer is not asked again
    assert [a[0] for a in src.sab.log] == ["add_url", "delete"]


def test_a_wanted_post_is_downloaded_from_the_nzb_already_in_hand(tmp_path):
    src = source(tmp_path)
    src.fetch(rel())
    nzo = src.start(rel(), PP_REPAIR)
    assert [a[0] for a in src.sab.log] == ["add_url", "delete", "add_nzb"]
    assert src.sab.jobs[nzo]["filename"] == rel().title             # named after the release
    src.close()
    assert nzo in src.sab.jobs                                      # a started job stays


def test_nothing_is_left_behind_by_posts_that_were_only_looked_at(tmp_path):
    src = source(tmp_path)
    src.fetch(rel("a"))
    src.fetch(rel("b"))
    src.drop(rel("a"))
    src.close()
    assert src.sab.jobs == {} and not grab._held


def test_sabnzbd_failing_to_get_the_nzb_is_reported(tmp_path):
    src = source(tmp_path, fail="URL Fetching failed; 403 grab limit")
    with pytest.raises(ApiError, match="grab limit"):
        src.fetch(rel())


def test_a_big_nzb_being_read_is_not_a_job_that_disappeared(tmp_path, monkeypatch):
    """SABnzbd takes the job out of its queue while it reads the NZB it fetched - seconds,
    for a season's hundreds of files - and it is in no history meanwhile."""
    monkeypatch.setattr(grab.time, "sleep", lambda s: None)
    src = source(tmp_path)
    real, polls = src.sab.queue_slot, []

    def reading(nzo):
        polls.append(nzo)
        return None if len(polls) <= 5 else real(nzo)
    src.sab.queue_slot = reading
    src.sab.history_slot = lambda nzo: None
    assert src.fetch(rel()) == NZB and src.sab.jobs == {}      # waited for it, read, let go


def test_a_job_gone_for_good_is_still_reported(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(grab.time, "time", lambda: clock[0])
    monkeypatch.setattr(grab.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    src = source(tmp_path)
    src.sab.queue_slot = lambda nzo: None
    src.sab.history_slot = lambda nzo: None
    with pytest.raises(ApiError, match="disappeared from the queue"):
        src.fetch(rel())
    assert clock[0] - 1000 >= grab.READING_GRACE


# ------------------------------------------------- builds running side by side

def queue_call(sab):
    """what remove_stray_checks reads: SABnzbd's whole queue"""
    def _call(mode, **kw):
        return {"queue": {"slots": [dict(j, nzo_id=n, percentage="0", status="Paused")
                                    for n, j in sab.jobs.items()]}}
    return _call


def test_the_sweep_leaves_a_job_that_is_still_being_fetched(tmp_path):
    """A job only exists while its NZB is being fetched - but during that moment another
    build must not sweep it up, or this one sees the indexer fail for no reason."""
    src = source(tmp_path)
    src.sab.jobs["nzo-in-flight"] = {"filename": grab.CHECK_PREFIX + "abcd1234", "status": "Paused"}
    src.sab._call = queue_call(src.sab)
    grab._hold_on("nzo-in-flight")
    try:
        assert grab.remove_stray_checks(src.sab) == 0        # in flight: left alone
    finally:
        grab._let_go("nzo-in-flight")
    assert grab.remove_stray_checks(src.sab) == 1            # let go: a leftover, so it goes


def test_nothing_is_held_once_the_nzb_has_been_read(tmp_path):
    src = source(tmp_path)
    src.fetch(rel())
    assert not grab._held and not src.held                   # the window has closed


def test_a_started_download_is_not_a_check_job(tmp_path):
    src = source(tmp_path)
    src.fetch(rel())
    src.start(rel(), PP_REPAIR)
    assert not grab._held


def test_a_failed_fetch_links_the_releases_page():
    """When SABnzbd cannot get an NZB, the message links its page on the indexer - so the
    person can look for themselves (nzb2seed never opens it)."""
    from types import SimpleNamespace
    from nzb2seed.grab import page_of
    rel = SimpleNamespace(info_url="https://indexer.example/details/abc123")
    assert page_of(rel) == " - its page: https://indexer.example/details/abc123"


def test_the_link_never_carries_a_key():
    from types import SimpleNamespace
    from nzb2seed.grab import page_of
    rel = SimpleNamespace(info_url="https://indexer.example/details?id=abc&apikey=SECRET&r=ALSO")
    link = page_of(rel)
    assert "SECRET" not in link and "ALSO" not in link and "id=abc" in link


def test_no_page_means_no_link():
    from types import SimpleNamespace
    from nzb2seed.grab import page_of
    assert page_of(SimpleNamespace(info_url="")) == ""
    assert page_of(SimpleNamespace(info_url="javascript:alert(1)")) == ""


def test_a_retry_uses_the_nzb_already_fetched(tmp_path):
    """Indexers often refuse the same NZB twice in 24 hours: a later run - a retry, or
    another build wanting the same post - reads the one kept on disk instead."""
    first = source(tmp_path)
    assert first.fetch(rel()) == NZB
    again = source(tmp_path)                        # a new run: nothing in memory
    assert again.fetch(rel()) == NZB
    assert again.sab.log == []                      # SABnzbd, and so the indexer, never asked


def test_a_failed_fetch_keeps_nothing(tmp_path):
    import pytest
    src = source(tmp_path, fail="Already downloaded")
    with pytest.raises(grab.ApiError):
        src.fetch(rel())
    assert src.store.get(rel()) is None             # so the next run does try again


def test_nzbs_are_kept_for_good(tmp_path):
    """However old, a kept NZB is still used rather than asking the indexer again."""
    src = source(tmp_path)
    src.fetch(rel())
    f = src.store._file(rel())
    os.utime(f, (0, 0))                             # as old as it gets
    again = source(tmp_path)
    assert again.fetch(rel()) == NZB and again.sab.log == []


class RefusingSab(Sab):
    """SABnzbd whose URL fetch the indexer refused: it waits to retry, and says so only
    as a WAIT label on the job."""

    def add_url(self, url, name, cat, pp, priority=-2):
        nzo = f"nzo{len(self.jobs) + 1}"
        self.jobs[nzo] = {"filename": name, "status": "Grabbing", "labels": ["WAIT 57 sec"]}
        self.log.append(("add_url", url, name))
        return nzo


def test_a_refused_nzb_is_reported_at_once_not_after_the_timeout(tmp_path):
    import pytest
    cfg = Config(path=str(tmp_path / "c.toml"), sab_to_local=[["/incomplete", str(tmp_path)]])
    src = grab.Source(cfg, NoIndexer(), RefusingSab(str(tmp_path)))
    start = time.time()
    with pytest.raises(grab.ApiError, match="refused the NZB"):
        src.fetch(rel())
    assert time.time() - start < 5                                 # not the 180 s timeout
    assert src.sab.jobs == {}                                      # its retry is called off
    assert src.store.get(rel()) is None


def test_only_a_wait_label_counts_as_refused():
    assert grab.told_to_wait({"labels": ["WAIT 57 sec"]})
    assert grab.told_to_wait({"labels": ["WACHTEN 30 sec"]})       # SABnzbd in another language
    assert not grab.told_to_wait({"labels": []})
    assert not grab.told_to_wait({"labels": ["DUPLICATE"]})
    assert not grab.told_to_wait({"labels": ["PROPAGATING 5 min"]})


def test_a_kept_nzb_gives_its_password_without_asking_the_indexer(tmp_path, monkeypatch):
    from nzb2seed import archives
    monkeypatch.setattr(archives, "_passwords", [])
    src = source(tmp_path)
    locked = NZB.replace(b"<nzb>", b'<nzb><head><meta type="password">s3cret</meta></head>')
    src.store.put(rel(), locked)
    assert src.kept(rel()) == locked and archives._passwords == ["s3cret"]
    assert src.sab.log == []                             # nothing fetched
    assert src.kept(rel("other")) is None
