"""The GUI asks for a login: admin / nzb2seed by default, changeable in Settings."""
import base64
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from nzb2seed import gui


@pytest.fixture
def server(tmp_path):
    cfg = tmp_path / "nzb2seed.toml"
    cfg.write_text("")
    app = gui.App(str(cfg))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), gui.make_handler(app, None, {"127.0.0.1"}))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield app, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def call(url, user=None, pw=None, body=None):
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode())
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if user is not None:
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode())
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"null") if "json" in r.headers.get("Content-Type", "") else None
    except urllib.error.HTTPError as e:
        return e.code, None


def test_default_login_is_required_and_works(server):
    app, url = server
    assert call(url + "/api/settings")[0] == 401
    assert call(url + "/api/settings", "admin", "wrong")[0] == 401
    assert call(url + "/api/settings", "someone", "nzb2seed")[0] == 401
    code, body = call(url + "/api/settings", "admin", "nzb2seed")
    assert code == 200 and body["default_login"] is True


def test_changing_the_password_in_settings_applies_at_once(server):
    app, url = server
    _, body = call(url + "/api/settings", "admin", "nzb2seed")
    s = body["settings"]
    s["gui"]["password"] = "better-secret"
    assert call(url + "/api/settings", "admin", "nzb2seed", {"settings": s})[0] == 200
    assert call(url + "/api/settings", "admin", "nzb2seed")[0] == 401
    code, body = call(url + "/api/settings", "admin", "better-secret")
    assert code == 200 and body["default_login"] is False
    assert 'password = "better-secret"' in open(app.cfg.path).read()


def test_empty_password_switches_login_off_for_local_clients(server):
    app, url = server
    app.cfg.gui_password = ""
    assert call(url + "/api/settings")[0] == 200     # 127.0.0.1 is a local client
