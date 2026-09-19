"""nzb2seed never contacts an indexer (Prowlarr redirects are refused), and its own
lookups only go out through the configured proxy."""
import urllib.error
import urllib.request

import pytest
import requests

from nzb2seed import metadata
from nzb2seed.clients import ApiError, Prowlarr, Release, short_reason


class Resp:
    def __init__(self, status, text="", headers=None, content=b""):
        self.status_code, self.text, self.headers, self.content = status, text, headers or {}, content
        self.is_redirect = status in (301, 302)


def rel(url):
    return Release("X", "usenet", "IndexerA", 1, 1, "g", url, "", "", 0, None, None)


def test_a_redirect_to_the_indexer_is_never_followed(monkeypatch):
    pr = Prowlarr("http://prowlarr.local:9696", "k")
    calls = []

    def get(url, **kw):
        calls.append(url)
        return Resp(301, headers={"Location": "https://indexer.example/get/1"})
    monkeypatch.setattr(pr.s, "get", get)
    monkeypatch.setattr(requests, "get", lambda *a, **kw: pytest.fail("fetched directly"))
    with pytest.raises(ApiError, match="never contacts indexers"):
        pr.fetch(rel("http://prowlarr.local:9696/1/download?link=x"))
    assert calls == ["http://prowlarr.local:9696/1/download?link=x"]


def test_prowlarr_proxied_download(monkeypatch):
    pr = Prowlarr("http://prowlarr.local:9696", "k")
    monkeypatch.setattr(pr.s, "get", lambda url, **kw: Resp(200, content=b"torrent"))
    assert pr.fetch(rel("http://prowlarr.local:9696/1/download?link=x")) == b"torrent"


def test_lookups_need_the_proxy(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: pytest.fail("went out directly"))
    metadata.configure(None, None)
    with pytest.raises(urllib.error.URLError, match="proxy"):
        metadata._get("https://api.example/x")
    data, why = metadata.get_json("https://api.example/x")
    assert data is None and "proxy" in why
    seen = {}

    class Opener:
        def open(self, req, timeout):
            raise urllib.error.URLError("stop")

    def build(handler):
        seen["proxies"] = handler.proxies
        return Opener()
    monkeypatch.setattr(urllib.request, "build_opener", build)
    metadata.configure(None, "http://127.0.0.1:8888")
    try:
        metadata.get_json("https://api.example/x")
    finally:
        metadata.configure(None, None)
    assert seen["proxies"] == {"http": "http://127.0.0.1:8888", "https": "http://127.0.0.1:8888"}


def test_error_pages_become_one_line():
    r = Resp(403, "<html><head><title>Forbidden</title><style>p{}</style></head><body><p>Grab   limit reached</p></body></html>")
    assert short_reason(r).startswith("HTTP 403 - Forbidden:")
    assert short_reason(r).endswith("Grab limit reached") and "p{}" not in short_reason(r)
