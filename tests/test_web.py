"""The page has to parse.

A single bad character in the page's script stops every tab working, and nothing else in
the test suite would notice - the Python side is perfectly happy. This runs the page's
script through node when node is there.
"""
import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

PAGE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "nzb2seed", "web", "index.html")


def page() -> str:
    with open(PAGE, encoding="utf-8") as fh:
        return fh.read()


def scripts() -> list[str]:
    return re.findall(r"<script[^>]*>(.*?)</script>", page(), re.S)


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_the_pages_script_parses(tmp_path):
    for i, src in enumerate(scripts()):
        f = tmp_path / f"page{i}.js"
        f.write_text(src, encoding="utf-8")
        done = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        assert done.returncode == 0, f"script block {i} does not parse:\n{done.stderr}"


def test_every_element_the_script_reaches_for_exists():
    """$("x") on an element that is not in the page throws, which kills the whole script."""
    html = page()
    ids = set(re.findall(r'id="([^"]+)"', html))
    wanted = set(re.findall(r'\$\("([^"]+)"\)', html))
    missing = {w for w in wanted if w not in ids}
    assert not missing, f"the script looks for elements that are not in the page: {sorted(missing)}"


def test_the_tabs_and_their_views_line_up():
    html = page()
    tabs = re.findall(r'<a href="#([a-z]+)"', html)
    views = set(re.findall(r'<section id="view-([a-z]+)"', html))
    listed = re.search(r'const views = \[([^\]]+)\]', html)
    named = {x.strip().strip('"\'') for x in listed.group(1).split(",")}
    for tab in tabs:
        if tab in ("build", "jobs", "seasons", "assemble", "auto", "demand", "settings"):
            assert tab in views, f"the {tab} tab has no section"
            assert tab in named, f"the {tab} tab is not in the router's list"
