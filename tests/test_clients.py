"""The service clients' own small behaviours."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from nzb2seed.clients import PP_REPAIR, SABnzbd  # noqa: E402


def fake_sab(cats):
    calls = []
    sab = SABnzbd("http://sab.example", "k")

    def call(mode, **kw):
        calls.append((mode, kw))
        return {"config": {"categories": cats}} if mode == "get_config" else {}
    sab._call = call
    return sab, calls


def test_nzb2seed_gets_its_own_sabnzbd_category():
    """Created with its own folder and +Repair (never +Delete)."""
    sab, calls = fake_sab([{"name": "*"}, {"name": "tv"}])
    assert sab.ensure_category("nzb2seed", "nzb2seed") is True
    made = [kw for mode, kw in calls if mode == "set_config"][0]
    assert made["keyword"] == "nzb2seed" and made["dir"] == "nzb2seed" and made["pp"] == str(PP_REPAIR)


def test_an_existing_category_is_left_as_it_is():
    sab, calls = fake_sab([{"name": "*"}, {"name": "NZB2seed"}])
    assert sab.ensure_category("nzb2seed", "nzb2seed") is False
    assert not [c for c in calls if c[0] == "set_config"]
