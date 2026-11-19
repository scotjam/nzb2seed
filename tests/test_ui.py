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
    # Settings: how long downloads of failed builds are kept, in hours
    (re.compile(r"Delete downloads of failed or dropped builds after \(hours\) (?:\[[^\]]*\] )?"), ""),
    # Settings: cross-seed's address, for cross-seeding a release built for one tracker on another
    (re.compile(r"cross-seed Optional\. With it, a release nzb2seed built.*?cross-seed prints it with: cross-seed api-key ?"), ""),
    # Settings: an Advanced section, with the tick that switches mend off
    (re.compile(r"Advanced (?:\[[x ]\] )?Disable mend \(.*?ticked always add\) ?"), ""),
    # a Pause / Resume button above the jobs list
    # (everything nzb2seed does by itself holds: builds, the list queue, automatic builds,
    # the Library, mend and the clean-ups)
    (re.compile(r"(?:❚❚ Pause|▶ Resume) (?:Pause holds everything nzb2seed does by itself:.*?Torrents keep seeding\.|"
                r"Paused[^:]*: nzb2seed does nothing by itself.*?Seeding carries on\.) ?"), ""),
    # the Jobs list is in parts - building now, queued in the order they start, ended - and
    # marks automatic builds in a column of their own (their names without "auto:")
    (re.compile(r"(?:Active|Build queue|Automatic queue|Finished) \(\d+\)"
                r"(?: next to start at the top(?: - newest torrent first)?)? ?"), ""),
    (re.compile(r"(\[[x ]\]) Auto (\S+)"), r"\1 auto: \2"),
    # Build and Demand open with a heading and what the tab is for
    (re.compile(r"^Build Find a torrent and matching nzbs so Usenet downloads can be used to build a torrent seed\. "), ""),
    (re.compile(r"^Demand Statistics measured from your torrents in qBittorrent\..*? are included in the stats\. "), ""),
    (re.compile(r"(What your seeding returns \d+ torrents older than \d+ days) "),
     r"\1 Measured from your own torrents in qBittorrent - nothing is sent anywhere. The number is upload per GB "
     "stored : 1.00 means a torrent has given back as much as it takes up. Only torrents older than the settling "
     "time count, because a new one has not had its chance yet. "),
    # Seasons is a part of Assemble: the tab's heading and the choice between the two parts
    (re.compile(r"^Assemble Arrange files into a torrent seed.*? with a report\. Start from (?:\([x ]\) )?A \.torrent and the files "
                r"you have (?:\([x ]\) )?A season or whole series, found on Usenet "), ""),
    # a job says when it started - on its page, and under it in the list
    (re.compile(r" ?\b[Ss]tarted (?:\d{1,2} [A-Z][a-z]{2}|[A-Z][a-z]{2} \d{1,2}),? \d{1,2}:\d{2}(?:\s?[AP]M)?"), ""),
    # the Automatic tab's tries: one list of minutes in place of a window and two gaps
    (re.compile(r"Look for the Usenet post at \(minutes after it arrived\) \[0, 2, 10, 20, 60\]"),
     "Keep trying for (hours) [0.23] First retry after (minutes) [2] …backing off to at most (minutes) [2]"),
    # the Seasons tab says it works from Usenet alone
    (re.compile(r"Grab a season, or the whole series, from Usenet"), "Grab a season, or the whole series"),
    (re.compile(r"folder next to the season, without searching for or downloading any torrent files\."), "folder next to the season."),
    # removal can be chosen per job, which the retention text mentions
    (re.compile(r"Only automatic builds are removed - your own never are - and an automatic build can be taken out of these rules on its job's page\. "), ""),
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
    # (each tracker's % and MB are drop-downs - Not specified, None allowed, Custom, Full - with
    # a line under each saying what it allows)
    (re.compile(r"A build missing a little can be handed to qBittorrent.*?What each tracker lets you download tracker % MB "
                r"always add (.*?) With % and MB both Not specified, a tracker lets you download nothing.*?"
                r"before relying on them\. "
                r"Stop looking for more posts when less than \(%\) is missing \[(\d*)\] …and less than \(MB\) \[\d*\]"),
     lambda m: "A build missing less than this much can be handed to qBittorrent to download the rest - many trackers "
               "let you download a few percent without it counting towards a hit-and-run. For the trackers listed below "
               "it is done without asking; everywhere else you are asked each time. Offer it when less than (%) is "
               f"missing [{m.group(2)}] Always add, for these trackers ["
               + ", ".join(re.findall(r"(\S+)(?: \(API\))?(?: \[[^\]]*\]){2,4} \[x\]", m.group(1))) + "]"),
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
    # the Jobs tab says what each Clear from list does with the downloads
    (re.compile(r" ?Tip: a job's Usenet downloads are kept while it is on the list\. Clear from list, delete files "
                r"removes them; Clear from list, keep files leaves them in your Usenet downloads folder\. ?"), " "),
    # ...and its "keep files" confirmation says nothing is deleted, and what may be left behind
    (re.compile(r"^Clear (\d+) jobs? from the list and keep their files\?\n\nNothing is deleted: neither the torrents' "
                r"files nor the jobs' Usenet downloads\. Temporary files may stay in your Usenet downloads folder - to "
                r"delete those too, use Clear from list, delete files\.\n\nNothing in qBittorrent is changed\. Automatic "
                r"torrents stay on the Automatic tab, where they can be tried again\."),
     r"Remove \1 job(s) from the list?\n\nOnly the list entries go: no file, download or torrent is touched, and "
     r"automatic torrents stay on the Automatic tab where they can still be tried again."),
    # (with no Abandon on a job any more, only Add to torrent client is counted - the sample has
    # one such job, where the classic page counted two with Add or Abandon)
    (re.compile(r"\n\n(?:One of them can|\d+ of them can) still be added to the torrent client\. Once cleared, "
                r"that button is gone, but the files are kept\."),
     "\n\n2 of them still have Add to torrent client or Abandon on them - those buttons go with them, and anything "
     "such a build left behind stays where it is."),
    # the Jobs bar: Select: and Action:, actions named by what they do
    (re.compile(r"Select: All Failed Nearly complete Running / queued Completed Automatic Manual None (?:\[Tracker.\] )?"
                r"Action: Retry selected Cancel selected \(kept on list\) Add to torrent client \(download the rest\) "
                r"Clear from list, keep files Clear from list, delete files"),
     "Tick all that failed Try again Add to torrent client Abandon Remove from list Clear auto jobs from queue"),
    (re.compile(r"Add to torrent client \([^)]*\): "), "Add to torrent client: "),
    (re.compile(r" \((?:TrackerOne|TrackerTwo|tracker unknown)\): (\S+%)"), r": \1"),
    # and has a button to remove jobs together with their Usenet downloads
    (re.compile(r" Remove and delete downloads(?: \(\d+\))?"), ""),
    # and offers to look for a nearly complete build on your other trackers
    (re.compile(r" Look on other trackers"), ""),
    # and running jobs can be cancelled from the bar, several at once
    (re.compile(r" Cancel(?: \(\d+\))?(?= Try again)"), ""),
    # and the NZB pick list can be filtered and sorted
    (re.compile(r" \[\] \[Best match\] \d+ NZBs"), ""),
    # each job's own buttons are the Jobs bar's: Retry, and Clear from list (keep or delete
    # files) in place of Try again and Abandon
    # (Abandon was only offered on a build that left files behind: the nearly complete ones)
    (re.compile(r"(for TrackerOne|over BitTorrent\.) Retry Clear from list, keep files Clear from list, delete files"),
     r"\1 Try again Abandon"),
    (re.compile(r"(?<!\w)Retry Clear from list, keep files Clear from list, delete files"), "Try again"),
    (re.compile(r" ?Clear from list, keep files Clear from list, delete files"), ""),
    # 10% of a tracker's limit is kept free: the sample's 190.8 MB short of a 200 MB limit is no
    # longer nearly complete, so it has Override (above) in place of the note that it could
    # not be offered for want of seeders - and Abandon is gone
    (re.compile(r"(Film\.2017\.1080p\.BluRay-GRPF the NZB download\(s\) do not contain every file) Try again"),
     r"\1 Not offered for the torrent client: Prowlarr reported no seeders for it on TrackerOne, so the missing "
     r"2.0001% (190.8 MB) could never come over BitTorrent. Try again Abandon"),
    # repairs can take what still fails from a permitted tracker, if that is smaller
    (re.compile(r" \[[x ]\] Prefer small downloads from permitted trackers over large Usenet downloads \(off: .*?it comes over BitTorrent\)"), ""),
    # the Build tab: which trackers to search, and a tracker filter on what was found
    (re.compile(r" Trackers: (?:all|[^\[]*? chosen|\S+)(?= Upload \.torrent)"), ""),
    (re.compile(r" Tracker \[All \(\d+\)\]"), ""),
    # the Automatic tab's description: what autobrr connects, and when the rest comes over BitTorrent
    (re.compile(r"Connect to Autobrr to automatically download from usenet as torrents are posted\. (autobrr drops torrents "
                r"from the filters you pick into a folder; nzb2seed builds each one from Usenet by itself) and can start them "
                r"seeding if they check to 100%\..*? then it may be completed through torrent downloading\."),
     r"\1 - it waits for the post, never asks, never downloads over BitTorrent, and starts seeding only at a 100.0% recheck."),
]


NEW_SETTINGS = {"skip_unposted", "max_age_days", "nearly_limits", "retry_at", "cross_seed"}
OLD_SETTINGS = {"wait_hours", "retry_minutes", "retry_first_minutes"}     # replaced by retry_at          # settings the new page sends that the classic one had not


def without_new_settings(value):
    """Both pages' output without the settings added since: the classic page carries them
    along in a full save, and the new page also sends them on its own."""
    if isinstance(value, list):
        return [without_new_settings(v) for v in value]
    if isinstance(value, dict):
        return {k: without_new_settings(v) for k, v in value.items() if k not in NEW_SETTINGS | OLD_SETTINGS}
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
def test_a_question_opens_with_the_best_match_picked(settings_file):
    """What looks ticked is what the button downloads: the best match, without a tap."""
    seen = run("new", "askdefault", settings_file)["seen"]
    assert seen["ticked"][0] and not any(seen["ticked"][1:])
    assert seen["off"] is False
    (sent,) = seen["sent"]
    assert sent["body"] == {"choice": 0}


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
def test_a_queued_build_can_be_moved_to_the_top_or_started_now(settings_file):
    seen = run("new", "queueJump", settings_file)["seen"]
    assert seen["first"] == ["Start now"]                    # already next: nothing to move
    assert seen["second"] == ["Move to top", "Start now"]
    assert seen["posted"] == ["/api/jobs/21/top", "/api/jobs/20/force"]


@pytest.mark.skipif(not READY, reason="node and jsdom (npm install in tests/ui) are needed")
def test_a_greyed_out_build_button_says_why_under_it(settings_file):
    seen = run("new", "builtAlready", settings_file)["seen"]
    assert seen["disabled"] is True
    assert seen["why"] == "Already built (job 11 in Jobs). Remove that job to build it again."
    assert seen["described"] == "buildwhy"          # screen readers hear the reason with the button


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
    # a tracker never set: Not specified in both
    assert seen["listed"] == ["TrackerFour unset% unsetMB [ ]", "TrackerOne 5% 200MB [ ]",
                              "TrackerThree (API) 5% 200MB [x]", "TrackerTwo 5% 200MB [ ]"]
    assert seen["after"] == ["TrackerFour 2% unsetMB [x]", "TrackerOne unset% unsetMB [ ]",
                             "TrackerThree (API) 5% 200MB [ ]", "TrackerTwo 5% 200MB [ ]"]
    assert seen["saved"] == ["TrackerFour"]
    # TrackerOne: neither specified, so it lets you download nothing (and is no longer listed)
    assert seen["limits"] == ["TrackerFour 2% -MB", "TrackerThree 5% 200MB", "TrackerTwo 5% 200MB"]


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
    assert seen["modes"] == ["unset", "unset"]
    assert seen["asks"] and not any(b.startswith("Add to torrent client") for b in seen["before"])
    assert any(b.startswith("Override") for b in seen["before"])           # still possible, knowingly
    assert seen["sent"] == [{"tracker": "TrackerNine", "percent": 1, "mb": None}]   # blank MB: not set
    assert any(b.startswith("Add to torrent client (TrackerNine)") for b in seen["after"]) and not seen["asksAfter"]


@pytest.mark.skipif(not READY, reason="node and jsdom (npm install in tests/ui) are needed")
def test_a_limit_that_would_not_behave_as_it_reads_is_warned_about_and_not_saved(settings_file):
    seen = run("new", "limitWarn", settings_file)["seen"]
    assert seen["first"] == "" and "cannot be blank" in seen["blank"] and seen["sentBlank"] == 0
    assert "percentage would be ignored" in seen["warning"] and "Not specified for MB" in seen["warning"]
    assert seen["sent"] == 0
    assert "below 100" in seen["hundred"]


@pytest.mark.skipif(not READY, reason="node and jsdom (npm install in tests/ui) are needed")
def test_settings_are_not_saved_while_a_trackers_custom_limit_is_blank(settings_file):
    seen = run("new", "trackersBlank", settings_file)["seen"]
    assert seen["untouched"] == 0                                    # never set: Not specified in both
    assert seen["info"]                                              # ...and each row says what it does
    assert len(seen["warn"]) == 1 and "cannot be blank" in seen["warn"][0]     # TrackerFour on Custom
    assert seen["sent"] == 0


@pytest.mark.skipif(not READY, reason="node and jsdom (npm install in tests/ui) are needed")
def test_a_build_in_qbittorrent_is_orange_then_green(settings_file):
    seen = run("new", "clientDots", settings_file)["seen"]
    assert seen["downloading"] == "dot client" and seen["complete"] == "dot done"
    assert seen["says"] == "complete in qBittorrent - seeding"


@pytest.mark.skipif(not READY, reason="node and jsdom (npm install in tests/ui) are needed")
def test_a_search_with_no_nzbs_says_why_and_offers_the_torrents_name(settings_file):
    seen = run("new", "noNzbs", settings_file)["seen"]
    assert "No NZBs at all were found for “Flim 2020”" in seen["tip"] and "Film 2020" in seen["tip"]
    assert seen["searched"] == ["Film 2020"]
    assert "No NZBs at all" not in seen["after"]


@pytest.mark.skipif(not READY, reason="node and jsdom (npm install in tests/ui) are needed")
def test_a_resolution_folds_away_under_its_group(settings_file):
    seen = run("new", "resFold", settings_file)["seen"]
    assert seen["arrow"] == "▾" and seen["items"] >= 1
    assert seen["folded"] == "▸" and seen["foldedItems"] == 0 and seen["says"].endswith("torrent" + ("s" if seen["items"] > 1 else ""))
    assert seen["groupsStillOpen"] >= 1 and seen["again"] == seen["items"]


@pytest.mark.skipif(not READY, reason="node and jsdom (npm install in tests/ui) are needed")
def test_select_by_tracker_ticks_jobs_from_it_and_via_it(settings_file):
    seen = run("new", "byTracker", settings_file)["seen"]
    assert "TrackerSix" in seen["options"] and "TrackerOne" in seen["options"]
    assert seen["ticked"] == ["Show.S04.1080p.BluRay-GRPV", "Show.S05.1080p.BluRay-GRPV via TrackerSix"]
    assert seen["label"] == "Add to torrent client (download the rest) (1)"   # S04: TrackerSix has no limit set
    assert seen["sent"] == [["/api/jobs/add_via", {"id": 96, "guid": "six1"}]]


@pytest.mark.skipif(not READY, reason="node and jsdom (npm install in tests/ui) are needed")
def test_what_was_found_on_other_trackers_by_itself_shows_on_the_job(settings_file):
    seen = run("new", "savedOthers", settings_file)["seen"]
    assert "1 other tracker has it:" in seen["text"] and "TrackerSix" in seen["text"]
    assert "not pre-approved" in seen["text"] and "different size - not the same files" in seen["text"]
    assert "Build from this tracker" in seen["buttons"] and "Look on other trackers" not in seen["buttons"]


@pytest.mark.skipif(not READY, reason="node and jsdom (npm install in tests/ui) are needed")
def test_a_job_built_again_from_another_tracker_is_replaced(settings_file):
    seen = run("new", "replacedJob", settings_file)["seen"]
    assert seen["says"] == "replaced by job 99: being built from TrackerTwo"
    assert seen["dot"] == "dot skipped"
    assert seen["buttons"] == ["Clear from list, keep files", "Clear from list, delete files"]


@pytest.mark.skipif(not READY, reason="node and jsdom (npm install in tests/ui) are needed")
def test_the_jobs_list_can_be_filtered_by_keyword(settings_file):
    seen = run("new", "jobSearch", settings_file)["seen"]
    assert seen["narrowed"] and all("Show.S02" in t for t in seen["narrowed"])
    assert len(seen["narrowed"]) < seen["before"] and seen["after"] == seen["before"]
