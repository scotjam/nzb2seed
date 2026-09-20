import os

import pytest

from nzb2seed import bencode, matching
from nzb2seed.clients import Release
from nzb2seed.pathmap import map_path
from nzb2seed.torrent import TorrentError, parse

from helpers import NAME, make_torrent, scene_layout


def rel(title, proto="usenet", grabs=0):
    return Release(title, proto, "idx", 1, 1, title, "", "", "", grabs, None, None)


def test_bencode_roundtrip_and_infohash():
    raw = make_torrent(NAME, scene_layout())
    t = parse(raw)
    assert bencode.decode(raw)[b"info"][b"name"] == NAME.encode()
    assert len(t.infohash) == 40 and t.private
    assert t.files[0].parts[0] == NAME


def test_unsafe_paths_rejected():
    raw = make_torrent(NAME, {"../evil": b"x"})
    with pytest.raises(TorrentError):
        parse(raw)


@pytest.mark.parametrize("path,pairs,want", [
    ("/downloads/complete/x", [["/downloads", "\\\\nas\\dl"]], "\\\\nas\\dl\\complete\\x"),
    ("\\\\NAS\\DL\\complete\\x", [["\\\\nas\\dl", "/data"]], "/data/complete/x"),
    ("/downloads/complete/x", [["/downloads", "/a"], ["/downloads/complete", "/b"]], "/b/x"),
    ("/other/x", [["/downloads", "/a"]], "/other/x"),
    ("/downloadsX/x", [["/downloads", "/a"]], "/downloadsX/x"),
    ("D:\\sab\\done", [["D:\\sab", "/mnt/sab"]], "/mnt/sab/done"),
])
def test_map_path(path, pairs, want):
    assert map_path(path, pairs) == want


def test_exact_and_episode_matching():
    tor = rel("Show.S01.1080p.BluRay.x264-GRP", "torrent")
    nzbs = [rel("Show S01E01 1080p BluRay x264-GRP", grabs=3),
            rel("Show.S01E01.1080p.BluRay.x264-GRP", grabs=9),
            rel("Show.S01E02.1080p.BluRay.x264-GRP"),
            rel("Show.S01E03.720p.BluRay.x264-GRP"),
            rel("Show.S01E04.1080p.BluRay.x264-OTHER")]
    assert matching.exact_matches(tor, nzbs) == []
    eps = matching.episode_matches(tor, nzbs)
    assert [e.title for e in eps] == ["Show.S01E01.1080p.BluRay.x264-GRP",
                                      "Show.S01E02.1080p.BluRay.x264-GRP"]
    assert matching.exact_matches(rel("show s01e02 1080p bluray x264-grp", "torrent"), nzbs)
    assert matching.episode_query(tor.title) == "show 1080p bluray x264 grp"


def test_gui_client_rules():
    from nzb2seed.gui import _is_local_client
    assert _is_local_client("192.168.1.50") and _is_local_client("10.0.0.3")
    assert _is_local_client("127.0.0.1") and _is_local_client("::ffff:192.168.1.7")
    assert not _is_local_client("8.8.8.8") and not _is_local_client("81.2.69.160")


def test_network_paths_from_a_pc_are_found_on_the_nas(tmp_path):
    import pytest
    from nzb2seed import gui
    from nzb2seed.config import Config
    disk = tmp_path / "disk2"
    (disk / "kids" / "Show & Co" / "Show.S01.1080p-GRPA").mkdir(parents=True)
    cfg = Config(path=str(tmp_path / "c.toml"), local_to_qbit=[[str(disk), "/data2"]])
    want = os.path.join(os.path.realpath(disk), "kids", "Show & Co", "Show.S01.1080p-GRPA")
    assert gui.local_folder(cfg, r"\\192.0.2.10\kids\Show & Co\Show.S01.1080p-GRPA") == want
    assert gui.local_folder(cfg, "smb://nas.local/KIDS/Show & Co/Show.S01.1080p-GRPA/") == want
    assert gui.local_folder(cfg, '"//nas.local/kids/Show & Co/Show.S01.1080p-GRPA"') == want
    with pytest.raises(ValueError, match="no shared folder"):
        gui.local_folder(cfg, r"\\nas\movies\x")
    with pytest.raises(ValueError, match="not a full path"):
        gui.local_folder(cfg, r"kids\Show & Co")


def test_the_config_file_is_written_owner_only(tmp_path):
    import stat
    from nzb2seed import config as config_mod
    cfg = config_mod.Config(path=str(tmp_path / "nzb2seed.toml"), sab_key="secret")
    config_mod.save(cfg)
    mode = stat.S_IMODE(os.stat(cfg.path).st_mode)
    assert not mode & 0o077 or os.name == "nt"      # no group/other access (Windows has no modes)
