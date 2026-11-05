"""The page has to load.

The page is a set of JavaScript modules (Preact + htm, web/js). A single bad character in
one of them stops every tab working, and nothing else in the test suite would notice - the
Python side is perfectly happy. So every module is run through node when node is there,
and the pieces that have to agree with each other (tabs, views, the job screens) are
checked here.
"""
import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "nzb2seed", "web")


def read(rel: str) -> str:
    with open(os.path.join(WEB, *rel.split("/")), encoding="utf-8") as fh:
        return fh.read()


def modules() -> list[str]:
    out = []
    for root, _, names in os.walk(os.path.join(WEB, "js")):
        out += [os.path.relpath(os.path.join(root, n), WEB).replace(os.sep, "/") for n in names if n.endswith(".js")]
    return sorted(out)


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_every_module_parses(tmp_path):
    for rel in modules():
        f = tmp_path / (rel.replace("/", "_") + ".mjs")          # .mjs: parsed as a module
        f.write_text(read(rel), encoding="utf-8")
        done = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        assert done.returncode == 0, f"{rel} does not parse:\n{done.stderr}"


def test_every_import_points_at_a_file():
    for rel in modules():
        for target in re.findall(r'from\s+"(\.[^"]+)"', read(rel)):
            path = os.path.normpath(os.path.join(WEB, os.path.dirname(rel), target))
            assert os.path.isfile(path), f"{rel} imports {target}, which is not there"


def test_the_page_loads_the_app():
    page = read("index.html")
    assert '<div id="app">' in page and 'src="js/app.js"' in page


def test_the_tabs_and_their_views_line_up():
    """A tab in the list with no view (or a name) shows an empty page or no link."""
    tabs = re.findall(r'"([a-z]+)"', re.search(r"export const TABS = \[([^\]]+)\]", read("js/store.js")).group(1))
    app = read("js/app.js")
    views = dict(re.findall(r"(\w+): (\w+Tab)", re.search(r"const VIEWS = \{(.*?)\};", app, re.S).group(1)))
    names = dict(re.findall(r'(\w+): "([^"]+)"', re.search(r"const NAMES = \{(.*?)\};", app, re.S).group(1)))
    assert set(tabs) == set(views) == set(names)
    for tab, view in views.items():
        assert re.search(r"import \{ " + view + r" \} from \"\./tabs/[\w-]+\.js\";", app), f"{view} is not imported"


def test_the_job_list_and_the_job_are_two_screens_on_a_phone():
    """Side by side they do not fit, and stacked the job sits far below the list - so a
    phone shows one at a time, with a way back."""
    css, jobs = read("css/app.css"), read("js/tabs/jobs.js")
    assert "backToJobs" in jobs and 'class="btn jback"' in jobs          # the way back exists
    assert ".jobs.showing .jobside { display: none; }" in css            # the list and its bar
    assert ".jobs:not(.showing) .job { display: none; }" in css
    assert ".jback { display: none;" in css                              # out of the way on a desktop
    assert ".jobs.showing .jback { display: inline-block; }" in css


def test_opening_a_job_shows_its_screen_and_leaving_goes_back():
    store, app = read("js/store.js"), read("js/app.js")
    body = re.search(r"export function openJob\([^)]*\) \{(.*?)\n\}", store, re.S).group(1)
    assert "jobScreen.set(true)" in body
    assert 'if (now !== "jobs") jobScreen.set(false);' in app           # leaving the tab: the list


def test_jobs_can_be_ticked_and_acted_on_together():
    jobs = read("js/tabs/jobs.js")
    assert 'id="jobbar"' in jobs                                         # the actions sit above the list
    for label in ("Retry selected", "Add to torrent client", "Clear from list, keep files",
                  "Clear from list, delete files"):
        assert label in jobs


def test_acting_on_a_job_stays_on_the_list():
    """Pressing Try again should not whisk you off to the job it started."""
    src = read("js/tabs/jobs-actions.js")
    for name in ("retryJob", "addToClient", "removeMany", "removeAndDelete", "cancelMany"):
        body = re.search(r"export async function " + name + r"\([^)]*\) \{(.*?)\n\}", src, re.S).group(1)
        assert "openJob(" not in body, f"{name} opens the job it acted on"


def test_no_javascript_escapes_in_the_markup():
    r"""← means an arrow inside a JavaScript string; in HTML it is shown as typed."""
    assert not re.findall(r"\\u[0-9a-fA-F]{4}", read("index.html"))



def test_help_explains_every_tab_and_the_tour_points_at_real_ones():
    """Help has an entry for each other tab; every tour card points at a tab that exists;
    the tour starts on first launch only and Help can start it again."""
    tabs = re.findall(r'"([a-z]+)"', re.search(r"export const TABS = \[([^\]]+)\]", read("js/store.js")).group(1))
    help_, tour, app = read("js/tabs/help.js"), read("js/components/tour.js"), read("js/app.js")
    covered = set(re.findall(r'\{ id: "([a-z]+)"', help_))
    assert covered == set(tabs) - {"help"}
    steps = set(re.findall(r'\{ tab: "([a-z]+)"', tour))
    assert steps <= set(tabs) and "help" in steps
    assert "localStorage.getItem(SEEN)" in tour and "maybeStartTour()" in app
    assert "startTour" in help_
