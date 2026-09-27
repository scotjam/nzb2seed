"""The page's own files, and the live feed that keeps it up to date."""
import base64
import os
import socket
import sys
import threading
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from test_gui_login import server  # noqa: E402, F401  (fixture)

AUTH = "Basic " + base64.b64encode(b"admin:nzb2seed").decode()


def get(url, auth=True):
    req = urllib.request.Request(url)
    if auth:
        req.add_header("Authorization", AUTH)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()
    except urllib.error.HTTPError as e:
        return e.code, "", b""


def raw(url, path):
    """A request exactly as written - urllib would tidy a path like /js/../gui.py."""
    host, port = url.split("//")[1].split(":")
    with socket.create_connection((host, int(port))) as s:
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: {AUTH}\r\nConnection: close\r\n\r\n".encode())
        return s.recv(200).split(b" ")[1]


def test_the_pages_files_are_served_with_their_types(server):  # noqa: F811
    app, url = server
    for path, kind in (("/js/app.js", "text/javascript"), ("/css/app.css", "text/css"),
                       ("/vendor/preact-htm.js", "text/javascript"), ("/favicon.svg", "image/svg+xml"),
                       ("/", "text/html")):
        code, ctype, body = get(url + path)
        assert code == 200 and ctype.startswith(kind) and body, path


def test_the_files_need_the_login_too(server):  # noqa: F811
    app, url = server
    assert get(url + "/js/app.js", auth=False)[0] == 401


def test_nothing_outside_the_pages_folders_is_served(server):  # noqa: F811
    app, url = server
    for path in ("/js/../../gui.py", "/js/%2e%2e/%2e%2e/gui.py", "/css/..%5c..%5cgui.py", "/web/index.html",
                 "/js/app.py", "/vendor/../../nzb2seed.toml", "/js//etc/passwd", "/js/C:/x.js"):
        assert raw(url, path) == b"404", path


def test_the_classic_page_is_gone(server):  # noqa: F811
    """It lives on only in tests/ui/baseline, as what the new page is compared with."""
    app, url = server
    assert get(url + "/classic")[0] == 404 and get(url + "/classic.html")[0] == 404


def test_the_live_feed_names_what_changed(server):  # noqa: F811
    app, url = server
    req = urllib.request.Request(url + "/api/events")
    req.add_header("Authorization", AUTH)
    with urllib.request.urlopen(req, timeout=10) as r:
        assert r.headers["Content-Type"] == "text/event-stream"
        assert r.readline().startswith(b": connected")
        r.readline()
        threading.Timer(0.2, lambda: app.events.publish("jobs")).start()
        lines = [r.readline() for _ in range(2)]
    assert lines[0] == b"event: change\n" and b'"jobs"' in lines[1]


def test_a_job_changing_is_told_to_the_feed(server):  # noqa: F811
    app, url = server
    before = app.events.version
    app.start_job("x", "build", lambda cfg: {"result": "done"})
    version, topics = app.events.wait(before, 5)
    assert "jobs" in topics
