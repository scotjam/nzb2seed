"""NZBs come from SABnzbd, never from nzb2seed talking to an indexer: SABnzbd is handed
Prowlarr's link as a paused job, nzb2seed reads the NZB SABnzbd saved, and the job is
either resumed (the post is wanted) or deleted (nothing downloaded)."""
import gzip
import os

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
    assert src.sab.log[0] == ("add_url", rel().download_url, src.sab.jobs["nzo1"]["filename"])
    assert src.sab.jobs["nzo1"]["filename"].startswith(grab.CHECK_PREFIX)
    assert src.fetch(rel()) == NZB and len(src.sab.jobs) == 1      # asked once


def test_a_wanted_post_is_resumed_not_grabbed_again(tmp_path):
    src = source(tmp_path)
    src.fetch(rel())
    assert src.start(rel(), PP_REPAIR) == "nzo1"
    actions = [a[0] for a in src.sab.log]
    assert actions == ["add_url", "rename", "pp", "priority", "resume"]
    src.close()
    assert "nzo1" in src.sab.jobs                                   # a started job stays


def test_unused_posts_are_deleted_when_the_build_ends(tmp_path):
    src = source(tmp_path)
    src.fetch(rel("a"))
    src.fetch(rel("b"))
    src.drop(rel("a"))
    src.close()
    assert src.sab.jobs == {}


def test_sabnzbd_failing_to_get_the_nzb_is_reported(tmp_path):
    src = source(tmp_path, fail="URL Fetching failed; 403 grab limit")
    with pytest.raises(ApiError, match="grab limit"):
        src.fetch(rel())
