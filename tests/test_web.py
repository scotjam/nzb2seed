"""The page has to load.

The interface is ES modules (Preact + htm, no build step), so a single bad character in
one of them stops the page - and nothing on the Python side would notice. These check
every module parses and every import points at a file that is there. What the page does
is checked in a browser-like environment by tests/ui (see test_ui.py).
"""
import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "nzb2seed", "web")


def modules() -> list[str]:
    out = []
    for root, _, files in os.walk(os.path.join(WEB, "js")):
        out += [os.path.join(root, f) for f in files if f.endswith(".js")]
    return sorted(out)


def read(p) -> str:
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def test_the_page_is_split_into_modules():
    """Never again one file holding the whole interface."""
    names = {os.path.relpath(p, WEB).replace(os.sep, "/") for p in modules()}
    for want in ("js/app.js", "js/api.js", "js/store.js", "js/util.js", "js/tabs/jobs.js", "js/tabs/build.js",
                 "js/tabs/seasons.js", "js/tabs/assemble.js", "js/tabs/auto.js", "js/tabs/demand.js",
                 "js/tabs/settings.js"):
        assert want in names
    biggest = max(len(read(p).splitlines()) for p in modules())
    assert biggest < 450, "a module has grown too big - split it"


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_every_module_parses(tmp_path):
    for p in modules() + [os.path.join(WEB, "vendor", "preact-htm.js")]:
        f = tmp_path / (os.path.basename(p) + ".mjs")
        f.write_text(read(p), encoding="utf-8")
        done = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        assert done.returncode == 0, f"{p} does not parse:\n{done.stderr}"


def test_every_import_points_at_a_file_that_is_there():
    for p in modules():
        for target in re.findall(r'''^\s*(?:import|export)\s[^;]*?from\s+["']([^"']+)["']''', read(p), re.M | re.S):
            assert target.startswith("."), f"{p} imports {target} from outside the page"
            assert os.path.isfile(os.path.normpath(os.path.join(os.path.dirname(p), target))), f"{p}: {target} is missing"


def test_the_page_loads_its_style_and_the_app():
    page = read(os.path.join(WEB, "index.html"))
    for ref in re.findall(r'(?:href|src)="([^":]+)"', page):
        assert os.path.isfile(os.path.join(WEB, ref)), f"index.html refers to {ref}, which is missing"
    assert 'type="module" src="js/app.js"' in page


def test_no_javascript_escapes_in_the_markup():
    """\\u2190 written into html`` text shows as the six characters, not the arrow."""
    for p in modules():
        for tpl in re.findall(r"html`(.*?)`", read(p), re.S):
            assert not re.search(r"\\u[0-9a-fA-F]{4}", tpl), f"{p}: a \\u escape inside markup"
