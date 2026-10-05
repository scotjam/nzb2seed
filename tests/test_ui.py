"""The new interface against the one it replaced.

Each scenario in tests/ui/scenarios.mjs is done on the classic page and on the new app,
in jsdom, against the same made-up server. What was on screen, every request sent (with
its body) and every dialog must be the same - apart from the changes made on purpose,
which are listed here. Needs node, and jsdom installed in tests/ui (npm install there);
skipped otherwise.
"""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from nzb2seed import gui  # noqa: E402

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "nzb2seed", "web")
READY = shutil.which("node") and os.path.isdir(os.path.join(HERE, "node_modules", "jsdom"))
SCENARIOS = ["jobs", "job", "build", "auto", "demand", "settings", "seasons", "assemble"]

# the differences made on purpose: the new page always names the tracker a nearly complete
# build would download the rest from (so you can tell whether it risks a hit-and-run)
INTENDED = [
    # what is found on Usenet, by group and resolution - and not trying groups never found
    (re.compile(r" ?Found on Usenet Nothing yet: every build that ends adds to this\. ?"), " "),
    (re.compile(r" ?\[[x ]\] Don't try groups that are never on Usenet: .*?it can still be tried again\)"), ""),
    # the card under "have never uploaded a byte" says whose disk it counts
    (re.compile(r"of disk held by those that never uploaded"), "of disk they hold"),
    # the torrents found can be narrowed: whole seasons/films, with NZBs found, a language
    (re.compile(r" \[[x ]\] Complete seasons or films only \[[x ]\] Only with NZBs found Language \[[^\]]*\]"), ""),
    # automatic builds only take torrents the tracker posted recently (2 days by default)
    (re.compile(r" Only if the tracker posted it within \(days, 0 = any age\) \[[\d.]*\]"), ""),
    # each tracker has its own limit: the confirmation for several says so...
    (re.compile(r"Each is within what its tracker lets you download, as you set it\."),
     "Only do this if TrackerOne lets you download up to 5% without a hit-and-run."),
    # ...and Settings has a table of trackers, with the global numbers for when to stop
    # looking for more posts (the classic page's single limit and tick list read back as it)
    (re.compile(r"A build missing a little can be handed to qBittorrent.*?What each tracker lets you download tracker % MB "
                r"always add (.*?) Left blank, a tracker lets you download nothing.*?asks you on the Jobs tab\. "
                r"Stop looking for more posts when less than \(%\) is missing \[(\d*)\] …and less than \(MB\) \[\d*\]"),
     lambda m: "A build missing less than this much can be handed to qBittorrent to download the rest - many trackers "
               "let you download a few percent without it counting towards a hit-and-run. For the trackers listed below "
               "it is done without asking; everywhere else you are asked each time. Offer it when less than (%) is "
               f"missing [{m.group(2)}] Always add, for these trackers ["
               + ", ".join(re.findall(r"(\S+)(?: \(API\))? \[[\d.]*\] \[[\d.]*\] \[x\]", m.group(1))) + "]"),
    # the Build tab opens on a summary by release group; the full list links back to it
    (re.compile(r" ?← Summary by release group"), ""),
    # a build outside the limit (or with no seeders) can be added anyway, by override
    (re.compile(r" Override: add to torrent client \([^)]*\)"), ""),
    # nearly complete is now 5% or 200 MB, whichever is less - named as such, with its own setting
    (re.compile(r"Always add nearly complete for (\S+)"), r"Always add >95% for \1"),
    (re.compile(r"(\d)% or \d+ MB"), r"\1%"),
    (re.compile(r" …and less than \(MB\) - whichever is less \[\d*\]"), ""),
    # the trackers always added for are ticked in a list of Prowlarr's, not typed
    (re.compile(r"Always add, for these trackers ((?:\[[x ]\] \S+(?: \(API\))? )*)Ticked: .*? you are asked each time\."),
     lambda m: "Always add, for these trackers [" + ", ".join(
         re.findall(r"\[x\] (\S+)", m.group(1))) + "]"),
    # the Jobs tab tells you to abandon old jobs so their downloads can be cleared
    (re.compile(r" ?Tip: abandoning old jobs allows nzb2seed to clear old downloads relating to those jobs - "
                r"be sure to abandon old jobs on your jobs list after you're done with them\. ?"), " "),
    # removing a job from the list now frees its Usenet downloads (within the hour)
    (re.compile(r"Their Usenet downloads are cleared within the hour, once nothing else needs them "
                r"\(use Remove and delete downloads to clear them now\)\. Nothing in qBittorrent and no file a build "
                r"placed for its torrent is touched\. Automatic torrents stay on the Automatic tab, where they can "
                r"still be tried again - downloading afresh\."),
     "Only the list entries go: no file, download or torrent is touched, and automatic torrents stay on the "
     "Automatic tab where they can still be tried again."),
    (re.compile(r"Add to torrent client \([^)]*\): "), "Add to torrent client: "),
    (re.compile(r" \((?:TrackerOne|TrackerTwo|tracker unknown)\): (\S+%)"), r": \1"),
    # a build already in qBittorrent can be abandoned too (qBittorrent is left alone), so the
    # sample list's one added build now counts among those still offering Abandon
    (re.compile(r"\b3 of them still have Add to torrent client or Abandon"), "2 of them still have Add to torrent client or Abandon"),
    # and has a button to remove jobs together with their Usenet downloads
    (re.compile(r" Remove and delete downloads(?: \(\d+\))?"), ""),
    # and offers to look for a nearly complete build on your other trackers
    (re.compile(r" Look on other trackers"), ""),
    # and running jobs can be cancelled from the bar, several at once
    (re.compile(r" Cancel(?: \(\d+\))?(?= Try again)"), ""),
    # and the NZB pick list can be filtered and sorted
    (re.compile(r" \[\] \[Best match\] \d+ NZBs"), ""),
]


NEW_SETTINGS = {"skip_unposted", "max_age_days", "nearly_limits"}          # settings the new page sends that the classic one had not


def without_new_settings(value):
    """Both pages' output without the settings added since: the classic page carries them
    along in a full save, and the new page also sends them on its own."""
    if isinstance(value, list):
        return [without_new_settings(v) for v in value]
    if isinstance(value, dict):
        return {k: without_new_settings(v) for k, v in value.items() if k not in NEW_SETTINGS}
    return value


def as_classic(value):
    """The new page's output with the intended changes taken back out."""
    if isinstance(value, str):
        for rx, to in INTENDED:
            value = rx.sub(to, value)
        return value
    if isinstance(value, list):
        return [as_classic(v) for v in value]
    if isinstance(value, dict):
        return {k: as_classic(v) for k, v in value.items()}
    return value


@pytest.fixture(scope="module")
def settings_file(tmp_path_factory):
    """What the real server says about a fresh config - the page reads exactly this."""
    d = tmp_path_factory.mktemp("ui")
    cfg = d / "nzb2seed.toml"
    cfg.write_text("")
    app = gui.App(str(cfg))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), gui.make_handler(app, None, {"127.0.0.1"}))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    req = urllib.request.Request(f"http://127.0.0.1:{httpd.server_address[1]}/api/settings")
    req.add_header("Authorization", "Basic " + base64.b64encode(b"admin:nzb2seed").decode())
    data = json.load(urllib.request.urlopen(req))
    httpd.shutdown()
    data["exists"] = True
    data["settings"]["behaviour"]["nearly_auto_trackers"] = ["TrackerThree"]
    # each tracker has its own limit; the test trackers allow what the old global limit did
    data["settings"]["behaviour"]["nearly_limits"] = [{"tracker": t, "percent": 5, "mb": 200}
                                                      for t in ("TrackerOne", "TrackerTwo", "TrackerThree")]
    path = d / "settings.json"
    path.write_text(json.dumps(data))
    return str(path)


def run(ui, scenario, settings_file):
    done = subprocess.run(["node", "run.mjs", ui, scenario, WEB, settings_file], cwd=HERE,
                          capture_output=True, text=True, timeout=180, encoding="utf-8")
    assert done.returncode == 0 and done.stdout, f"{ui} {scenario} crashed:\n{done.stderr[-2000:]}"
    out = json.loads(done.stdout)
    assert not out["failure"], f"{ui} {scenario}: {out['failure']}"
    assert not out["errors"], f"{ui} {scenario} threw: {out['errors']}"
    return out


@pytest.mark.skipif(not READY, reason="node and jsdom (npm install in tests/ui) are needed")
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_the_new_page_does_what_the_classic_one_did(scenario, settings_file):
    classic = without_new_settings(run("classic", scenario, settings_file))
    new = without_new_settings(as_classic(run("new", scenario, settings_file)))
    assert new["seen"] == classic["seen"]
    assert new["confirms"] == classic["confirms"]


@pytest.mark.skipif(not READY, reason="node and jsdom (npm install in tests/ui) are needed")
def test_the_intended_change_is_really_there(settings_file):
    """The tracker is named on Add to torrent client - the reason the pages may differ."""
    new = run("new", "jobs", settings_file)
    assert "Add to torrent client (TrackerOne): 99.9999%" in new["seen"]["nearly"]


@pytest.mark.skipif(not READY, reason="node and jsdom (npm install in tests/ui) are needed")
def test_the_list_follows_the_server_without_closing_the_filter(settings_file):
    """What started the rewrite: the job filter closed while you read it, because every
    refresh built the whole page again."""
    seen = run("new", "live", settings_file)["seen"]
    assert seen["newJob"] and seen["rowsAfter"] == seen["rowsBefore"] + 1
    assert seen["sameFilter"] and seen["focused"]


@pytest.mark.skipif(not READY, reason="node and jsdom (npm install in tests/ui) are needed")
def test_on_a_phone_the_list_and_the_job_are_two_screens(settings_file):
    seen = run("new", "phone", settings_file)["seen"]
    assert seen == {"start": False, "opened": True, "back": False, "leftAndCameBack": False}


@pytest.mark.skipif(not READY, reason="node and jsdom (npm install in tests/ui) are needed")
def test_a_nearly_complete_build_can_be_built_from_another_tracker(settings_file):
    seen = run("new", "others", settings_file)["seen"]         # pre-approved, but nobody seeding
    assert "TrackerThree pre-approved · 0 seeders" in seen["listed"] and "same size" in seen["listed"]
    assert "different size - not the same files" in seen["listed"]
    (sent,) = seen["sent"]
    assert sent["body"]["torrent"]["indexer"] == "TrackerThree" and sent["body"]["nzbs"] == []
    assert "approved" not in sent["body"]["torrent"]            # only the release goes to the build


@pytest.mark.skipif(not READY, reason="node and jsdom (npm install in tests/ui) are needed")
def test_running_builds_are_cancelled_from_the_bar(settings_file):
    seen = run("new", "cancel", settings_file)["seen"]
    assert seen["sent"] == ["/api/jobs/9/cancel"]
    assert "still running - cancel them first" in seen["why"]


@pytest.mark.skipif(not READY, reason="node and jsdom (npm install in tests/ui) are needed")
def test_the_nzb_pick_list_can_be_filtered_and_sorted(settings_file):
    seen = run("new", "askfilter", settings_file)["seen"]
    assert seen["sorted"] == ["Show.S03E02.1080p.WEB-GRPC-xpost", "Show.S03E02.1080p.WEB-GRPC"]   # smallest first
    assert seen["filtered"] == ["Show.S03E02.1080p.WEB-GRPC-xpost"]
    (sent,) = seen["sent"]
    assert sent["body"] == {"choice": 1}          # the second NZB of the question, as it was asked


@pytest.mark.skipif(not READY, reason="node and jsdom (npm install in tests/ui) are needed")
def test_settings_save_whatever_numbers_are_typed(settings_file):
    """A browser silently refuses to submit a form holding a number off its field's step -
    so the Settings form checks numbers itself and never lets the browser block saving."""
    seen = run("new", "oddnumbers", settings_file)["seen"]
    assert seen == {"saved": 1, "space": 7.3, "mb": 205}


@pytest.mark.skipif(not READY, reason="node and jsdom (npm install in tests/ui) are needed")
def test_a_build_gets_add_or_override_never_both_and_each_asks(settings_file):
    seen = run("new", "override", settings_file)["seen"]
    add = [b for b in seen["nearly"] if "torrent client" in b]
    assert add == ["Add to torrent client (TrackerOne): 99.9999%"]                  # within the limit
    over = [b for b in seen["noSeeders"] if "torrent client" in b]
    assert over == ["Override: add to torrent client (TrackerOne)"]                 # no seeders reported
    assert seen["asked"].startswith("Override: add this to qBittorrent anyway?") and "no seeders" in seen["asked"]
    assert seen["sent"] == [{"id": 3, "override": True}]


@pytest.mark.skipif(not READY, reason="node and jsdom (npm install in tests/ui) are needed")
def test_the_torrents_can_be_filtered_by_release_group(settings_file):
    seen = run("new", "groups", settings_file)["seen"]
    assert seen["chips"][0] == "All groups" and any(c.startswith("GRPZ") for c in seen["chips"])
    assert seen["only"] == ["Film.2020.2160p.BluRay-GRPZ"] and seen["after"] == seen["before"] == 2


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_release_groups_are_ordered_by_how_many_seasons_they_cover():
    script = """
import { groupsOf, seasonsOf } from "./nzb2seed/web/js/components/groups.js";
const t = (title) => ({ title });
const got = groupsOf([t("Show S01 1080p WEB-GRPA"), t("Show S02 1080p WEB-GRPA"), t("Show S01 1080p WEB-GRPB"),
  t("Show S01 1080p WEB-GRPB"), t("Show S01 1080p WEB-GRPB"), t("Show (2022) S01-S03 (1080p WEB English - GRPC)")]);
console.log(JSON.stringify({ order: got.map(g => [g.name, g.seasons.size, g.count]),
  ep: [...seasonsOf("Show.S02E05.1080p-GRP")], range: [...seasonsOf("Show S01-S03 1080p")] }));
"""
    root = os.path.dirname(os.path.dirname(HERE))
    done = subprocess.run(["node", "--input-type=module", "-e", script], cwd=root, capture_output=True, text=True)
    out = json.loads(done.stdout)
    assert out["order"] == [["GRPC", 3, 1], ["GRPA", 2, 2], ["GRPB", 1, 3]]   # most seasons first
    assert out["ep"] == [2] and out["range"] == [1, 2, 3]


@pytest.mark.skipif(not READY, reason="node and jsdom (npm install in tests/ui) are needed")
def test_the_torrents_found_are_summarised_by_release_group(settings_file):
    seen = run("new", "digest", settings_file)["seen"]
    summary = seen["summary"]
    # the group with NZBs found on Usenet comes first, with its resolution and size
    assert summary.index("GRPA") < summary.index("GRPZ")
    assert "1080p" in summary and "2160p" in summary and "NZBs found" in summary and "20.00 GB" in summary
    assert "Film.2020.1080p.BluRay-GRPA" in seen["bar"]          # ticking it there picks it to build
    assert seen["full"] == 2                                       # and the full list is one tap away
    # each group folds away under its arrow: down when open, right when folded
    f = seen["folding"]
    assert (f["arrow"], f["foldedArrow"], f["expanded"]) == ("▾", "▸", "false")
    assert f["foldedItems"] == f["items"] - 1 and f["openedAgain"] == f["items"]
    # hovering over an entry shows the release's full name
    assert sorted(seen["hover"]) == ["Film.2020.1080p.BluRay-GRPA", "Film.2020.2160p.BluRay-GRPZ"]


@pytest.mark.skipif(not READY, reason="node and jsdom (npm install in tests/ui) are needed")
def test_the_always_added_trackers_are_ticked_in_prowlarrs_list(settings_file):
    seen = run("new", "trackers", settings_file)["seen"]
    # each of Prowlarr's trackers with its own limit; the saved "TrackerThree" is Prowlarr's
    # "TrackerThree (API)" (ticked, not listed twice); TrackerTwo is not in Prowlarr's list
    assert seen["listed"] == ["TrackerFour -% -MB [ ]", "TrackerOne 5% 200MB [ ]",
                              "TrackerThree (API) 5% 200MB [x]", "TrackerTwo 5% 200MB [ ]"]
    assert seen["after"] == ["TrackerFour 2% -MB [x]", "TrackerOne -% -MB [ ]",
                             "TrackerThree (API) 5% 200MB [ ]", "TrackerTwo 5% 200MB [ ]"]
    assert seen["saved"] == ["TrackerFour"]
    # TrackerOne cleared: blank, so it lets you download nothing (and is no longer listed)
    assert seen["limits"] == ["TrackerFour 2% 0MB", "TrackerThree 5% 200MB", "TrackerTwo 5% 200MB"]


@pytest.mark.skipif(not READY, reason="node and jsdom (npm install in tests/ui) are needed")
def test_a_pre_approved_tracker_with_a_seeder_is_built_from_at_once(settings_file):
    seen = run("new", "othersAuto", settings_file)["seen"]
    (sent,) = seen["sent"]                                       # no list, no choice
    assert sent["body"]["torrent"]["indexer"] == "TrackerThree" and not seen["listed"]
    assert "Building it from TrackerThree - pre-approved, 4 seeders" in seen["shown"]


@pytest.mark.skipif(not READY, reason="node and jsdom (npm install in tests/ui) are needed")
def test_a_torrents_name_opens_its_tracker_page_and_only_the_box_ticks(settings_file):
    seen = run("new", "pagelinks", settings_file)["seen"]
    assert seen["digestHref"] == "https://tracker.example/details/1?id=5"     # the passkey left out
    assert seen["linkChanged"] is False and seen["boxChanged"] is True
    assert seen["rowHref"] == "https://tracker.example/details/1?id=5"
    assert seen["plain"] >= 1                                                   # no page: plain text


@pytest.mark.skipif(not READY, reason="node and jsdom (npm install in tests/ui) are needed")
def test_the_demand_tab_shows_nzb2seeds_builds_alongside_all_torrents(settings_file):
    seen = run("new", "demandAuto", settings_file)["seen"]
    assert "nzb2seed's manual builds 40 older than 14 days (of 44)" in seen["page"] and "1.22x" in seen["page"]
    assert "nzb2seed's automatic builds" in seen["page"] and "1.50x" in seen["page"]
    assert "2 cross-seeds of their files" in seen["page"]
    assert seen["rows"] == [["GRPA", "30", "1.80x", "5%", "-", "7 · 1.90x"],
                            ["GRPB", "12", "0.20x", "50%", "12 · 0.20x", "-"]]


@pytest.mark.skipif(not READY, reason="node and jsdom (npm install in tests/ui) are needed")
def test_a_skipped_automatic_build_gets_a_grey_dot(settings_file):
    seen = run("new", "skippedDot", settings_file)["seen"]
    assert seen["skipped"] == "dot skipped" and seen["done"] == "dot done"


@pytest.mark.skipif(not READY, reason="node and jsdom (npm install in tests/ui) are needed")
def test_the_torrents_shown_are_whole_with_nzbs_found_and_can_be_narrowed_by_language(settings_file):
    seen = run("new", "tfilters", settings_file)["seen"]
    # whole films with an NZB of the same group and resolution - not the 2160p one (no NZB),
    # not the single episode
    assert seen["first"] == ["Film.2020.1080p.BluRay-GRPA", "Film.2020.FRENCH.1080p.BluRay-GRPA"]
    assert seen["head"] == "2 of 4 shown"
    assert seen["tip"].startswith("2 more torrents are not shown.") and "Only with NZBs found" in seen["tip"]
    assert seen["french"] == ["Film.2020.FRENCH.1080p.BluRay-GRPA"]
    assert seen["none"] == ["Film.2020.1080p.BluRay-GRPA"]
    assert len(seen["all"]) == 4 and seen["tipAfter"] == ""


@pytest.mark.skipif(not READY, reason="node and jsdom (npm install in tests/ui) are needed")
def test_try_whole_posts_never_says_nothing_is_missing(settings_file):
    seen = run("new", "wholeSmall", settings_file)["seen"]
    assert seen["button"] == "Try whole posts (1 part, 1 KB missing)"        # not "0.0 MB"


@pytest.mark.skipif(not READY, reason="node and jsdom (npm install in tests/ui) are needed")
def test_a_copy_without_a_trackers_small_nfo_counts_as_the_same_video(settings_file):
    assert run("new", "othersNear", settings_file)["seen"]["sent"] == ["TrackerThree"]


@pytest.mark.skipif(not READY, reason="node and jsdom (npm install in tests/ui) are needed")
def test_back_from_a_job_goes_to_the_jobs_list(settings_file):
    seen = run("new", "backButton", settings_file)["seen"]
    assert seen["opened"] == [True, True]
    assert seen["afterBack"] == ["#jobs", False]
    assert seen["afterTwo"] == ["#jobs", False]          # one job to another: still one Back to the list


@pytest.mark.skipif(not READY, reason="node and jsdom (npm install in tests/ui) are needed")
def test_a_tracker_without_a_limit_is_asked_about_on_the_job(settings_file):
    seen = run("new", "limitAsk", settings_file)["seen"]
    assert seen["asks"] and not any(b.startswith("Add to torrent client") for b in seen["before"])
    assert any(b.startswith("Override") for b in seen["before"])           # still possible, knowingly
    assert seen["sent"] == [{"tracker": "TrackerNine", "percent": 1, "mb": 0}]
    assert any(b.startswith("Add to torrent client (TrackerNine)") for b in seen["after"]) and not seen["asksAfter"]


@pytest.mark.skipif(not READY, reason="node and jsdom (npm install in tests/ui) are needed")
def test_a_build_in_qbittorrent_is_orange_then_green(settings_file):
    seen = run("new", "clientDots", settings_file)["seen"]
    assert seen["downloading"] == "dot client" and seen["complete"] == "dot done"
    assert seen["says"] == "complete in qBittorrent - seeding"
