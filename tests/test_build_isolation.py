"""Builds run side by side: each one's Usenet downloads are recorded under its own torrent.

They were not, once: the torrent being built was one variable shared by every build, so
with five seasons building at once their downloads were filed under whichever season had
started last - and neither "Remove and delete downloads" nor the automatic clean-up could
find them again.
"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from nzb2seed import pipeline  # noqa: E402
from nzb2seed.clients import Release  # noqa: E402
from nzb2seed.config import Config  # noqa: E402

NZB = b'<?xml version="1.0"?><nzb><file subject="a"><segments><segment number="1">id@x</segment></segments></file></nzb>'


class Sab:
    def __init__(self):
        self.n, self.lock = 0, threading.Lock()

    def add_nzb(self, nzb, name, cat, pp, prio):
        with self.lock:
            self.n += 1
            return f"nzo{self.n}"

    def ensure_pp(self, nzo, pp):
        return "+Repair"


def test_each_build_records_its_downloads_under_its_own_torrent(tmp_path, monkeypatch):
    cfg = Config(path=str(tmp_path / "c.toml"))
    monkeypatch.setattr(pipeline.space, "wait_for_room", lambda *a, **k: None)
    sab = Sab()
    both_started = threading.Barrier(2)

    def build(h):
        pipeline._this_build.torrent = h                 # what execute_run does
        both_started.wait(5)                             # the other build starts meanwhile
        rel = Release(f"Show.S0{h[0]}.1080p-GRP", "usenet", "idx", 1, 10, "g" + h, "", "", "", 0, None, 1)
        pipeline.sab_submit(cfg, None, sab, rel, NZB, 1)
    threads = [threading.Thread(target=build, args=(c * 40,)) for c in "12"]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    got = {e["title"]: e["torrent"] for e in pipeline.ledger_for(cfg).all()}
    assert got == {"Show.S01.1080p-GRP": "1" * 40, "Show.S02.1080p-GRP": "2" * 40}
