"""The "File list to seeds" tab: pasted names read, and each judged plausible only
when Usenet has posts that could build the torrent."""
import pytest

from nzb2seed import tracklist
from nzb2seed.clients import Release
from nzb2seed.config import Config

GB = 1024 ** 3


def rel(title, protocol, size, indexer="TrackerNine", seeders=None, grabs=None):
    return Release(title=title, protocol=protocol, indexer=indexer, indexer_id=1 if protocol == "torrent" else 9,
                   size=size, guid=title + protocol + indexer, download_url="", info_url="", publish_date="",
                   grabs=grabs, seeders=seeders, files=None)


class FakeProwlarr:
    def __init__(self, results):
        self.results, self.asked = results, []

    def indexers(self):
        return [(1, "TrackerNine"), (2, "TrackerX")]

    def search(self, query, ids=None, categories=None):
        self.asked.append((query, tuple(ids or ())))
        return [r for r in self.results.get(query, [])
                if (r.protocol == "torrent") == (tuple(ids or ()) != (9,))]


def run(results, name, tracker="TrackerNine"):
    return tracklist.check(Config("x.json"), FakeProwlarr(results), name, tracker, [9])


def test_names_are_read_from_rows_copied_off_a_tracker_page():
    text = ("Name\tSize\tSeeders\n"
            "Show.S01.1080p.WEB-DL.DDP5.1.H.264-Grpv\t24.1 GB\t12\n"
            "  Film.2020.1080p.BluRay.x264-Rel.torrent  \n"
            "Uploaded 2 hours ago\n"
            "Show S02 1080p WEB-DL DDP5 1 H 264-Grpv    3.2 GB    5\n"
            "Show.S01.1080p.WEB-DL.DDP5.1.H.264-Grpv\n"
            "12\n")
    assert tracklist.parse(text) == ["Show.S01.1080p.WEB-DL.DDP5.1.H.264-Grpv",
                                     "Film.2020.1080p.BluRay.x264-Rel",
                                     "Show S02 1080p WEB-DL DDP5 1 H 264-Grpv"]


def test_titles_headings_chat_and_footers_are_not_taken_for_torrents():
    text = "Pick of the Week\nSome Film (2019)\nFILM\nAnother Film (2006)\n2160p WEB-DL\n\t\nNew.Film.2023.2160p.AMZN.WEB-DL.DDP2.0.H.265-Rel\n(Anonymous)\n\t11.03 GiB\n14 minutes ago\nsomeone posted 12 messages today\nOn Torrent Show S01 480p WEB-DL AAC2.0 H.264-Grpv\nA Book - An Author [2023] [AUDIOBOOK] [Rel]\n1999 2001 1080p BluRay x264-Rel\nDate: 2026-01-01 04:35:17\nSite and design (c) 2024-2026 Site | v1.0"
    assert tracklist.parse(text) == ["New.Film.2023.2160p.AMZN.WEB-DL.DDP2.0.H.265-Rel",
                                     "Show S01 480p WEB-DL AAC2.0 H.264-Grpv",
                                     "1999 2001 1080p BluRay x264-Rel"]


def test_the_same_release_on_usenet_is_plausible():
    name = "Film.2020.1080p.BluRay.x264-Rel"
    r = run({name: [rel(name, "torrent", 10 * GB, seeders=40),
                    rel(name, "usenet", 10.4 * GB, indexer="UsenetA", grabs=3),
                    rel("Film.2020.720p.BluRay.x264-Rel", "usenet", 5 * GB)]}, name)
    assert r["plausible"] and r["torrent"]["title"] == name
    assert [n["title"] for n in r["nzbs"]] == [name]


def test_a_name_shown_with_spaces_finds_the_dotted_torrent():
    shown, real = "Film 2020 1080p BluRay DDP5 1 x264-Rel", "Film.2020.1080p.BluRay.DDP5.1.x264-Rel"
    r = run({shown: [rel(real, "torrent", 10 * GB), rel(real, "usenet", 11 * GB)]}, shown)
    assert r["plausible"] and r["torrent"]["title"] == real


def test_a_post_smaller_than_the_torrent_is_not_plausible():
    name = "Film.2020.1080p.BluRay.x264-Rel"
    r = run({name: [rel(name, "torrent", 10 * GB), rel(name, "usenet", 6 * GB)]}, name)
    assert not r["plausible"] and "smaller" in r["verdict"]


def test_another_group_on_usenet_is_not_plausible():
    name = "Film.2020.1080p.BluRay.x264-Rel"
    r = run({name: [rel(name, "torrent", 10 * GB), rel("Film.2020.1080p.BluRay.x264-Grpv", "usenet", 10 * GB)]}, name)
    assert not r["plausible"] and r["verdict"].startswith("no Usenet post of this release")


def test_a_season_pack_is_plausible_from_every_episode_of_the_same_release():
    pack = "Show.S01.1080p.WEB-DL.H.264-Grpv"
    eps = [rel(f"Show.S01E0{i}.1080p.WEB-DL.H.264-Grpv", "usenet", 1.1 * GB) for i in range(1, 4)]
    query = "show 1080p web dl h 264 grpv"
    assert run({pack: [rel(pack, "torrent", 3 * GB)], query: eps}, pack)["plausible"]
    short = run({pack: [rel(pack, "torrent", 3 * GB)], query: eps[:1]}, pack)
    assert not short["plausible"] and "not the whole season" in short["verdict"]


def test_a_name_the_tracker_does_not_have_says_what_it_found_instead():
    name = "Film.2020.1080p.BluRay.x264-Rel"
    r = run({name: [rel("Film.2020.2160p.BluRay.x265-Rel", "torrent", 20 * GB), rel(name, "usenet", 10 * GB)]}, name)
    assert r["torrent"] is None and not r["plausible"]
    assert r["others"] == ["Film.2020.2160p.BluRay.x265-Rel"] and "not found on TrackerNine" in r["verdict"]


def test_a_tracker_that_finds_nothing_by_the_whole_name_is_asked_for_the_title():
    name = "Film.2020.1080p.Remux.AVC.DTS-HD.MA.5.1-Rel"
    r = run({name: [rel(name, "usenet", 30 * GB)],
             "Film 2020": [rel("Film.2020.2160p.Remux-Rel", "torrent", 60 * GB), rel(name, "torrent", 29 * GB)]}, name)
    assert r["plausible"] and r["torrent"]["title"] == name
    assert tracklist.short_query("Show.S01.1080p.WEB-DL-Grpv") == "Show S01"
    assert tracklist.short_query("1999 2001 1080p BluRay x264-Rel") == "1999 2001"


def test_the_tracker_is_only_asked_about_names_usenet_has():
    pr = FakeProwlarr({})
    r = tracklist.check(Config("x.json"), pr, "Film.2020.1080p.BluRay.x264-Rel", "TrackerNine", [9])
    assert not r["plausible"] and all(ids == (9,) for _, ids in pr.asked)


def test_only_the_chosen_tracker_is_searched_for_the_torrent():
    pr = FakeProwlarr({"Film.2020.1080p.BluRay.x264-Rel": [rel("Film.2020.1080p.BluRay.x264-Rel", "usenet", GB)]})
    tracklist.check(Config("x.json"), pr, "Film.2020.1080p.BluRay.x264-Rel", "trackerx", [9])
    assert pr.asked[-1][1] == (2,)
    with pytest.raises(ValueError):
        tracklist.check(Config("x.json"), pr, "Film.2020.1080p.BluRay.x264-Rel", "Nowhere", [9])


def test_the_uploader_shown_after_a_name_is_left_off():
    text = ("Some Film 2009 1080p BluRay DD+7.1 x264-Rel Some-User\n"
            "Show S01 1080p AMZN WEB-DL DD+ 5.1 H.264-Grpv Uploader\n"
            "Other Film 2012 1080p USA Blu-ray AVC DTS-HD MA 5.1 Anon\n"
            "New Show S02 2160p WEB-DL DD+ 5.1 H.265-Grpv someone42\n")
    assert tracklist.parse(text) == ["Some Film 2009 1080p BluRay DD+7.1 x264-Rel",
                                     "Show S01 1080p AMZN WEB-DL DD+ 5.1 H.264-Grpv",
                                     "Other Film 2012 1080p USA Blu-ray AVC DTS-HD MA 5.1",
                                     "New Show S02 2160p WEB-DL DD+ 5.1 H.265-Grpv"]
    # a release name of its own is left as it is
    for n in ("Show.S01.1080p.WEB-DL.DDP5.1.H.264-Grpv", "Film 2020 1080p WEB-DL DDP5.1 H.264-Rel",
              "Film 2020 1080p BluRay DTS-HD MA"):
        assert tracklist.bare(n) == n


def test_a_name_the_tracker_rewrote_is_matched_by_what_the_release_is():
    shown = "Some Film 2026 2160p iT WEB-DL DD+ 5.1 Atmos DV HDR10+ H.265-Rel"
    post = "Some.Film.2026.2160p.iTunes.WEB-DL.DDP5.1.Atmos.DV.HDR10+.H.265-Rel"
    r = run({"Some Film 2026": [rel(post, "usenet", 17.1 * GB, indexer="UsenetA"),
                                rel("Some.Film.2026.2160p.iTunes.WEB-DL.DDP5.1.Atmos.DV.H.265-Rel", "usenet", 17 * GB),
                                rel("Some.Film.2026.1080p.iTunes.WEB-DL.DDP5.1.H.264-Rel", "usenet", 8 * GB),
                                rel(shown, "torrent", 17 * GB)]}, shown)
    assert r["plausible"] and [n["title"] for n in r["nzbs"]] == [post]
    assert post in r["verdict"]


def test_a_rewritten_season_pack_is_matched_from_its_episodes():
    pack = "Show S01 1080p NF WEB-DL DD+ 5.1 Atmos x264-Grpv"
    eps = [rel(f"Show.S01E0{i}.Title.1080p.NF.WEB-DL.DDP5.1.Atmos.H.264-Grpv", "usenet", 1.1 * GB) for i in (1, 2, 3)]
    other = [rel("Show.S01E01.Title.720p.NF.WEB-DL.DDP5.1.Atmos.H.264-Grpv", "usenet", 0.5 * GB),
             rel("Show.S01E01.Title.1080p.AMZN.WEB-DL.DDP5.1.H.264-Grpv", "usenet", 1.2 * GB)]
    r = run({"Show S01": eps + other + [rel(pack, "torrent", 3.2 * GB)]}, pack)
    assert r["plausible"] and len(r["nzbs"]) == 3 and "season pack" in r["verdict"]


def test_what_makes_a_release_another_one_is_never_matched():
    name = "Some Film 2020 1080p AMZN WEB-DL DD+ 5.1 H.264-Rel"
    for post in ["Some.Film.2020.1080p.AMZN.WEB-DL.DDP5.1.H.264-Grpv",      # group
                 "Some.Film.2020.720p.AMZN.WEB-DL.DDP5.1.H.264-Rel",       # resolution
                 "Some.Film.2020.1080p.NF.WEB-DL.DDP5.1.H.264-Rel",        # service
                 "Some.Film.2020.1080p.AMZN.WEB-DL.DDP5.1.H.265-Rel",      # codec
                 "Some.Film.2019.1080p.AMZN.WEB-DL.DDP5.1.H.264-Rel",      # year
                 "Some.Film.Returns.2020.1080p.AMZN.WEB-DL.DDP5.1.H.264-Rel",  # title
                 "Some.Film.2020.1080p.BluRay.DD5.1.x264-Rel",             # source
                 "Some.Film.2020.1080p.AMZN.WEB-DL.DDP5.1.HDR.H.264-Rel"]:  # HDR
        assert not tracklist.alike(post, name), post
    assert not tracklist.alike("Some.Film.2020.2160p.AMZN.WEB-DL.DDP5.1.H.265-Rel",
                               "Some Film 2020 2160p AMZN WEB-DL DD+ 5.1 HDR10+ H.265-Rel")
    assert tracklist.alike("Some.Film.2020.1080p.AMZN.WEB-DL.DDP5.1.H.264-Rel", name)
    assert not tracklist.alike("Some.Film.2020.1080p.WEB-DL", "Some Film 2020 1080p WEB-DL")   # no group: never


def test_the_uploader_after_a_dotted_name_is_left_off():
    assert tracklist.bare("Show.S03.2160p.NF.WEB-DL.DDP5.1.DV.HDR.H.265-Grpv Anon") == \
        "Show.S03.2160p.NF.WEB-DL.DDP5.1.DV.HDR.H.265-Grpv"
    assert tracklist.bare("Show S01") == "Show S01"


def test_a_row_of_source_filters_is_not_a_release():
    assert tracklist.parse("BDRip BluRay BluRay Raw HDRip HDTV REMUX WEB-DL WEBRip clear\n"
                           "Film.2001.1080p.BluRay.x264-Rel\n") == ["Film.2001.1080p.BluRay.x264-Rel"]


def test_a_film_with_an_edition_is_also_searched_by_its_title_and_year():
    assert tracklist.title_year("Some Film 1985 Directors Cut version 1080p Bluray REMUX AVC DD 2.0-Rel") == "Some Film 1985"
    assert tracklist.title_year("Film.2020.1080p.BluRay.x264-Rel") is None          # nothing to leave off
    assert tracklist.title_year("Show 2020 S01 1080p WEB-DL-Rel") is None
    name = "Some Film 1985 Directors Cut 1080p Bluray REMUX AVC DTS-HD MA 5.1-Rel"
    post = "Some.Film.1985.Directors.Cut.1080p.BluRay.REMUX.AVC.DTS-HD.MA.5.1-Rel"
    r = run({"Some Film 1985": [rel(post, "usenet", 30 * GB)]}, name, tracker="")
    assert r["on_usenet"] and post in r["verdict"]


def test_a_release_with_no_group_matches_only_a_post_with_none_and_of_its_size():
    name = "Show S01 1080p NF WEB-DL AAC 2.0 x264-AAC2.0.x264"           # the tracker's stand-in for no group
    post = "Show.S01.1080p.NF.WEB-DL.HE-AAC2.0.H.264"
    assert tracklist.facets(name)["group"] is None and tracklist.facets("Film 2020 1080p Blu-ray AVC-NoGroup")["group"] is None
    assert tracklist.alike(post, name)
    for other in ["Show.S01.1080p.NF.WEB-DL.AAC2.0.H.264-Rel",           # has a group
                  "Show.S01.1080p.AMZN.WEB-DL.AAC2.0.H.264",             # another service
                  "Show S01 1080p"]:                                     # says too little
        assert not tracklist.alike(other, name), other
    found = run({"Show S01": [rel(post, "usenet", 15.2 * GB)]}, name, tracker="")
    assert found["on_usenet"] and found["verdict"].startswith("possibly the release") and "names a release group" in found["verdict"]
    fits = run({"Show S01": [rel(post, "usenet", 15.2 * GB), rel(name, "torrent", 15 * GB)]}, name)
    assert fits["plausible"]
    bigger = run({"Show S01": [rel(post, "usenet", 18 * GB), rel(name, "torrent", 15 * GB)]}, name)
    assert not bigger["plausible"]


def test_a_disc_is_never_taken_for_an_encode_of_it():
    disc = "Some Film 1986 1080p USA Blu-ray AVC TrueHD 5.1-NOGROUP"
    assert tracklist.facets(disc)["kind"] == "disc"
    assert tracklist.facets("Some.Film.1986.1080p.BluRay.H264.AAC")["kind"] == "bluray"
    assert tracklist.facets("Some.Film.1986.1080p.BluRay.REMUX.AVC.TrueHD.5.1")["kind"] == "remux"
    for post in ["Some.Film.1986.1080p.BluRay.H264.AAC", "Some.Film.1986.1080p.BluRay.x264",
                 "Some.Film.1986.1080p.Blu-ray.AVC.DTS-HD.MA.5.1"]:                 # other audio
        assert not tracklist.alike(post, disc), post
    assert tracklist.alike("Some.Film.1986.1080p.USA.Blu-ray.AVC.TrueHD.5.1", disc)


def test_with_no_tracker_only_usenet_is_searched():
    name = "Film.2020.1080p.BluRay.x264-Rel"
    pr = FakeProwlarr({name: [rel(name, "usenet", 10.4 * GB, indexer="UsenetA"), rel(name, "torrent", 10 * GB)]})
    r = tracklist.check(Config("x.json"), pr, name, "", [9])
    assert r["on_usenet"] and not r["plausible"] and r["torrent"] is None
    assert "UsenetA" in r["verdict"] and "no tracker chosen" in r["verdict"]
    assert all(ids == (9,) for _, ids in pr.asked)                    # no tracker was asked
    none = tracklist.check(Config("x.json"), FakeProwlarr({}), name, "", [9])
    assert not none["on_usenet"] and none["verdict"] == "no Usenet post of this release"


def test_a_list_with_no_tracker_never_builds(tmp_path):
    q = tracklist.Queue(FakeApp(), str(tmp_path / "t.json"), start=False)
    lst = q._find(q.add(["Film.2020.1080p.BluRay.x264-Rel"], "", True, {}))
    assert lst["tracker"] == "" and lst["auto"] is False
    with pytest.raises(ValueError):
        q.add([], "", False, {})


def test_a_job_title_reads_as_the_torrent_name():
    assert tracklist.job_key("auto: Film.2020.1080p.WEB-DL-Rel.mkv") == tracklist.job_key("Film 2020 1080p WEB-DL-Rel")
    assert tracklist.job_key("Film.2020.1080p.WEB-DL-Rel") != tracklist.job_key("Film.2020.2160p.WEB-DL-Rel")


class FakeApp:
    def __init__(self, on_list=()):
        self.cfg = Config("x.json")
        self.on_list, self.built, self.downloaded = set(on_list), [], []

    def listed(self, title):
        return type("J", (), {"id": 5})() if title in self.on_list else None

    def build_from_list(self, torrent, nzbs, options):
        if torrent["title"] in self.on_list:
            raise ValueError("already on the jobs list (job 5)")
        self.built.append((torrent["title"], options["pp"]))
        return type("J", (), {"id": 100 + len(self.built)})()

    def download_from_list(self, torrent, tracker):
        self.downloaded.append((torrent["title"], tracker))
        return type("J", (), {"id": 200 + len(self.downloaded)})()


@pytest.fixture
def queue(tmp_path, monkeypatch):
    def fake_check(cfg, pr, name, tracker, ids, mode="normal"):
        ok = name.startswith("Good")
        return {"name": name, "torrent": {"title": name}, "nzbs": [], "plausible": ok, "verdict": "v"}
    monkeypatch.setattr(tracklist, "check", fake_check)
    monkeypatch.setattr(tracklist, "Prowlarr", lambda *a: None)
    monkeypatch.setattr("nzb2seed.pipeline.usenet_indexers", lambda cfg, pr: [9])

    def make(app):
        return tracklist.Queue(app, str(tmp_path / "tracklist.json"), start=False)
    return make


def test_lists_queue_up_and_are_checked_oldest_first(queue):
    app = FakeApp()
    q = queue(app)
    first = q.add(["Good.A", "Bad.B"], "TrackerNine", False, {"pp": "repair"})
    q.step()
    second = q.add(["Good.C"], "TrackerX", True, {})          # pasted while the first is being checked
    lists = {l["id"]: l for l in q.snapshot()}
    assert lists[first]["state"] == "checking" and lists[second]["state"] == "queued"
    while q.step():
        pass
    lists = {l["id"]: l for l in q.snapshot()}
    assert [r["state"] for r in lists[first]["rows"]] == ["done", "done"]
    assert lists[first]["state"] == lists[second]["state"] == "done"
    assert app.built == [("Good.C", "auto")]                  # only the auto list built by itself
    q.build(first, ["Good.A", "Bad.B"])                        # Build ticked: only the plausible one
    assert app.built[-1] == ("Good.A", "auto")                 # always matches the torrent


def test_a_stopped_list_skips_the_rest_and_a_restart_carries_on(queue):
    q = queue(FakeApp())
    a = q.add(["Good.A", "Good.B", "Good.C"], "TrackerNine", False, {})
    q.step()
    q.stop(a)
    b = q.add(["Good.D", "Good.E"], "TrackerNine", False, {})
    q._next()                                                  # mid-check when the restart comes
    again = queue(FakeApp())
    lists = {l["id"]: l for l in again.snapshot()}
    assert [r["state"] for r in lists[a]["rows"]] == ["done", "skipped", "skipped"]
    assert [r["state"] for r in lists[b]["rows"]] == ["waiting", "waiting"]
    while again.step():
        pass
    assert all(r["state"] == "done" for r in {l["id"]: l for l in again.snapshot()}[b]["rows"])
    with pytest.raises(ValueError):
        again.remove(999)


def test_an_auto_list_never_builds_what_is_already_on_the_jobs_list(queue):
    app = FakeApp(on_list={"Good.A"})
    q = queue(app)
    lid = q.add(["Good.A", "Good.B"], "TrackerNine", True, {})
    while q.step():
        pass
    rows = {r["name"]: r for r in q.snapshot()[0]["rows"]}
    assert app.built == [("Good.B", "auto")]
    assert rows["Good.A"]["listed"] == 5 and "job" not in rows["Good.A"] and rows["Good.B"]["job"] == 101
    assert q.snapshot()[0]["id"] == lid


def test_a_name_already_on_the_jobs_list_costs_no_search(queue, monkeypatch):
    searched = []
    monkeypatch.setattr(tracklist, "check", lambda cfg, pr, name, tracker, ids, mode="normal": searched.append(name) or
                        {"name": name, "torrent": {"title": name}, "nzbs": [], "plausible": True, "verdict": "v"})
    q = queue(FakeApp(on_list={"Good.A"}))
    q.add(["Good.A", "Good.B"], "TrackerNine", False, {})
    while q.step():
        pass
    rows = {r["name"]: r for r in q.snapshot()[0]["rows"]}
    assert searched == ["Good.B"]
    assert rows["Good.A"]["listed"] == 5 and rows["Good.A"]["result"]["verdict"] == "not searched"


def test_not_only_video_takes_books_programs_and_games_too():
    page = ("Books\nMP3\nA Long Title Here - An Author Name [2015] [AUDIOBOOK]\n(Anonymous)\n    N/A\n    140.51 MiB\n"
            "Some Program v1.2.345 2024 - GRPA [MULTI]\nUploaderName\nA Game v1.0.1 2024 - GRPB [MULTI]\n"
            "How To Do Things - Some Writer\nA Manual Of Sorts - Another Writer [2008]\n"
            "Film 2010 1080p WEB-DL AAC2.0 H.264-Rel\nRelUploader\nJapanese (JP)\nPortuguese (BR)\n"
            "Seedbox: SomeHost | OtherHost\nTerms Privacy Policy DMCA IRC Chat\nRecently Downloaded, Un-Seeded Torrents\n")
    assert tracklist.parse(page) == ["Film 2010 1080p WEB-DL AAC2.0 H.264-Rel"]          # video only, as before
    assert tracklist.parse(page, any_kind=True) == [
        "A Long Title Here - An Author Name [2015] [AUDIOBOOK]", "Some Program v1.2.345 2024 - GRPA [MULTI]",
        "A Game v1.0.1 2024 - GRPB [MULTI]", "How To Do Things - Some Writer",
        "A Manual Of Sorts - Another Writer [2008]", "Film 2010 1080p WEB-DL AAC2.0 H.264-Rel"]


def test_a_video_name_still_wins_its_row_when_other_kinds_are_taken_too():
    row = "Film 2010 1080p WEB-DL AAC2.0 H.264-Rel\tA Note - Something Else\t1.2 GB"
    assert tracklist.parse(row, any_kind=True) == ["Film 2010 1080p WEB-DL AAC2.0 H.264-Rel"]


def test_the_tab_offers_it_with_its_caveat():
    import pathlib
    src = (pathlib.Path(tracklist.__file__).parent / "web" / "js" / "tabs" / "tracklist.js").read_text(encoding="utf-8")
    assert "(less reliable parsing)" in src and "any_kind" in src


def test_a_scene_game_is_found_by_its_title_and_group():
    """A tracker lists "Title v1.0 2026 - GRP [MULTI]"; the group posts "Title-GRP"."""
    name = "Some Game: Second Part v1.0 2026 - GRPA [MULTI]"
    post = "Some.Game.Second.Part-GRPA"
    assert tracklist.other_facets(name) == {"title": "Some Game Second Part", "key": "somegamesecondpart", "group": "grpa"}
    assert tracklist.other_alike(post, tracklist.other_facets(name))
    assert tracklist.other_alike(post + "-xpost", tracklist.other_facets(name))          # a repost
    for other in ["Some.Game.Second.Part-GRPB",                  # another group
                  "Some.Game.Second.Part.Update.v1.0.2-GRPA",    # an update, not the game
                  "Some.Game.Second.Part.Trainer-GRPA",
                  "Some.Game.Third.Part-GRPA"]:                  # another game
        assert not tracklist.other_alike(other, tracklist.other_facets(name)), other
    r = run({"Some Game Second Part": [rel(post, "usenet", 4 * GB, indexer="UsenetA")]}, name, tracker="")
    assert r["on_usenet"] and post in r["verdict"] and r["verdict"].startswith("same release")


def test_a_game_with_no_group_is_never_matched_by_its_title():
    assert tracklist.other_facets("Some Game 11890051 2018 - NoGroup [MULTI]") is None
    assert tracklist.other_facets("Some Game v1.1 1997 - GRPA [ENG] [PS1]")["title"] == "Some Game"


def test_patches_and_updates_are_left_out_of_the_list():
    text = ("Some Game v1.4.1 2026 - GRPA [MULTI] [PATCH]\nOther Game Patch v1.2 2025 - GRPB [MULTI]\n"
            "Fourth Game v1.0.2 Update 2026 - GRPD [MULTI]\nThird Game v1.0 2026 - GRPC [MULTI]\n")
    assert tracklist.DEFAULT_RULES["skip_words"] == ["patch", "update"]
    assert tracklist.parse(text, any_kind=True) == ["Third Game v1.0 2026 - GRPC [MULTI]"]


def test_rules_are_kept_per_tracker_with_the_default_ones_for_the_rest(tmp_path):
    store = tracklist.RulesStore(str(tmp_path / "rules.json"))
    assert store.rules("TrackerNine") == tracklist.DEFAULT_RULES and not store.own("TrackerNine")
    store.save("TrackerNine", {"uploader_words": "anon, staff ,  ", "skip_words": [], "groupless_max_pct": "10",
                               "must_agree": ["year", "bogus"], "nonsense": 1})
    nine = tracklist.RulesStore(str(tmp_path / "rules.json")).rules("TrackerNine")      # read back from disk
    assert nine["uploader_words"] == ["anon", "staff"] and nine["skip_words"] == []
    assert nine["groupless_max_pct"] == 10.0 and nine["must_agree"] == ["year"] and "nonsense" not in nine
    assert store.rules("TrackerX") == tracklist.DEFAULT_RULES                            # others: the defaults
    store.save("", {"skip_words": ["sample"]})
    assert store.rules("TrackerX")["skip_words"] == ["sample"] and store.rules("TrackerNine")["skip_words"] == []
    store.reset("TrackerNine")
    assert store.rules("TrackerNine")["skip_words"] == ["sample"] and not store.own("TrackerNine")


def test_the_rules_change_how_names_are_read():
    text = "Some Film 2020 1080p WEB-DL DDP5.1 H.264-Rel Staff\nOther Film 2021 1080p WEB-DL H.264-Rel SAMPLE\n"
    assert tracklist.parse(text) == ["Some Film 2020 1080p WEB-DL DDP5.1 H.264-Rel",
                                     "Other Film 2021 1080p WEB-DL H.264-Rel"]
    with tracklist.using({"strip_uploader": False, "skip_words": ["sample"]}):
        assert tracklist.parse(text) == ["Some Film 2020 1080p WEB-DL DDP5.1 H.264-Rel Staff"]


def test_the_rules_change_what_has_to_agree():
    post, name = "Some.Film.2020.1080p.NF.WEB-DL.DDP5.1.H.264-Rel", "Some Film 2020 1080p AMZN WEB-DL DD+ 5.1 H.264-Rel"
    assert not tracklist.alike(post, name)                                   # another service
    with tracklist.using({"must_agree": ["year", "source", "codec", "hdr"]}):
        assert tracklist.alike(post, name)                                   # service left out
    with tracklist.using({"renamed_match": False}):
        r = run({"Some Film 2020": [rel(post, "usenet", 10 * GB)]}, "Some Film 2020 1080p NF WEB-DL DD+ 5.1 H.264-Rel", tracker="")
        assert not r["on_usenet"]                                            # only the exact name will do


# ---------------------------------------------------------------- downloading over BitTorrent

def test_normal_mode_never_downloads_over_bittorrent():
    name = "Film.2020.1080p.BluRay.x264-Rel"
    r = run({name: [rel(name, "torrent", 10 * GB, seeders=5)]}, name)
    assert not r["plausible"] and not r.get("download")


def test_fallback_downloads_what_usenet_does_not_have():
    name = "Film.2020.1080p.BluRay.x264-Rel"
    r = tracklist.check(Config("x.json"), FakeProwlarr({name: [rel(name, "torrent", 10 * GB, seeders=5)]}),
                        name, "TrackerNine", [9], "fallback")
    assert r["download"] and not r["plausible"] and r["torrent"]["title"] == name
    assert "over BitTorrent" in r["verdict"]


def test_fallback_still_builds_what_usenet_has():
    name = "Film.2020.1080p.BluRay.x264-Rel"
    r = tracklist.check(Config("x.json"), FakeProwlarr({name: [rel(name, "usenet", 10.4 * GB, indexer="UsenetA"),
                                                              rel(name, "torrent", 10 * GB, seeders=5)]}),
                        name, "TrackerNine", [9], "fallback")
    assert r["plausible"] and not r.get("download")


def test_fallback_downloads_when_the_usenet_post_is_too_small():
    name = "Film.2020.1080p.BluRay.x264-Rel"
    r = tracklist.check(Config("x.json"), FakeProwlarr({name: [rel(name, "usenet", 5 * GB, indexer="UsenetA"),
                                                              rel(name, "torrent", 10 * GB, seeders=5)]}),
                        name, "TrackerNine", [9], "fallback")
    assert r["download"] and not r["plausible"]


def test_torrent_only_never_asks_usenet_and_skips_unseeded_torrents():
    name = "Film.2020.1080p.BluRay.x264-Rel"
    pr = FakeProwlarr({name: [rel(name, "usenet", 10.4 * GB, indexer="UsenetA"), rel(name, "torrent", 10 * GB, seeders=3)]})
    r = tracklist.check(Config("x.json"), pr, name, "TrackerNine", [], "torrent")
    assert r["download"] and not r["on_usenet"]
    assert all(ids != (9,) for _, ids in pr.asked)                    # Usenet was not asked
    dead = tracklist.check(Config("x.json"), FakeProwlarr({name: [rel(name, "torrent", 10 * GB, seeders=0)]}),
                           name, "TrackerNine", [], "torrent")
    assert not dead.get("download") and "no seeders" in dead["verdict"]


def test_a_list_with_no_tracker_never_downloads_whatever_its_mode(tmp_path):
    q = tracklist.Queue(FakeApp(), str(tmp_path / "t.json"), start=False)
    assert q._find(q.add(["Film.2020.1080p.BluRay.x264-Rel"], "", True, {}, "torrent"))["mode"] == "normal"
    with pytest.raises(ValueError):
        q.add(["X.2020.1080p-Rel"], "TrackerNine", True, {}, "everything")


def test_a_download_list_starts_downloads_not_builds(tmp_path, monkeypatch):
    def fake_check(cfg, pr, name, tracker, ids, mode="normal"):
        assert mode == "torrent" and ids == []
        return {"name": name, "torrent": {"title": name}, "nzbs": [], "plausible": False,
                "download": True, "verdict": "v"}
    monkeypatch.setattr(tracklist, "check", fake_check)
    monkeypatch.setattr(tracklist, "Prowlarr", lambda *a: None)
    monkeypatch.setattr("nzb2seed.pipeline.usenet_indexers", lambda cfg, pr: pytest.fail("Usenet asked"))
    app = FakeApp()
    q = tracklist.Queue(app, str(tmp_path / "t.json"), start=False)
    q.add(["Film.A", "Film.B"], "TrackerNine", True, {}, "torrent")
    while q.step():
        pass
    assert app.built == [] and app.downloaded == [("Film.A", "TrackerNine"), ("Film.B", "TrackerNine")]


def test_the_tab_warns_before_downloading_over_bittorrent():
    import pathlib
    src = (pathlib.Path(tracklist.__file__).parent / "web" / "js" / "tabs" / "tracklist.js").read_text(encoding="utf-8")
    for text in ("Normal safe operation", "Download from torrents if not on usenet",
                 "Skip usenet search and just download from torrent tracker",
                 "WARNING: uses tracker ratio and creates HnR exposure", "don't do this",
                 "OK proceed", "Cancel", "Don't warn me again", 'type="radio"', "Warn me", 'role="switch"'):
        assert text in src, text


def test_a_download_job_reads_as_the_torrent_name():
    assert tracklist.job_key("Download over BitTorrent: Film.2020.1080p-Rel") == tracklist.job_key("Film.2020.1080p-Rel")


def test_each_list_keeps_the_mode_it_was_queued_with(tmp_path, monkeypatch):
    seen = []

    def fake_check(cfg, pr, name, tracker, ids, mode="normal"):
        seen.append((name, mode))
        return {"name": name, "torrent": {"title": name}, "nzbs": [], "plausible": False,
                "download": mode != "normal", "verdict": "v"}
    monkeypatch.setattr(tracklist, "check", fake_check)
    monkeypatch.setattr(tracklist, "Prowlarr", lambda *a: None)
    monkeypatch.setattr("nzb2seed.pipeline.usenet_indexers", lambda cfg, pr: [9])
    app = FakeApp()
    q = tracklist.Queue(app, str(tmp_path / "t.json"), start=False)
    q.add(["Old.A", "Old.B"], "TrackerNine", True, {})                 # queued in normal mode
    q.step()                                                           # one checked...
    q.add(["New.C"], "TrackerNine", True, {}, "fallback")             # ...then a warning mode is chosen
    while q.step():
        pass
    assert seen == [("Old.A", "normal"), ("Old.B", "normal"), ("New.C", "fallback")]
    assert app.downloaded == [("New.C", "TrackerNine")]               # only the new list downloads
    old = tracklist.Queue(app, str(tmp_path / "t.json"), start=False)  # kept across a restart
    assert [l.get("mode") for l in old.snapshot()] == ["fallback", "normal"]


def test_a_list_queued_with_a_warning_option_says_so_in_its_header():
    import pathlib
    src = (pathlib.Path(tracklist.__file__).parent / "web" / "js" / "tabs" / "tracklist.js").read_text(encoding="utf-8")
    assert "<${ListWarning} l=${l} />" in src
    body = src[src.index("function ListWarning"):src.index("function stateOf") if src.index("function stateOf") > src.index("function ListWarning") else None]
    for part in ("${RISK}", "${l.tracker}", "to download over BitTorrent", "result.torrent?.size"):
        assert part in body, part


def none_check(searched):
    def check(cfg, pr, name, tracker, ids, mode="normal"):
        searched.append(name)
        return {"name": name, "torrent": None, "others": [], "nzbs": [], "plausible": False,
                "on_usenet": False, "verdict": "no Usenet post of this release - the tracker was not asked"}
    return check


def test_a_name_another_list_found_nothing_of_lately_is_not_searched_again(queue, monkeypatch):
    searched = []
    monkeypatch.setattr(tracklist, "check", none_check(searched))
    q = queue(FakeApp())
    q.add(["Nothing.2015.1080p.WEB-DL-GRP"], "TrackerNine", False, {})
    while q.step():
        pass
    second = q.add(["Nothing 2015 1080p WEB-DL-GRP", "Other.2015.1080p-GRP"], "TrackerX", False, {})
    while q.step():
        pass
    assert searched == ["Nothing.2015.1080p.WEB-DL-GRP", "Other.2015.1080p-GRP"]
    row = {r["name"]: r for r in q._find(second)["rows"]}["Nothing 2015 1080p WEB-DL-GRP"]
    assert row["result"]["reused"] and "not searched again" in row["result"]["verdict"]
    q.recheck(second, "Nothing 2015 1080p WEB-DL-GRP")             # Search again: asked now
    while q.step():
        pass
    assert searched[-1] == "Nothing 2015 1080p WEB-DL-GRP"
    assert "reused" not in {r["name"]: r for r in q._find(second)["rows"]}["Nothing 2015 1080p WEB-DL-GRP"]["result"]


def test_an_old_search_or_a_list_that_downloads_asks_usenet_again(queue, monkeypatch):
    searched = []
    monkeypatch.setattr(tracklist, "check", none_check(searched))
    q = queue(FakeApp())
    first = q.add(["Nothing.2015.1080p-GRP"], "TrackerNine", False, {})
    while q.step():
        pass
    q.add(["Nothing.2015.1080p-GRP"], "TrackerNine", False, {}, mode="fallback")
    while q.step():
        pass
    assert len(searched) == 2                                     # it would ask the tracker instead
    q._find(first)["rows"][0]["result"]["searched"] -= tracklist.REUSE_NONE_FOR + 1
    for l in q.lists[:1]:
        l["rows"][0]["result"]["searched"] -= tracklist.REUSE_NONE_FOR + 1
    q.add(["Nothing.2015.1080p-GRP"], "TrackerNine", False, {})
    while q.step():
        pass
    assert len(searched) == 3                                     # five days on: searched again
