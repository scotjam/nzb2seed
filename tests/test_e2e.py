"""Full `run` pipeline against fake Prowlarr / SABnzbd / qBittorrent.

The fake qBittorrent computes recheck progress by really hashing the files at
its save path, so a 100% result means the layout on disk is byte-exact.
"""
import os

from nzb2seed import cli, pipeline
from nzb2seed.clients import PP_REPAIR, Release
from nzb2seed.config import Config
from nzb2seed.torrent import PieceVerifier, parse

from helpers import NAME, make_torrent, nzb_job, scene_layout

TORRENT = make_torrent(NAME, scene_layout())


class FakeProwlarr:
    def __init__(self, *a):
        pass

    def search(self, query, *a):
        mk = lambda proto, idx: Release(NAME, proto, idx, 1, 10**6, f"{proto}-{idx}",  # noqa: E731
                                        f"http://x/{proto}", "", "2026-09-01T00:00:00Z", 5, 9, 10)
        return [mk("torrent", "TrackerA"), mk("usenet", "IndexerB")]

    def fetch(self, r):
        return TORRENT if r.protocol == "torrent" else b"<?xml version='1.0'?><nzb/>"


class FakeSAB:
    root = None
    calls = []

    def __init__(self, *a):
        pass

    def version(self):
        return "4.5.0"

    def config(self):
        return {"misc": {}}

    def add_nzb(self, nzb, name, cat, pp, prio):
        FakeSAB.calls.append(("add", name, cat, pp))
        nzb_job(os.path.join(FakeSAB.root, name + ".1"))  # "downloads" the post
        return "SABnzbd_nzo_1"

    def ensure_pp(self, nzo, pp):
        FakeSAB.calls.append(("ensure_pp", nzo, pp))
        return "+Repair"

    def status(self, nzo):
        return "Completed", {"storage": "/sab/complete/" + NAME + ".1"}

    def delete_history(self, nzo):
        pass


class FakeQB:
    inst = None

    def __init__(self, *a):
        FakeQB.inst = self
        self.t = None
        self.save_path = None
        self.state = "stoppedDL"
        self.progress = 0.0
        self.calls = []
        self.started = False

    def login(self): pass
    def app_version(self): return "v5.0.0"
    def api_version(self): return (2, 11)
    def preferences(self): return {}

    def info(self, h):
        if self.t is None or h != self.t.infohash:
            return None
        return {"state": self.state, "progress": self.progress, "save_path": self.save_path}

    def add_stopped(self, data, fn, save_path, cat, tags):
        self.calls.append("add")
        self.category = cat
        self.t = parse(data)
        self.save_path = "/qbit/" + os.path.basename(save_path)  # deliberately wrong at first

    def set_location(self, h, loc):
        self.calls.append(("setLocation", loc))
        self.save_path = loc

    def recheck(self, h):
        self.calls.append("recheck")
        local = self.save_path.replace("/qbit-root", FakeQB.local_root)
        with PieceVerifier(self.t, lambda f: os.path.join(local, *f.parts)) as pv:
            good = sum(pv.check(i) for i in range(len(self.t.pieces)))
        self.progress = good / len(self.t.pieces)

    def wait_idle(self, h, **kw):
        return self.info(h)

    def files(self, h):
        return []

    def piece_states(self, h):
        return [2 if self.progress == 1 else 0] * len(self.t.pieces)

    def stop(self, h): pass

    def start(self, h):
        self.started = True


class Args:
    query = NAME
    torrent = None
    indexer = None
    yes = True
    pp = None
    output_dir = None
    no_cleanup = False
    local_verify = True
    no_qbit = False
    start = True
    dry_run = False


def test_run_end_to_end(tmp_path, monkeypatch):
    sab_root = tmp_path / "nas" / "complete"
    sab_root.mkdir(parents=True)
    FakeSAB.root = str(sab_root)
    FakeSAB.calls = []
    FakeQB.local_root = str(sab_root)
    monkeypatch.setattr(cli, "Prowlarr", FakeProwlarr)
    monkeypatch.setattr(pipeline, "Prowlarr", FakeProwlarr)
    monkeypatch.setattr(pipeline, "SABnzbd", FakeSAB)
    monkeypatch.setattr(pipeline, "QBittorrent", FakeQB)

    cfg = Config(path=tmp_path / "c.toml", sab_category="nzb2seed",
                 sab_to_local=[["/sab/complete", str(sab_root)]],
                 local_to_qbit=[[str(sab_root), "/qbit-root"]],
                 torrent_dir=str(tmp_path / "torrents"))
    assert cli.cmd_run(cfg, Args()) == 0

    assert ("add", NAME, "nzb2seed", PP_REPAIR) in FakeSAB.calls
    qb = FakeQB.inst
    assert qb.calls[0] == "add" and ("setLocation", "/qbit-root") in qb.calls and "recheck" in qb.calls
    assert qb.category == "nzb2seed"      # its own category, so builds stand out in qBittorrent
    assert qb.progress == 1.0 and qb.started
    tree = {os.path.relpath(os.path.join(d, n), sab_root).replace("\\", "/")
            for d, _, ns in os.walk(sab_root) for n in ns}
    assert tree == {f"{NAME}/{r}" for r in scene_layout()}
    assert os.path.exists(tmp_path / "torrents" / f"{NAME}.torrent")
