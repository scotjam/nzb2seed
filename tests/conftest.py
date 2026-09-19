"""Tests that fake Prowlarr and SABnzbd hand NZBs straight from the fake Prowlarr; the
real route (SABnzbd fetching them as paused jobs) is tested in test_grab.py."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from nzb2seed import grab  # noqa: E402


class PassSource:
    def __init__(self, cfg, pr, sab):
        self.pr = pr

    def search(self, *a, **kw):
        return self.pr.search(*a, **kw)

    def fetch(self, rel):
        return self.pr.fetch(rel)

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _nzbs_from_the_fake_prowlarr(request, monkeypatch):
    if request.module.__name__.endswith("test_grab"):
        return
    monkeypatch.setattr(grab, "Source", PassSource)
    monkeypatch.setattr(grab, "remove_stray_checks", lambda sab: 0)
