"""The Library tab: seasons found on disk, the tracker's packs judged against them, and
the swap that puts a torrent's files in the library's place."""
import os
import time
from types import SimpleNamespace

import pytest

from nzb2seed import assemble as asm
from nzb2seed import library as lib
from nzb2seed import torrent as torrent_mod
from nzb2seed.clients import Release

from helpers import make_torrent, rnd


def rel(title, size=10 * 1024 ** 3, seeders=5, protocol="torrent"):
    return Release(title=title, protocol=protocol, indexer="TL", indexer_id=1, size=size, guid=title,
                   download_url="", info_url="", publish_date="", grabs=None, seeders=seeders, files=None)


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


# ---------------------------------------------------------------- which titles are packs

SHOWS = {"all.show.foxtrot.and.golf.2020", "all.show.foxtrot.and.golf"}


@pytest.mark.parametrize("title,season", [
    ("all show foxtrot and golf 2020 S04 1080p BluRay x264-VMA", 4),
    ("all.show.foxtrot.and.golf.2020.S04.1080p.BluRay.x264-WMh", 4),
    ("All Show Foxtrot & Golf S04 COMPLETE 720p", 4),
    ("all show foxtrot and golf 2020 S04E01 1080p", None),        # an episode
    ("all show foxtrot and golf 2020 S01-S04 1080p", None),        # several seasons
    ("all show foxtrot and golf 2020 S03 1080p", 3),
    ("all show foxtrot and golf 1978 S04 DVDRip", None),           # the other show
    ("all show foxtrot and golf Christmas Special 2020", None),
])
def test_pack_season(title, season):
    assert lib.pack_season(title, SHOWS) == season


def test_a_year_the_library_never_names_is_another_show():
    shows = {"no.show.hotel"}
    assert lib.pack_season("No, Show Hotel S01 1080p WEB-DL AAC 2.0 H.264-CBFM", shows) == 1
    assert lib.pack_season("No Show Hotel 2013 S01 DVDRip XviD-iNGOT", shows) is None


# ---------------------------------------------------------------- the library on disk

@pytest.fixture()
def library(tmp_path):
    root = tmp_path / "tv"
    show = root / "Alpha (2009)" / "Season 4"
    eps = {}
    for n in (1, 2, 3):
        name = f"Alpha.2009.S04E0{n}.Title.1080p.BluRay.x265-pCf.mkv"
        eps[name] = rnd(40_000 + n, n)
        write(str(show / name), eps[name])
    write(str(show / "Alpha.2009.S04E01.Title.1080p.BluRay.x265-pCf.srt"), b"subs")
    write(str(root / "Loose Show" / "Loose.Show.S02E01.720p.WEB.h264-GRP.mkv"), rnd(30_000, 9))
    (root / ".nzb2seed-replaced").mkdir()
    return SimpleNamespace(root=str(root), season_dir=str(show), eps=eps)


def test_scan_finds_season_folders_and_loose_episodes(library):
    seasons = lib.scan([library.root])
    assert [(s.show, s.season, len(s.files)) for s in seasons] == [("Alpha (2009)", 4, 3), ("Loose Show", 2, 1)]
    alpha = seasons[0]
    assert alpha.release() == ("pcf", "1080p")
    assert alpha.query() == "alpha 2009 S04"
    assert "alpha.2009" in alpha.names()


def test_a_season_is_seeded_when_every_episode_is_in_a_torrent(library, tmp_path):
    s = lib.scan([library.root])[0]
    assert not lib.seeded(s, set())
    for f in s.files:
        os.link(f.path, f.path + ".linked")
    assert lib.seeded(lib.scan([library.root])[0], set())
    # or the torrent seeds straight from the library folder
    s2 = lib.scan([library.root])[1]
    assert lib.seeded(s2, {os.path.normcase(os.path.abspath(s2.dir))})


def test_candidates_put_the_same_release_first_and_skip_what_is_not_a_pack(library):
    s = lib.scan([library.root])[0]
    picks = lib.candidates(s, [
        rel("Alpha 2009 S04 1080p Blu-ray DD 5.1 x264-JaysOK", size=8 * 1024 ** 3, seeders=50),
        rel("Alpha (2009) S04 1080p BluRay 10bit EAC3 5 1 x265-pCf", size=5 * 1024 ** 3, seeders=10),
        rel("Alpha (2009) S04E01 1080p x265-pCf"),
        rel("Alpha 2009 S04 1080p BluRay REMUX AVC-GRP"),
        rel("Alpha 2009 S04 720p BluRay x264-DEMAND"),
        rel("Alpha (2009) S04 1080p x265-pCf", protocol="usenet"),
    ])
    assert [p.release.title for p in picks] == [
        "Alpha (2009) S04 1080p BluRay 10bit EAC3 5 1 x265-pCf",
        "Alpha 2009 S04 1080p Blu-ray DD 5.1 x264-JaysOK",
        "Alpha 2009 S04 720p BluRay x264-DEMAND"]
    assert picks[0].same and not picks[1].same


def _pack(library, extra=None, drop=None, change=None):
    files = {}
    for name, data in library.eps.items():
        if drop and drop in name:
            continue
        files[name] = change if change and "E02" in name else data
    files.update(extra or {})
    data = make_torrent("Alpha.2009.S04.1080p.BluRay.x265-pCf", files)
    return torrent_mod.parse(data), files


def test_identical_and_covers(library):
    s = lib.scan([library.root])[0]
    t, _ = _pack(library, extra={"Alpha.2009.S04.1080p.BluRay.x265-pCf.nfo": b"nfo"})
    assert lib.identical(t, s) and lib.covers(t, s) == []
    other, _ = _pack(library, change=rnd(41_000, 77))
    assert not lib.identical(other, s)
    short, _ = _pack(library, drop="E03")
    assert lib.covers(short, s) == ["s04e03"]


# ---------------------------------------------------------------- swapping

def _placed(tmp_path, files, name="Alpha.2009.S04.1080p.BluRay.x264-OTHER"):
    out = tmp_path / "torrents"
    for rel_, data in files.items():
        write(str(out / name / rel_), data)
    t = torrent_mod.parse(make_torrent(name, files))
    return t, str(out / name)


def test_swap_sets_the_old_files_aside_and_links_the_torrents(library, tmp_path):
    s = lib.scan([library.root])[0]
    new = {f"Alpha.2009.S04E0{n}.1080p.BluRay.x264-OTHER.mkv": rnd(50_000 + n, 100 + n) for n in (1, 2, 3)}
    t, root = _placed(tmp_path, new)
    cfg = SimpleNamespace(library_roots=[library.root], library_keep_replaced_days=14)
    plan = lib.swap_plan(t, root, s)
    assert len(plan) == 3
    assert any(p.endswith(".srt") for p in plan[0][0])         # a sidecar goes with its episode
    n, moved = lib.do_swap(cfg, t, root, s)
    assert (n, moved) == (3, 4)
    now = sorted(os.listdir(library.season_dir))
    assert now == sorted(new)
    for name in new:
        assert os.path.samefile(os.path.join(library.season_dir, name), os.path.join(root, name))
    aside = os.path.join(os.path.dirname(library.root), lib.REPLACED_DIR)
    kept = [f for _, _, fs in os.walk(aside) for f in fs]
    assert len(kept) == 4
    # done: nothing more to swap
    assert lib.swap_plan(t, root, lib.scan([library.root])[0]) == []


def test_episodes_the_torrent_lacks_stay(library, tmp_path):
    s = lib.scan([library.root])[0]
    t, root = _placed(tmp_path, {"Alpha.2009.S04E01.1080p.BluRay.x264-OTHER.mkv": rnd(50_000, 5)})
    lib.do_swap(SimpleNamespace(library_roots=[library.root]), t, root, s)
    left = sorted(os.listdir(library.season_dir))
    assert "Alpha.2009.S04E02.Title.1080p.BluRay.x265-pCf.mkv" in left
    assert "Alpha.2009.S04E01.Title.1080p.BluRay.x265-pCf.mkv" not in left


def test_a_season_linked_from_the_library_needs_no_swap(library, tmp_path):
    s = lib.scan([library.root])[0]
    out = tmp_path / "torrents" / "Alpha.2009.S04.1080p.BluRay.x265-pCf"
    out.mkdir(parents=True)
    for f in s.files:
        os.link(f.path, str(out / os.path.basename(f.path)))
    t, _ = _pack(library)
    assert lib.swap_plan(t, str(out), s) == []


def test_set_aside_files_are_deleted_after_the_kept_days(tmp_path):
    root = tmp_path / "tv"
    root.mkdir()
    old = tmp_path / lib.REPLACED_DIR / "20200101" / "tv" / "Show" / "a.mkv"
    write(str(old), b"x")
    fresh = tmp_path / lib.REPLACED_DIR / time.strftime("%Y%m%d") / "b.mkv"
    write(str(fresh), b"x")
    cfg = SimpleNamespace(library_roots=[str(root)], library_keep_replaced_days=14)
    assert lib.purge_replaced(cfg) == 1
    assert not old.exists() and fresh.exists()
    assert lib.purge_replaced(SimpleNamespace(library_roots=[str(root)], library_keep_replaced_days=0)) == 0


# ---------------------------------------------------------------- the library's files are never written to

def test_a_repair_never_writes_into_a_library_file(tmp_path):
    mine = write(str(tmp_path / "library" / "ep.mkv"), b"A" * 1000)
    placed = str(tmp_path / "torrent" / "ep.mkv")
    os.makedirs(os.path.dirname(placed))
    os.link(mine, placed)
    assert asm.own_copy(placed) is True
    with open(placed, "r+b") as fh:
        fh.write(b"B")
    with open(mine, "rb") as fh:
        assert fh.read(1) == b"A"                           # the library's copy is untouched
    assert asm.own_copy(placed) is False                    # already its own


def test_budget_counts_the_last_day_only(tmp_path):
    st = lib.State(str(tmp_path / "library.json"))
    st.spent = [[time.time() - 90_000, 50], [time.time() - 100, 7]]
    assert st.spent_today() == 7
    st.spend(3)
    assert lib.State(str(tmp_path / "library.json")).spent_today() == 10


def test_a_season_cut_off_by_a_restart_is_tried_again(tmp_path):
    p = str(tmp_path / "library.json")
    st = lib.State(p)
    st.update("k", status=lib.BUILDING)
    assert lib.State(p).seasons["k"]["status"] == lib.WAITING


def test_a_pack_with_a_year_the_library_lacks_is_only_offered_as_the_same_release(tmp_path):
    root = tmp_path / "tv"
    write(str(root / "A Show in Echo" / "Season 1" / "A.Show.in.Echo.S01E01.1080p.WEBRip.x265-pCf.mkv"),
          rnd(1000, 1))
    s = lib.scan([str(root)])[0]
    picks = lib.candidates(s, [rel("A Show in Echo (2024) S01 WEBRip EAC3 5 1 1080p x265-pCf"),
                               rel("A Show in Echo (2024) S01 1080p x265-LSpAL"),
                               rel("A Show in Echo S01 1080p WEBRip x265-DH")])
    assert [(p.release.title, p.unsure) for p in picks] == [
        ("A Show in Echo (2024) S01 WEBRip EAC3 5 1 1080p x265-pCf", True),
        ("A Show in Echo S01 1080p WEBRip x265-DH", False)]


# ---------------------------------------------------------------- a season that ends short

def _worker(tmp_path, library, monkeypatch, found_title):
    """A Library worker without its thread, against a fake Prowlarr that has one pack."""
    from nzb2seed import pipeline
    s = lib.scan([library.root])[0]
    t_bytes = make_torrent("Alpha.2009.S04.1080p.BluRay.x265-pCf",
                           {**library.eps, "Alpha.2009.S04.1080p.BluRay.x265-pCf.nfo": b"nfo"})

    class FakePr:
        def __init__(self, *a): pass
        def indexers(self): return [(7, "TL")]
        def search(self, q, ids, cats): return [rel(found_title, seeders=12)]
        def fetch(self, r): return t_bytes
    monkeypatch.setattr(lib, "Prowlarr", FakePr)
    w = object.__new__(lib.Library)
    w.app = SimpleNamespace(cfg=None)
    w.state = lib.State(str(tmp_path / "library.json"))
    w.seasons = {s.key: s}
    w.current = None
    cfg = SimpleNamespace(prowlarr_url="", prowlarr_key="", library_indexer="TL", library_output_dir=str(tmp_path / "out"),
                          output_dir="", torrent_dir=str(tmp_path / "torrents"), library_daily_gb=0,
                          library_roots=[library.root])
    return w, cfg, s, pipeline


def test_a_short_season_is_handed_to_the_tracker_rules_not_rebuilt(tmp_path, library, monkeypatch):
    """The library's files are the tracker's release but one small file is nowhere to be had:
    the job runner then applies the tracker's limit / the other-tracker lookup, exactly as
    for an automatic build. It is never rebuilt from Usenet in full."""
    w, cfg, s, pipeline = _worker(tmp_path, library, monkeypatch, "Alpha (2009) S04 1080p BluRay x265-pCf")
    seen = {}

    def assemble(cfg_, opts, path, dirs):
        seen["tracker"], seen["seeders"] = pipeline._this_build.tracker, pipeline._this_build.seeders
        raise pipeline.Incomplete("recheck finished at 99.99%", 0.9999, "h", path, "/x", True, tracker="TL")
    monkeypatch.setattr(pipeline, "execute_assemble", assemble)
    monkeypatch.setattr(pipeline, "execute_run", lambda *a, **k: pytest.fail("must not rebuild"))
    with pytest.raises(pipeline.Incomplete):
        w.run(cfg, s.key)
    assert seen == {"tracker": "TL", "seeders": 12}
    it = w.state.seasons[s.key]
    assert it["status"] == lib.SHORT and "other trackers" in it["why"]


def test_the_usual_torrents_folder_is_used_only_on_the_librarys_drive(tmp_path, monkeypatch):
    lib_dir, usual, chosen = tmp_path / "tv", tmp_path / "torrents", tmp_path / "torrents-library"
    for d in (lib_dir, usual, chosen):
        d.mkdir()
    cfg = SimpleNamespace(library_roots=[str(lib_dir)], library_output_dir="", output_dir=str(usual))
    assert lib.output_dir(cfg) == str(usual)                      # same drive: used
    monkeypatch.setattr(lib, "same_drive", lambda a, b: False)
    assert lib.output_dir(cfg) == ""                              # another drive: never
    cfg.library_output_dir = str(chosen)
    assert lib.output_dir(cfg) == str(chosen)                     # a chosen folder always wins


@pytest.mark.parametrize("title,seasons", [
    ("Show.Charlie-Two.S02.1080p.WEB-DL.DD5.1.H.264-UAi", {2}),
    ("Show.Charlie.Two.S03.2015.Complete.1080p.Netflix.WEB-DL.AVC.DDP.5.1-KIAC", {3}),
    ("Show Charlie-Two S01-S03 1080p BluRay x264-GRP", {1, 2, 3}),
    ("Show.Charlie-Two.2013.S04.720p.BluRay.x264-IVYKBYL", {4}),
    ("Show.Charlie-Two.S02E05.1080p.WEB-DL-UAi", set()),
    ("Show.Charlie-Two.1999.S02.1080p.WEB-DL-UAi", set()),      # a year the library does not name
    ("Another.Show.S02.1080p.WEB-DL-UAi", set()),
])
def test_held_seasons(title, seasons):
    assert lib.held_seasons(title, {"show.charlie.two", "show.charlie.two.2013"}) == seasons


def _client_worker(tmp_path, library, packs, **cfg):
    w = object.__new__(lib.Library)
    w.app = SimpleNamespace(cfg=SimpleNamespace(**{"library_indexer": "TL", "library_redownload": False, **cfg}))
    w.state = lib.State(str(tmp_path / "library.json"))
    w.client = lambda: (set(), packs)
    s = lib.scan([library.root])[0]
    return w, s


def test_a_season_already_in_the_client_is_left_alone(tmp_path, library):
    w, s = _client_worker(tmp_path, library, [("Alpha.2009.S04.720p.WEB-DL-UAi", "TrackerA")])
    status, why = w.client_status(s, w.client(), w.app.cfg)
    assert status == lib.IN_CLIENT and "TrackerA" in why
    # unless "redownload for this tracker" is ticked ...
    w.app.cfg.library_redownload = True
    assert w.client_status(s, w.client(), w.app.cfg)[0] == lib.WAITING
    # ... and never when this very tracker has it already
    w.client = lambda: (set(), [("Alpha.2009.S04.720p.WEB-DL-UAi", "TL (API)")])
    assert w.client_status(s, w.client(), w.app.cfg)[0] == lib.IN_CLIENT
    # another season of the show is no reason
    w.client = lambda: (set(), [("Alpha.2009.S05.720p.WEB-DL-UAi", "TrackerA")])
    assert w.client_status(s, w.client(), w.app.cfg)[0] == lib.WAITING


def test_the_loop_skips_a_season_in_the_client_but_downloads_it_again_when_asked(tmp_path, library):
    w, s = _client_worker(tmp_path, library, [("Alpha.2009.S04.720p.WEB-DL-UAi", "TrackerA")],
                          library_roots=[library.root], library_enabled=True, library_daily_gb=0,
                          output_dir="", torrent_dir=str(tmp_path), library_keep_replaced_days=0)
    w.app.guard = lambda: SimpleNamespace(holding=lambda: False)
    w.seasons = {s.key: s}
    w.state.scanned_at = time.time()
    w.state.update(s.key, status=lib.WAITING)
    w.current, w.wake, w.offered_at = None, SimpleNamespace(set=lambda: None), time.time()
    started = []
    w.start = started.append
    w.poll()
    assert started == [] and w.state.seasons[s.key]["status"] == lib.IN_CLIENT
    w.again(s.key)
    w.poll()
    assert started == [s.key]


@pytest.mark.parametrize("folder,name", [
    ("Show.Bravo.S06E01.1080p.WEB.x265-YLLSpAL", "Show Bravo"),
    ("Show.Delta.S01-S07.COMPLETE.1080p.WEBRip.DDP2.0.H265-k6n", "Show Delta"),
    ("Show.India.Juliet.S01.720p.WEB-DL.AAC2.0.x264-AndreMor", "Show India Juliet"),
    ("Alpha.2009.S04.1080p.BluRay.x265-pCf", "Alpha (2009)"),
    ("Alpha (2009)", "Alpha (2009)"),
    ("Show Charlie-Two", "Show Charlie-Two"),
    ("Show Bravo", "Show Bravo"),
    ("Season 1", "Season 1"),
])
def test_a_release_folder_is_named_after_its_show(folder, name):
    assert lib.show_name(folder) == name


def test_a_release_folder_in_the_library_top_scans_as_its_show(tmp_path):
    root = tmp_path / "tv"
    write(str(root / "Show.Bravo.S06E01.1080p.WEB.x265-YLLSpAL" / "Show.Bravo.S06E01.1080p.WEB.x265-YLLSpAL.mkv"),
          rnd(20_000, 3))
    (s,) = lib.scan([str(root)])
    assert (s.show, s.season) == ("Show Bravo", 6)
    assert lib.held_seasons("Show.Bravo.S06.NORDiC.1080p.ATV.WEB-DL.H.265-UVYCpUL", s.names()) == {6}


def test_a_swap_never_moves_a_file_a_torrent_seeds_from(library, tmp_path):
    s = lib.scan([library.root])[0]
    files = {n.replace("x265-pCf", "x264-OTHER"): rnd(50_000 + i, i) for i, n in enumerate(library.eps)}
    t, root = _placed(tmp_path, files)
    plan = lib.swap_plan(t, root, s)
    assert plan and lib.seeded_from(plan, set()) == []
    # a torrent in the client seeds straight from the library's season folder
    held = {os.path.normcase(os.path.abspath(s.dir))}
    assert len(lib.seeded_from(plan, held)) >= len(library.eps)



def test_a_season_left_alone_is_searched_only_when_asked_and_its_pack_is_kept(tmp_path, library, monkeypatch):
    w, cfg, s, pipeline = _worker(tmp_path, library, monkeypatch, "Alpha (2009) S04 1080p BluRay x265-pCf")
    w.app.cfg = cfg
    w.state.update(s.key, status=lib.IN_CLIENT)
    w.check(s.key)
    o = w.state.seasons[s.key]["offer"]
    assert o["title"] == "Alpha (2009) S04 1080p BluRay x265-pCf" and o["same"] and o["files"] == 4
    assert os.path.exists(lib.kept_torrent(cfg, o["infohash"]))
    # "Download again" uses the kept pack: no second search
    monkeypatch.setattr(lib.Prowlarr, "search", lambda *a: pytest.fail("searched again"))
    seen = {}
    monkeypatch.setattr(pipeline, "execute_assemble", lambda cfg_, opts, path, dirs: seen.setdefault("path", path) and {})
    w.run(cfg, s.key)
    assert seen["path"] == lib.kept_torrent(cfg, o["infohash"])


def test_a_season_over_the_budget_waits_and_is_not_started_again_and_again(tmp_path, library):
    w, s = _client_worker(tmp_path, library, [], library_roots=[library.root], library_enabled=True,
                          library_daily_gb=100, output_dir="", torrent_dir=str(tmp_path),
                          library_keep_replaced_days=0)
    w.app.guard = lambda: SimpleNamespace(holding=lambda: False)
    w.seasons = {s.key: s}
    w.state.scanned_at = time.time()
    w.current, w.wake = None, SimpleNamespace(set=lambda: None)
    w.state.update(s.key, status=lib.BUDGET, need=72 * 1024 ** 3)
    w.state.spend(40 * 1024 ** 3)                       # 60 GB left: not enough
    started = []
    w.start = started.append
    w.poll()
    assert started == []
    w.state.spent = []                                  # the day's spending has rolled off
    w.poll()
    assert started == [s.key]



class FakeCrossEyed:
    def __init__(self, show_dir, root, infohash):
        self.calls, self.jobs, self.dry, self.mode_dry = [], [], True, False
        self.a = {"inside": [{"hash": infohash}], "blockers": [], "conflicts": [], "ghosts": [], "copy_bytes": 0,
                  "movable": True, "target_host": "/host" + os.path.join(show_dir, os.path.basename(root))}

    def refresh(self):
        self.calls.append("refresh")

    def preview(self, source, target):
        self.calls.append(("preview", source, target))
        return self.a

    def move(self, source, target):
        self.calls.append(("move", source, target))
        return "b1"

    def local(self, p):
        return p[len("/host"):]

    def batch(self, b):
        return self.jobs, self.dry

    def dry_run(self):
        return self.mode_dry


def test_approve_with_cross_eyed_moves_the_torrent_in_and_lists_the_original(tmp_path, library, monkeypatch):
    w, s = _client_worker(tmp_path, library, [], library_cross_eyed_url="http://ce", library_cross_eyed_prefix="/host",
                          local_to_qbit=[])
    w.seasons = {s.key: s}
    w.wake = SimpleNamespace(set=lambda: None)
    root = tmp_path / "out" / "Alpha.S04.1080p-NEW"
    root.mkdir(parents=True)
    w.state.update(s.key, status=lib.SWAP, root=str(root), infohash="ih")
    show_dir = os.path.dirname(s.dir)
    ce = FakeCrossEyed(show_dir, str(root), "ih")
    monkeypatch.setattr(lib, "CrossEyed", lambda *a: ce)
    monkeypatch.setattr(lib, "seeded_from", lambda *a: [])
    monkeypatch.setattr(w, "_torrent", lambda h: None)
    monkeypatch.setattr(lib, "swap_plan", lambda *a: [])
    monkeypatch.setattr(lib.threading, "Thread", lambda target, args, **k: SimpleNamespace(start=lambda: target(*args)))
    ce.active = lambda: 0
    ce.a["inside"] = []                                 # not known yet: refreshed first (nothing running)
    real_preview = ce.preview

    def preview(source, target):
        out = real_preview(source, target)
        return dict(out, inside=[{"hash": "ih"}]) if "refresh" in ce.calls else out
    ce.preview = preview
    moved_to = os.path.join(show_dir, root.name)
    w.approve(s.key)
    it = w.state.seasons[s.key]
    assert it["status"] == lib.MOVING and ("move", str(root), show_dir) in ce.calls and "refresh" in ce.calls
    assert it["moved_to"] == moved_to
    info = {"progress": 1.0, "content_path": str(root), "state": "stalledUP"}
    w.app._qbit = lambda: SimpleNamespace(info=lambda h: info, start=lambda h: None)
    # held (dry run): said so, still moving
    ce.jobs = [{"batch_id": "b1", "state": "pending"}]
    w.follow_moves(w.app.cfg)
    assert it["status"] == lib.MOVING and "dry-run" in it["why"]
    # cross-eyed's finalize is stuck, but qBittorrent seeds it from the new folder: moved
    ce.jobs = [{"batch_id": "b1", "state": "done", "job_type": "relocate"}, {"batch_id": "b1", "state": "awaiting_user", "job_type": "folder_finalize"}]
    info["content_path"] = moved_to
    w.follow_moves(w.app.cfg)
    assert w.state.seasons[s.key]["status"] == lib.MOVED and "last step" in w.state.seasons[s.key]["why"]
    info["content_path"] = str(root)
    w.in_client = lambda: {"/elsewhere"}
    with pytest.raises(ValueError):
        w.delete_original(s.key)                    # still seeding from the old place
    info["content_path"] = moved_to
    w.in_client = lambda: {os.path.normcase(os.path.abspath(os.path.join(s.dir, "x.mkv")))}
    with pytest.raises(ValueError):
        w.delete_original(s.key)                    # a torrent has files in the original
    assert os.path.isdir(s.dir)
    w.in_client = lambda: {"/elsewhere"}
    w.delete_original(s.key)
    assert not os.path.exists(s.dir) and w.state.seasons[s.key]["status"] == lib.ORIGINAL_DELETED


def test_cross_eyed_in_dry_run_mode_moves_nothing_and_says_so(tmp_path, library, monkeypatch):
    w, s = _client_worker(tmp_path, library, [], library_cross_eyed_url="http://ce", library_cross_eyed_prefix="/host",
                          library_auto_move=True, library_auto_delete=False)
    w.seasons = {s.key: s}
    root = tmp_path / "out" / "Alpha.S04.1080p-NEW"
    root.mkdir(parents=True)
    w.state.update(s.key, status=lib.SWAP, root=str(root), infohash="ih", why="seeding at 100.0%")
    ce = FakeCrossEyed(os.path.dirname(s.dir), str(root), "ih")
    ce.mode_dry = True
    monkeypatch.setattr(lib, "CrossEyed", lambda *a: ce)
    monkeypatch.setattr(lib.threading, "Thread", lambda target, args, **k: SimpleNamespace(start=lambda: target(*args)))
    assert w.check_dry_run(w.app.cfg) is True
    w.auto_moves(w.app.cfg)
    assert w.state.seasons[s.key]["status"] == lib.SWAP and ce.calls == []      # not handed over
    with pytest.raises(ValueError, match="dry-run"):
        w.approve(s.key)
    monkeypatch.setattr(w, "torrent_states", lambda: {})
    w.current = None
    assert w.items()["cross_eyed_dry_run"] is True
    # switched to dry run while being handed over: back to ready, and auto move tries it again later
    w.ce_dry = False
    w.approve(s.key)
    it = w.state.seasons[s.key]
    assert it["status"] == lib.SWAP and it["why"] == lib.DRY_RUN_WAIT and ("move", str(root), os.path.dirname(s.dir)) not in ce.calls
    ce.mode_dry = False
    ce.active = lambda: 0
    w.check_dry_run(w.app.cfg)
    w.auto_moves(w.app.cfg)
    assert w.state.seasons[s.key]["status"] == lib.MOVING and ("move", str(root), os.path.dirname(s.dir)) in ce.calls


def test_a_hand_over_a_restart_cut_off_is_ready_again(tmp_path):
    st = lib.State(str(tmp_path / "library.json"))
    st.update("a", status=lib.MOVING, why="approved - handing it to cross-eyed")
    st.update("b", status=lib.MOVING, batch="b1")
    st2 = lib.State(str(tmp_path / "library.json"))
    assert st2.seasons["a"]["status"] == lib.SWAP and not st2.seasons["a"]["why"].startswith("not moved")
    assert st2.seasons["b"]["status"] == lib.MOVING


def test_a_failed_cross_eyed_move_goes_back_to_ready(tmp_path, library):
    w, s = _client_worker(tmp_path, library, [], library_cross_eyed_url="http://ce", library_cross_eyed_prefix="/host")
    root = tmp_path / "out" / "Rel"
    root.mkdir(parents=True)
    w.state.update(s.key, status=lib.MOVING, batch="b1", root=str(root), moved_to="/elsewhere/Rel", infohash="ih")
    w.app._qbit = lambda: SimpleNamespace(info=lambda h: {"progress": 1.0, "content_path": str(root), "state": "stalledUP"})
    ce = SimpleNamespace(batch=lambda b: ([{"batch_id": "b1", "state": "rolled_back"}], False))
    import nzb2seed.library as L
    orig = L.CrossEyed
    L.CrossEyed = lambda *a: ce
    try:
        w.follow_moves(w.app.cfg)
    finally:
        L.CrossEyed = orig
    assert w.state.seasons[s.key]["status"] == lib.SWAP and "rolled_back" in w.state.seasons[s.key]["why"]



def test_a_season_whose_own_build_a_restart_cut_off_is_settled_and_started(tmp_path, library, monkeypatch):
    """Its torrent was added and rechecked to 100% when a restart cut the build off: it is
    started and offered as a swap - not called "in the client" because of its own torrent."""
    w, s = _client_worker(tmp_path, library, [("Alpha.2009.S04.1080p.BluRay.x265-NEW", "TL")],
                          start_when_complete=True)
    started = []
    info = {"progress": 1.0, "state": "stoppedUP"}
    w.app._qbit = lambda: SimpleNamespace(info=lambda h: info, start=started.append)
    w.state.update(s.key, status=lib.WAITING, infohash="ih")      # cut off before its root was recorded
    w.app.cfg.library_output_dir = str(tmp_path / "out")
    w.app.cfg.library_roots = [library.root]
    monkeypatch.setattr(w, "_torrent", lambda h: SimpleNamespace(name="Rel"))
    monkeypatch.setattr(lib, "torrent_mod_root", lambda t, out: os.path.join(out, t.name))
    monkeypatch.setattr(lib, "swap_plan", lambda *a: [("x",)])
    it = w.state.seasons[s.key]
    assert w._own_build(s, it)
    assert it["status"] == lib.SWAP and started == ["ih"]
    assert it["root"] == os.path.join(str(tmp_path / "out"), "Rel")
    # short of 100%: built again, never "in the client"
    info.update(progress=0.5)
    it["status"] = lib.IN_CLIENT
    assert w._own_build(s, it) is None and it["status"] == lib.WAITING



def test_a_service_not_answering_is_tried_again_by_itself(tmp_path, library, monkeypatch):
    """qBittorrent refusing the connection (a VPN reconnect) is no reason to stop the
    season for good: it waits a little, then the build runs again and reuses its pack."""
    import requests
    w, cfg, s, pipeline = _worker(tmp_path, library, monkeypatch, "Alpha (2009) S04 1080p BluRay x265-pCf")
    w.app.cfg = cfg

    def refused(*a, **k):
        raise requests.ConnectionError("HTTPConnectionPool(host='q', port=8082): Max retries exceeded "
                                       "(Failed to establish a new connection: [Errno 111] Connection refused)")
    monkeypatch.setattr(pipeline, "execute_assemble", refused)
    with pytest.raises(requests.ConnectionError):
        w.run(cfg, s.key)
    it = w.state.seasons[s.key]
    assert it["status"] == lib.WAITING and it["retry_at"] > time.time() + 60
    assert lib.unreachable(ValueError("no usable season pack")) is False



def test_an_approved_move_can_be_cancelled_only_until_cross_eyed_starts_it(tmp_path, library, monkeypatch):
    w, s = _client_worker(tmp_path, library, [], library_cross_eyed_url="http://ce", library_cross_eyed_prefix="/host")
    w.state.update(s.key, status=lib.MOVING, batch="b1", moved_to="/x")
    jobs = [{"batch_id": "b1", "state": "pending"}, {"batch_id": "b1", "state": "pending"}]
    cancelled = []

    def cancel(b):
        cancelled.append(b)
        jobs.clear()
    ce = SimpleNamespace(batch=lambda b: (list(jobs), True), cancel=cancel)
    monkeypatch.setattr(lib, "CrossEyed", lambda *a: ce)
    # started: refused, nothing cancelled
    jobs[0]["state"] = "rechecking"
    with pytest.raises(ValueError, match="no longer be cancelled"):
        w.cancel_move(s.key)
    assert cancelled == [] and w.state.seasons[s.key]["status"] == lib.MOVING
    # still held (dry run): cancelled, back to ready
    jobs[0]["state"] = "pending"
    w.cancel_move(s.key)
    assert cancelled == ["b1"] and w.state.seasons[s.key]["status"] == lib.SWAP



def test_a_season_with_no_pack_ends_marked_as_nothing_built(tmp_path, library, monkeypatch):
    """The job list shows it grey, not as a success."""
    w, cfg, s, pipeline = _worker(tmp_path, library, monkeypatch, "Something Else S09 720p")
    w.app.cfg = cfg
    monkeypatch.setattr(w, "look", lambda cfg_, key: None)
    out = w.run(cfg, s.key)
    assert out["extra"]["nothing_built"] is True and w.state.seasons[s.key]["status"] == lib.NO_PACK


def test_still_checking_is_not_a_failure():
    from nzb2seed import library
    from nzb2seed.pipeline import Abort
    assert library.still_checking(Abort("this torrent is already in qBittorrent and active (checkingDL); stop it first"))
    assert not library.still_checking(Abort("this torrent is already in qBittorrent and active (uploading); stop it first"))
    assert not library.still_checking(Abort("something else went wrong while checking"))


def test_seeded_only_by_a_torrent_qbittorrent_has(tmp_path):
    import os
    from nzb2seed import library
    lib = tmp_path / "tv" / "Show" / "Season 1"
    lib.mkdir(parents=True)
    ep = lib / "Show.S01E01.mkv"
    ep.write_bytes(b"x" * 100)
    left = tmp_path / "torrents" / "Show.S01.Never.Added"
    left.mkdir(parents=True)
    os.link(ep, left / "Show.S01E01.mkv")                # a build that was never added
    s = library.scan([str(tmp_path / "tv")])[0]
    dev = os.stat(lib).st_dev
    assert not library.seeded(s, set(), library.files_of(set(), dev))
    seeding = {str(left)}                                 # qBittorrent has it, complete
    assert library.seeded(s, seeding, library.files_of(seeding, dev))


def test_a_build_from_another_tracker_reuses_the_first_ones_folder(tmp_path):
    from types import SimpleNamespace
    from nzb2seed import pipeline
    first = tmp_path / "torrents" / "Show S01 GRP"
    first.mkdir(parents=True)
    t = SimpleNamespace(name="Show (S01) - GRP", multi_file=True)
    opts = pipeline.Options(reuse_dirs=(str(first), str(tmp_path / "gone")))
    assert pipeline.reuse_dirs_for(t, opts, str(tmp_path / "torrents")) == [str(first)]
    same = SimpleNamespace(name="Show S01 GRP", multi_file=True)       # the same folder: not reused
    assert pipeline.reuse_dirs_for(same, opts, str(tmp_path / "torrents")) == []


def test_torrent_label():
    from nzb2seed.library import torrent_label
    assert torrent_label({"state": "stalledUP", "progress": 1}) == "seeding"
    assert torrent_label({"state": "stoppedUP", "progress": 1}) == "stopped (complete)"
    assert torrent_label({"state": "checkingDL", "progress": 0.5}) == "checking (50.0%)"
    assert torrent_label({"state": "stoppedDL", "progress": 0.999}) == "stopped at 99.9%"
    assert torrent_label(None) == "not in qBittorrent"
    assert torrent_label(None, True) == "qBittorrent not answering"


def test_a_move_is_done_only_once_rechecked():
    from nzb2seed.library import moves_done
    assert not moves_done([{"job_type": "relocate", "state": "rechecking"}, {"job_type": "folder_finalize", "state": "pending"}])
    assert moves_done([{"job_type": "relocate", "state": "done"}, {"job_type": "folder_finalize", "state": "failed"}])


def test_hook_saves_holds_and_restores(tmp_path):
    from types import SimpleNamespace
    from nzb2seed import hook as hook_mod
    out = tmp_path / "ran.txt"

    class Q:
        prefs = {"autorun_enabled": True, "autorun_program": f'echo "%I %N" >> "{out}"'}
        def preferences(self): return dict(self.prefs)
        def set_preferences(self, p): self.prefs.update(p)
        def info(self, h): return {"hash": h, "name": "Show S01"}
    q = Q()
    app = SimpleNamespace(cfg=SimpleNamespace(qbit_url="http://192.0.2.10:8082", library_enabled=True,
                                              library_hook_url=""), gui_port=9797, _qbit=lambda: q)
    hk = hook_mod.Hook(app, str(tmp_path / "hook.json"))
    original = q.prefs["autorun_program"]
    assert hk.install() == "installed"
    assert hook_mod.ours(q.prefs["autorun_program"]) and "192.0.2.10:9797" in q.prefs["autorun_program"]
    assert hk.install() == "already in place"            # the saved original is not overwritten
    assert hk.data["saved"]["program"] == original
    h = "a" * 40
    assert hk.received(h, busy=True).startswith("held")
    hk.tick(busy=True)
    assert not out.exists()
    hk.tick(busy=False)
    assert out.read_text().strip() == f"{h} Show S01" and hk.data["queue"] == []
    assert hk.received("nope", busy=False) == "not an infohash"
    assert hk.restore() == "restored"
    assert q.prefs["autorun_program"] == original and q.prefs["autorun_enabled"] is True
    assert hk.restore().startswith("nothing saved")
    assert not hk.check_token("wrong") and hk.check_token(hk.data["token"])


def test_hook_setting_off_restores(tmp_path):
    from types import SimpleNamespace
    from nzb2seed import hook as hook_mod

    class Q:
        prefs = {"autorun_enabled": False, "autorun_program": "true"}
        def preferences(self): return dict(self.prefs)
        def set_preferences(self, p): self.prefs.update(p)
    q = Q()
    cfg = SimpleNamespace(qbit_url="http://h:8082", library_enabled=True, library_hold_hook=True, library_hook_url="")
    app = SimpleNamespace(cfg=cfg, gui_port=9797, _qbit=lambda: q)
    hk = hook_mod.Hook(app, str(tmp_path / "hook.json"))
    hk.sync()
    assert hook_mod.ours(q.prefs["autorun_program"])
    app.cfg = SimpleNamespace(**{**vars(cfg), "library_hold_hook": False})
    hk.sync()
    assert q.prefs == {"autorun_enabled": False, "autorun_program": "true"}


# ---------------------------------------------------------------- one tracker, or several in order

def test_the_order_one_tracker_by_default_several_when_chosen():
    one = SimpleNamespace(library_indexer="TL", library_multi=False, library_order=["TB", lib.OWN, "TL"])
    assert lib.order(one) == [lib.OWN, "TL"]                       # the list is kept, not used
    several = SimpleNamespace(library_indexer="TL", library_multi=True, library_order=["TB", lib.OWN, "TL"])
    assert lib.order(several) == ["TB", lib.OWN, "TL"] and lib.trackers(several) == ["TB", "TL"]
    no_own = SimpleNamespace(library_indexer="", library_multi=True, library_order=["TB", "TL", "tb"])
    assert lib.order(no_own) == [lib.OWN, "TB", "TL"]              # your files at the top; no doubles
    assert lib.order(SimpleNamespace(library_indexer="", library_multi=True, library_order=[lib.OWN])) == []
    assert lib.order(SimpleNamespace(library_indexer="TL")) == [lib.OWN, "TL"]     # an older config
    assert lib.trackers_text(several) == "TB or TL" and lib.is_ours(several, "TB (API)")


def _picker(tmp_path, library, monkeypatch, packs, **cfg):
    """A Library worker against a fake Prowlarr with three trackers; ``packs``: {tracker:
    "same" (the library's very files) | "other" (another release of the season)}."""
    s = lib.scan([library.root])[0]
    same = make_torrent("Alpha.2009.S04.1080p.BluRay.x265-pCf",
                        {**library.eps, "Alpha.2009.S04.1080p.BluRay.x265-pCf.nfo": b"nfo"})
    other = make_torrent("Alpha.2009.S04.1080p.WEB-DL.x264-GRP",
                         {f"Alpha.2009.S04E0{n}.1080p.WEB-DL.x264-GRP.mkv": rnd(30_000 + n, n + 7) for n in (1, 2, 3)})
    titles = {"same": "Alpha 2009 S04 1080p BluRay x265-pCf", "other": "Alpha 2009 S04 1080p WEB-DL x264-GRP"}
    ids = {"TL": 7, "TB": 8, "TC": 9}
    searched = []

    class FakePr:
        def __init__(self, *a): pass
        def indexers(self): return [(i, n) for n, i in ids.items()]

        def search(self, q, want, cats):
            searched.append(sorted(want))
            return [Release(title=titles[kind], protocol="torrent", indexer=n, indexer_id=ids[n], size=10 * 1024 ** 3,
                            guid=f"{n}:{kind}", download_url="", info_url="", publish_date="", grabs=None,
                            seeders=5, files=None)
                    for n, kind in packs.items() if ids[n] in want]

        def fetch(self, r): return same if r.guid.endswith(":same") else other
    monkeypatch.setattr(lib, "Prowlarr", FakePr)
    w = object.__new__(lib.Library)
    w.app = SimpleNamespace(cfg=None)
    w.state = lib.State(str(tmp_path / "library.json"))
    w.seasons = {s.key: s}
    w.current = None
    c = SimpleNamespace(prowlarr_url="", prowlarr_key="", torrent_dir=str(tmp_path / "torrents"),
                        library_indexer="TL", library_multi=True, library_order=[lib.OWN, "TL", "TB"])
    for k, v in cfg.items():
        setattr(c, k, v)
    return w, c, s, searched


def test_your_files_at_the_top_win_on_any_ticked_tracker(tmp_path, library, monkeypatch):
    w, cfg, s, searched = _picker(tmp_path, library, monkeypatch, {"TL": "other", "TB": "same"})
    p, _, _, same = w.look(cfg, s.key)
    assert (p.release.indexer, same) == ("TB", True)
    assert searched == [[7, 8]]                                    # one search for both
    assert w.state.seasons[s.key]["offer"]["tracker"] == "TB"


def test_a_tracker_above_your_files_wins_with_its_own_pack(tmp_path, library, monkeypatch):
    w, cfg, s, _ = _picker(tmp_path, library, monkeypatch, {"TL": "other", "TB": "same"},
                           library_order=["TL", lib.OWN, "TB"])
    p, _, _, same = w.look(cfg, s.key)
    assert (p.release.indexer, same) == ("TL", False)


def test_the_next_tracker_when_the_first_has_no_pack(tmp_path, library, monkeypatch):
    w, cfg, s, _ = _picker(tmp_path, library, monkeypatch, {"TB": "other", "TC": "same"})
    p, _, _, same = w.look(cfg, s.key)                             # TC is not ticked: never used
    assert (p.release.indexer, same) == ("TB", False)


def test_one_tracker_searches_only_that_tracker(tmp_path, library, monkeypatch):
    w, cfg, s, searched = _picker(tmp_path, library, monkeypatch, {"TL": "other", "TB": "same"},
                                  library_multi=False)
    p, _, _, same = w.look(cfg, s.key)
    assert (p.release.indexer, same, searched) == ("TL", False, [[7]])


def test_a_new_order_means_a_new_search(tmp_path, library, monkeypatch):
    w, cfg, s, _ = _picker(tmp_path, library, monkeypatch, {"TL": "other", "TB": "same"})
    w.look(cfg, s.key)
    assert w.kept_pick(cfg, s.key)[0].release.indexer == "TB"
    cfg.library_order = ["TL", lib.OWN, "TB"]
    assert w.kept_pick(cfg, s.key) is None
    assert lib.offer_current({"indexer": "TL"}, SimpleNamespace(library_indexer="TL"))   # an older offer


def test_no_pack_seasons_search_again_only_for_a_newly_ticked_tracker(tmp_path, library, monkeypatch):
    w, cfg, s, searched = _picker(tmp_path, library, monkeypatch, {"TC": "same"})
    assert w.look(cfg, s.key) is None
    w.state.update(s.key, status=lib.NO_PACK)
    assert w.reopen_no_pack(cfg) == 0                              # nothing changed
    cfg.library_order = ["TB", lib.OWN, "TL"]                      # reordered, nothing new
    assert w.reopen_no_pack(cfg) == 0
    cfg.library_order = [lib.OWN, "TL"]                            # one unticked
    assert w.reopen_no_pack(cfg) == 0
    cfg.library_order = [lib.OWN, "TL", "TC"]                      # TC ticked
    assert w.reopen_no_pack(cfg) == 1
    assert w.state.seasons[s.key]["status"] == lib.WAITING and not w.state.seasons[s.key]["offer"]
    w.state.update(s.key, status=lib.NO_PACK, offer=None)           # from before searches were recorded
    assert w.reopen_no_pack(SimpleNamespace(library_indexer="TL", library_multi=False)) == 0
    assert w.reopen_no_pack(cfg) == 1                               # TC was never searched
    assert lib.searched_on({"indexer": "TL"}) == {lib.config_key("TL")}     # an older offer
    assert lib.searched_on({"order": [lib.OWN, "TL", "TB"]}) == {lib.config_key("TL"), lib.config_key("TB")}


def test_a_season_one_of_the_ticked_trackers_has_is_left_alone(tmp_path, library):
    w, s = _client_worker(tmp_path, library, [("Alpha.2009.S04.720p.WEB-DL-UAi", "TB")],
                          library_redownload=True, library_multi=True, library_order=[lib.OWN, "TL", "TB"])
    assert w.client_status(s, w.client(), w.app.cfg)[0] == lib.IN_CLIENT
