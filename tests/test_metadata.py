"""Outside lookups: Cloudflare challenges go through FlareSolverr, with one reused session."""
import io
import json
import urllib.error

from nzb2seed import metadata

CHALLENGE = b"<!DOCTYPE html><html><head><title>Just a moment...</title></head></html>"


class FakeResp(io.BytesIO):
    def __init__(self, body, status=200):
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_urlopen(site_body, site_status, calls):
    def urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        if url.endswith("/v1"):
            cmd = json.loads(req.data)
            calls.append(cmd)
            if cmd["cmd"] == "sessions.create":
                return FakeResp(json.dumps({"status": "ok", "session": "s1"}).encode())
            if cmd["cmd"] == "request.get":
                page = ('<div class="post-list"><div class="pl-body"><div class="post" id="42"><div class="p-head">'
                        '<span class="p-time" data="5" title="x"></span><span class="p-cat c-5">'
                        '<a href="#" class="c-adult">TV</a><a href="#" class="c-child">HD</a></span>'
                        '<h2><a class="p-title" href="https://predb.me?post=42">Rel-GRP</a></h2></div></div>')
                return FakeResp(json.dumps({"status": "ok", "solution": {"status": 200, "response": page}}).encode())
            return FakeResp(b'{"status": "ok"}')
        if site_status >= 400:
            raise urllib.error.HTTPError(url, site_status, "x", {}, io.BytesIO(site_body))
        return FakeResp(site_body, site_status)
    return urlopen


def stub(monkeypatch, fn):
    """The internet (only through the proxy) and FlareSolverr (on the LAN) both answer via fn."""
    class Local:
        open = staticmethod(fn)
    monkeypatch.setattr(metadata, "_urlopen", fn)
    monkeypatch.setattr(metadata, "_LOCAL", Local())


def test_plain_json_is_used_directly(monkeypatch):
    calls = []
    stub(monkeypatch, fake_urlopen(b'{"a": 1}', 200, calls))
    metadata.configure("http://fs:8191", "http://proxy:8888")
    assert metadata.get_json("https://example/x") == ({"a": 1}, None) and calls == []


def test_challenge_goes_through_flaresolverr_with_one_session(monkeypatch):
    calls = []
    stub(monkeypatch, fake_urlopen(CHALLENGE, 403, calls))
    metadata.configure("http://fs:8191", "http://proxy:8888")
    try:
        r1 = metadata.predb_me("Rel-GRP")
        r2 = metadata.predb_me("Rel-GRP")
    finally:
        metadata.close_session()
    assert r1["found"] and r2["found"] and r1["pretime"] == 5 and r1["section"] == "TV / HD"
    assert r1["url"].endswith("post=42")
    kinds = [c["cmd"] for c in calls]
    assert kinds == ["sessions.create", "request.get", "request.get", "sessions.destroy"]
    assert all(c.get("session") == "s1" for c in calls if c["cmd"] != "sessions.create")


def test_challenge_without_flaresolverr_is_reported(monkeypatch):
    stub(monkeypatch, fake_urlopen(CHALLENGE, 403, []))
    metadata.configure("", "http://proxy:8888")
    r = metadata.predb_me("Rel-GRP")
    assert not r["available"] and "no FlareSolverr" in r["why"]
