"""Local web GUI: python -m nzb2seed gui

A small JSON API over the same pipeline the CLI uses, plus one static page.
Each build runs in its own thread and reports into its own job log.
"""
from __future__ import annotations

import base64
import dataclasses
import hmac
import ipaddress
import json
import os
import re
import socket
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import urlsplit

from . import config as config_mod
from . import report
from .clients import ApiError, Prowlarr, QBittorrent, Release, SABnzbd
from . import metadata
from . import season as season_mod
from .torrent import TorrentError, parse as parse_torrent
from .pipeline import (Abort, Options, execute_assemble, execute_run, group_selection, pair,
                       previous_downloads, search)

MAX_BODY = 64 << 20
MAX_LINES = 20000


# ---------------------------------------------------------------- jobs

INTERRUPTED = ("the GUI stopped while this build was running; build it again - files it "
               "already placed are reused and a stopped torrent stays stopped")


class Job:
    def __init__(self, id: int, title: str, kind: str, on_change=None):
        self.id = id
        self.title = title
        self.kind = kind
        self.status = "running"      # running | done | failed | cancelled | interrupted
        self.result = ""
        self.lines: list[dict] = []
        self.progress = ""
        self.pieces = ""
        self.started = time.time()
        self.ended: float | None = None
        self.cancel = threading.Event()
        self.lock = threading.Lock()
        self.on_change = on_change or (lambda urgent=False: None)
        self.question: dict | None = None     # {"prompt", "choices"} while waiting for the person
        self._answer: int | None = None
        self._answered = threading.Event()

    # report sink
    def emit(self, kind: str, text: str):
        with self.lock:
            self.progress = ""
            if len(self.lines) < MAX_LINES:
                self.lines.append({"k": kind, "t": text})
        self.on_change()

    def progress_fn(self, text: str):
        self.progress = text
        self.on_change()

    def end_progress(self):
        if self.progress:
            self.emit("info", self.progress)

    def pieces_fn(self, states: str):
        self.pieces = states
        self.on_change()

    def cancelled(self) -> bool:
        return self.cancel.is_set()

    def ask(self, prompt: str, choices: list[dict]) -> int | None:
        """Pause the build until the person picks a choice (or stops the build)."""
        self._answer = None
        self._answered.clear()
        self.question = {"prompt": prompt, "choices": choices}
        self.status = "waiting"
        self.emit("warn", "waiting for you: " + prompt)
        self.on_change(True)
        while not self._answered.wait(1.0):
            if self.cancel.is_set():
                break
        self.question = None
        self.status = "running"
        self.on_change(True)
        if self.cancel.is_set():
            raise report.Cancelled("cancelled")
        return self._answer

    def answer(self, choice: int | None):
        if self.question is None:
            raise ValueError("this build is not waiting for an answer")
        if choice is not None and not (0 <= choice < len(self.question["choices"])):
            raise ValueError("no such choice")
        self._answer = choice
        self._answered.set()

    def summary(self) -> dict:
        return {"id": self.id, "title": self.title, "kind": self.kind, "status": self.status,
                "result": self.result, "started": self.started, "ended": self.ended,
                "progress": self.progress, "question": self.question,
                "steps": [x["t"] for x in self.lines if x["k"] == "step"]}

    def to_dict(self) -> dict:
        with self.lock:
            return {**self.summary(), "lines": list(self.lines), "pieces": self.pieces}

    @classmethod
    def from_dict(cls, d: dict, on_change) -> "Job":
        job = cls(d["id"], d.get("title", ""), d.get("kind", "build"), on_change)
        job.status, job.result = d.get("status", "done"), d.get("result", "")
        job.lines, job.pieces = d.get("lines", []), d.get("pieces", "")
        job.progress, job.started, job.ended = d.get("progress", ""), d.get("started", 0), d.get("ended")
        job.question = None
        if job.status in ("running", "waiting"):     # its thread died with the previous GUI process
            job.status, job.result, job.progress = "interrupted", INTERRUPTED, ""
            job.ended = job.ended or time.time()
            job.lines.append({"k": "warn", "t": INTERRUPTED})
        return job


class JobStore:
    """Keeps the job list in a JSON file: written ~once a second while anything changes,
    immediately when a job starts or ends, and on shutdown."""

    def __init__(self, path: str, jobs_fn):
        self.path = path
        self.jobs_fn = jobs_fn
        self.dirty = threading.Event()
        self.write_lock = threading.Lock()
        threading.Thread(target=self._loop, name="job-store", daemon=True).start()

    def load(self) -> list[dict]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                return json.load(fh).get("jobs", [])
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            print(f"could not read {self.path} ({e}); starting with an empty job list", flush=True)
            return []

    def changed(self, urgent: bool = False):
        if urgent:
            try:
                self.flush()
            except OSError as e:     # never let bookkeeping break a build
                print(f"could not save jobs to {self.path}: {e}", flush=True)
                self.dirty.set()
        else:
            self.dirty.set()

    def flush(self):
        with self.write_lock:
            self.dirty.clear()
            data = {"jobs": [j.to_dict() for j in sorted(self.jobs_fn(), key=lambda j: j.id)]}
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = f"{self.path}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, self.path)

    def _loop(self):
        while True:
            self.dirty.wait()
            time.sleep(1.0)              # coalesce a burst of log lines into one write
            try:
                self.flush()
            except OSError as e:
                print(f"could not save jobs to {self.path}: {e}", flush=True)


class _Sink:
    """Adapter so a Job can be the thread's report sink."""

    def __init__(self, job: Job):
        self.job = job

    def emit(self, kind, text):
        self.job.emit(kind, text)

    def progress(self, text):
        self.job.progress_fn(text)

    def end_progress(self):
        self.job.end_progress()

    def pieces(self, states):
        self.job.pieces_fn(states)

    def ask(self, prompt, choices):
        return self.job.ask(prompt, choices)

    def cancelled(self):
        return self.job.cancelled()


class App:
    def __init__(self, config_path: str | None):
        self.config_path = config_path
        self.cfg = config_mod.load_or_default(config_path)
        metadata.configure(self.cfg.flaresolverr_url, self.cfg.outbound_proxy)
        self.jobs: dict[int, Job] = {}
        self.lock = threading.Lock()
        self.store = JobStore(os.path.join(os.path.dirname(os.path.abspath(self.cfg.path)), "jobs.json"),
                              lambda: list(self.jobs.values()))
        for d in self.store.load():
            job = Job.from_dict(d, self.store.changed)
            self.jobs[job.id] = job
        if self.jobs:
            self.store.flush()           # persist any "interrupted" marks right away
        self._next = max(self.jobs, default=0) + 1

    def active_build(self, title: str) -> Job | None:
        """A build of this torrent that has not ended yet (a second one would fight it)."""
        with self.lock:
            return next((j for j in self.jobs.values() if j.kind == "build" and j.title == title
                         and j.status in ("running", "waiting")), None)

    def start_job(self, title: str, kind: str, fn) -> Job:
        with self.lock:
            job = Job(self._next, title, kind, self.store.changed)
            self._next += 1
            self.jobs[job.id] = job
        self.store.changed(urgent=True)
        cfg = self.cfg

        def run():
            report.use(_Sink(job))
            try:
                out = fn(cfg) or {}
                job.result = out.get("result", "done")
                job.status = "done"
            except report.Cancelled:
                job.status, job.result = "cancelled", "cancelled"
                report.warn("cancelled - nothing further was changed; a stopped torrent stays stopped")
            except (Abort, ApiError, ValueError, OSError) as e:
                job.status, job.result = "failed", str(e)
                report.warn(str(e))
            except Exception as e:  # keep the server alive and show what broke
                job.status, job.result = "failed", f"{type(e).__name__}: {e}"
                report.line(traceback.format_exc())
            finally:
                job.progress = ""
                job.ended = time.time()
                self.store.changed(urgent=True)
        threading.Thread(target=run, name=f"job-{job.id}", daemon=True).start()
        return job


# ---------------------------------------------------------------- HTTP

def _rel(d: dict) -> Release:
    fields = {f.name for f in dataclasses.fields(Release)}
    return Release(**{k: v for k, v in d.items() if k in fields})


UPLOAD_PREFIX = "upload:"


def _upload_path(cfg, infohash: str) -> str:
    return os.path.join(cfg.torrent_dir, "uploads", f"{infohash}.torrent")


def upload_torrent(cfg, name: str, data: bytes) -> Release:
    """Keep an uploaded .torrent and describe it like a search result, so the Build tab
    can pair and build it the same way. Its guid points back at the saved file."""
    t = parse_torrent(data)                     # raises TorrentError for anything else
    path = _upload_path(cfg, t.infohash)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return Release(title=t.name, protocol="torrent", indexer=os.path.basename(name or "uploaded.torrent"),
                   indexer_id=0, size=t.total_size, guid=UPLOAD_PREFIX + t.infohash, download_url="",
                   info_url="", publish_date="", grabs=None, seeders=None, files=len(t.real_files))


def uploaded_data(cfg, rel: Release) -> bytes | None:
    """The saved .torrent behind an uploaded result (None: it came from a search)."""
    if not rel.guid.startswith(UPLOAD_PREFIX):
        return None
    infohash = rel.guid[len(UPLOAD_PREFIX):]
    if not re.fullmatch(r"[0-9a-f]{40}", infohash):
        raise ValueError("not an uploaded torrent")
    try:
        with open(_upload_path(cfg, infohash), "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        raise ValueError("the uploaded .torrent is gone; upload it again") from None


def _opts(d: dict) -> Options:
    return Options(pp=d.get("pp") or None, output_dir=d.get("output_dir") or None,
                   no_cleanup=bool(d.get("no_cleanup")), local_verify=bool(d.get("local_verify")),
                   no_qbit=bool(d.get("no_qbit")), start=bool(d.get("start")),
                   dry_run=bool(d.get("dry_run")),
                   retry_bad=None if d.get("retry_bad") is None else bool(d.get("retry_bad")))


def make_handler(app: App, login_override: tuple[str, str] | None, allowed_hosts: set[str]):
    """``login_override`` (from --username/--password) beats the login in the config."""

    def login() -> tuple[str, str]:
        if login_override:
            return login_override
        return app.cfg.gui_username or "", app.cfg.gui_password or ""

    page = resources.files("nzb2seed").joinpath("web/index.html").read_bytes()

    class H(BaseHTTPRequestHandler):
        server_version = "nzb2seed"

        def log_message(self, fmt, *args):
            pass

        # -- plumbing
        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _err(self, msg, code=400):
            self._json({"error": msg}, code)

        def _guard(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            if allowed_hosts and host not in allowed_hosts and not _is_ip(host):
                # also stops DNS-rebinding pages from reaching the API through a browser
                self._err(f"unexpected Host header {host!r}; start the GUI with --allowed-host {host} "
                          "if that is how you reach this machine", 403)
                return False
            user, password = login()
            if not password and not _is_local_client(self.client_address[0]):
                self._err("the login is switched off, so only this machine and private-network "
                          "addresses may use the GUI", 403)
                return False
            if password:
                auth = self.headers.get("Authorization", "")
                ok = False
                if auth.startswith("Basic "):
                    try:
                        u, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
                        # compare both, always, so timing does not reveal which one was wrong
                        ok = hmac.compare_digest(u.encode(), user.encode()) & \
                            hmac.compare_digest(pw.encode(), password.encode())
                    except ValueError:
                        pass
                if not ok:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="nzb2seed"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return False
            return True

        def _body(self) -> dict | None:
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                self._err("expected application/json", 415)  # blocks cross-site form posts
                return None
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                self._err("request too large", 413)
                return None
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                self._err("invalid JSON")
                return None

        # -- routes
        def do_GET(self):
            if not self._guard():
                return
            path = urlsplit(self.path).path
            if path in ("/", "/index.html"):
                return self._send(200, page, "text/html; charset=utf-8")
            if path == "/api/settings":
                return self._json(self.settings_payload())
            if path == "/api/season/reports":
                return self._json(season_mod.reports(app.cfg, SABnzbd(app.cfg.sab_url, app.cfg.sab_key)))
            if path in ("/api/season/report", "/api/season/file"):
                return self.season_file(path)
            if path == "/api/jobs":
                with app.lock:
                    jobs = [j.summary() for j in sorted(app.jobs.values(), key=lambda j: -j.id)]
                return self._json(jobs)
            if path.startswith("/api/jobs/"):
                job = app.jobs.get(int(path.rsplit("/", 1)[1]) if path.rsplit("/", 1)[1].isdigit() else -1)
                if not job:
                    return self._err("no such job", 404)
                q = urlsplit(self.path).query
                since = int(q.split("since=")[1].split("&")[0]) if "since=" in q else 0
                with job.lock:
                    lines = job.lines[since:]
                return self._json({**job.summary(), "lines": lines, "next": since + len(lines),
                                   "pieces": job.pieces})
            self._err("not found", 404)

        def do_POST(self):
            if not self._guard():
                return
            body = self._body()
            if body is None:
                return
            path = urlsplit(self.path).path
            try:
                if path == "/api/settings":
                    return self.save_settings(body)
                if path == "/api/test":
                    return self._json(self.test_connections())
                if path == "/api/search":
                    q = (body.get("query") or "").strip()
                    if not q:
                        return self._err("type a release name or search text")
                    torrents, nzbs = search(app.cfg, q)
                    if body.get("protocol") == "usenet":
                        torrents = []
                    return self._json({"torrents": [dataclasses.asdict(r) for r in torrents],
                                       "usenet": [dataclasses.asdict(r) for r in nzbs]})
                if path == "/api/pair":
                    return self.pair(body)
                if path == "/api/season/shows":
                    return self._json(metadata.tvmaze_search((body.get("query") or "").strip()))
                if path == "/api/season/seasons":
                    return self._json(metadata.tvmaze_seasons(int(body["show_id"])))
                if path == "/api/season/options":
                    return self.season_options(body)
                if path == "/api/season/grab":
                    return self.season_grab(body)
                if path == "/api/previous":
                    sab = SABnzbd(app.cfg.sab_url, app.cfg.sab_key)
                    return self._json({"previous": previous_downloads(app.cfg, sab, body.get("title") or "")})
                if path == "/api/build":
                    return self.build(body)
                if path == "/api/upload_torrent":
                    try:
                        data = base64.b64decode(body.get("torrent_b64") or "", validate=True)
                    except ValueError:
                        return self._err("that upload was not readable")
                    if not data:
                        return self._err("choose a .torrent file")
                    try:
                        rel = upload_torrent(app.cfg, body.get("torrent_name") or "", data)
                    except TorrentError as e:
                        return self._err(f"not a usable .torrent file: {e}")
                    except Exception as e:          # bencode errors on random files
                        return self._err(f"not a .torrent file ({type(e).__name__})")
                    return self._json({"torrent": dataclasses.asdict(rel)})
                if path == "/api/assemble":
                    return self.assemble(body)
                if path.startswith("/api/jobs/") and path.endswith("/answer"):
                    job = app.jobs.get(int(path.split("/")[3]))
                    if not job:
                        return self._err("no such job", 404)
                    choice = body.get("choice")
                    job.answer(None if choice is None else int(choice))
                    return self._json({"ok": True})
                if path.startswith("/api/jobs/") and path.endswith("/cancel"):
                    job = app.jobs.get(int(path.split("/")[3]))
                    if not job:
                        return self._err("no such job", 404)
                    job.cancel.set()
                    return self._json({"ok": True})
            except (ApiError, Abort, ValueError) as e:
                return self._err(str(e), 502 if isinstance(e, ApiError) else 400)
            except Exception as e:
                return self._err(f"{type(e).__name__}: {e}", 500)
            self._err("not found", 404)

        # -- handlers
        def settings_payload(self):
            cfg = app.cfg
            user, password = login()
            return {"path": str(cfg.path), "exists": os.path.isfile(cfg.path),
                    "settings": config_mod.to_sections(cfg),
                    "default_login": (not login_override and password == config_mod.DEFAULT_GUI_PASSWORD),
                    "login_from_command_line": bool(login_override)}

        def save_settings(self, body):
            sections = body.get("settings") or {}
            try:
                cfg = config_mod.from_sections(sections, app.cfg.path)
            except (ValueError, TypeError) as e:
                return self._err(str(e))
            config_mod.save(cfg)
            app.cfg = cfg
            metadata.configure(cfg.flaresolverr_url, cfg.outbound_proxy)
            return self._json(self.settings_payload())

        def season_options(self, body):
            show = metadata.tvmaze_show(int(body["show_id"]))
            sn = int(body["season"])
            eps = metadata.tvmaze_episodes(show["id"], sn)
            wanted = [e["number"] for e in eps]
            pr = Prowlarr(app.cfg.prowlarr_url, app.cfg.prowlarr_key)
            opts = season_mod.find_options(app.cfg, pr, show["name"], sn, wanted)
            return self._json({"show": show, "episodes": eps, "links": metadata.links(show),
                               "options": [o.summary(wanted) for o in opts]})

        def season_grab(self, body):
            sid, sn, key = int(body["show_id"]), int(body["season"]), body["key"]
            packing = body.get("packing") or None
            label = body.get("label") or f"season {sn}"
            job = app.start_job(label, "season",
                                lambda cfg: season_mod.grab_season(cfg, sid, sn, key, packing))
            return self._json({"id": job.id})

        def season_file(self, path):
            q = dict(p.split("=", 1) for p in urlsplit(self.path).query.split("&") if "=" in p)
            from urllib.parse import unquote
            name = unquote(q.get("name", ""))
            if not name or "/" in name or "\\" in name or name.startswith("."):
                return self._err("bad name")
            root = season_mod.downloads_root(app.cfg, SABnzbd(app.cfg.sab_url, app.cfg.sab_key))
            side = os.path.realpath(os.path.join(root, name + season_mod.SIDE_SUFFIX))
            if os.path.dirname(side) != os.path.realpath(root) or not os.path.isdir(side):
                return self._err("no such report", 404)
            if path == "/api/season/report":
                with open(os.path.join(side, "report.json"), encoding="utf-8") as fh:
                    r = json.load(fh)
                mi = os.path.join(side, "mediainfo.txt")
                if os.path.exists(mi):
                    with open(mi, encoding="utf-8", errors="replace") as fh:
                        r["mediainfo"] = fh.read()
                return self._json(r)
            rel = unquote(q.get("path", ""))
            target = os.path.realpath(os.path.join(side, rel))
            if not target.startswith(side + os.sep) or not os.path.isfile(target):
                return self._err("no such file", 404)
            ctype = {".png": "image/png", ".jpg": "image/jpeg", ".txt": "text/plain; charset=utf-8",
                     ".html": "text/html; charset=utf-8"}.get(os.path.splitext(target)[1].lower(),
                                                              "application/octet-stream")
            with open(target, "rb") as fh:
                return self._send(200, fh.read(), ctype)

        def test_connections(self):
            cfg = app.cfg
            out = {}
            checks = {
                "prowlarr": lambda: Prowlarr(cfg.prowlarr_url, cfg.prowlarr_key).status(),
                "sabnzbd": lambda: f"SABnzbd {SABnzbd(cfg.sab_url, cfg.sab_key).version()}",
                "qbittorrent": lambda: self._qb_check(cfg),
            }
            for name, fn in checks.items():
                try:
                    out[name] = {"ok": True, "text": fn()}
                except Exception as e:
                    out[name] = {"ok": False, "text": str(e) or type(e).__name__}
            return out

        @staticmethod
        def _qb_check(cfg):
            qb = QBittorrent(cfg.qbit_url, cfg.qbit_user, cfg.qbit_pass)
            qb.login()
            return f"qBittorrent {qb.app_version()} (WebAPI {'.'.join(map(str, qb.api_version()))})"

        def pair(self, body):
            torrent = _rel(body["torrent"])
            nzbs = [_rel(n) for n in body.get("usenet", [])]
            pr = Prowlarr(app.cfg.prowlarr_url, app.cfg.prowlarr_key)
            groups, nzbs, how = pair(app.cfg, pr, torrent, nzbs)
            return self._json({"groups": [[n.guid for n in g] for g in groups],
                               "usenet": [dataclasses.asdict(n) for n in nzbs], "how": how})

        def build(self, body):
            torrent = _rel(body["torrent"])
            nzbs = [_rel(n) for n in body.get("nzbs", [])]
            groups = group_selection(nzbs)     # empty: the build finds the NZBs itself
            opts = _opts(body.get("options", {}))
            busy = app.active_build(torrent.title)
            if busy:
                return self._err(f"{torrent.title} is already being built (job {busy.id})", 409)
            data = uploaded_data(app.cfg, torrent)   # None: download it through Prowlarr
            job = app.start_job(torrent.title, "build",
                                lambda cfg: execute_run(cfg, opts, torrent, groups, torrent_data=data))
            return self._json({"id": job.id})

        def assemble(self, body):
            sources = [s.strip() for s in body.get("sources", []) if s.strip()]
            if not sources:
                return self._err("add at least one folder holding the NZB download")
            tpath = (body.get("torrent_path") or "").strip()
            if body.get("torrent_b64"):
                os.makedirs(app.cfg.torrent_dir, exist_ok=True)
                name = os.path.basename(body.get("torrent_name") or "upload.torrent")
                tpath = os.path.join(app.cfg.torrent_dir, f"upload-{int(time.time())}-{name}")
                with open(tpath, "wb") as fh:
                    fh.write(base64.b64decode(body["torrent_b64"]))
            if not tpath:
                return self._err("choose a .torrent file")
            opts = _opts(body.get("options", {}))
            job = app.start_job(os.path.basename(tpath), "assemble",
                                lambda cfg: execute_assemble(cfg, opts, tpath, sources))
            return self._json({"id": job.id})

    return H


def _is_ip(host: str) -> bool:
    """IP-literal Host headers are always fine: DNS rebinding needs a domain name."""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _is_local_client(addr: str) -> bool:
    """Loopback or a private LAN address (like the *arr apps' "local addresses")."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_private or ip.is_link_local


def own_names(bind: str) -> set[str]:
    """Names and addresses this machine answers to: the Host headers the GUI accepts."""
    names = {"127.0.0.1", "localhost", "::1"}
    hn = socket.gethostname()
    names |= {hn.lower(), f"{hn.lower()}.local", socket.getfqdn().lower()}
    try:
        names |= {a[4][0] for a in socket.getaddrinfo(hn, None)}
    except OSError:
        pass
    try:   # every IPv4 address of this machine (Linux; harmless elsewhere)
        import subprocess
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=3).stdout
        names |= {a for a in out.split() if a.count(".") == 3}
    except (OSError, subprocess.SubprocessError):
        pass
    try:   # the address used for outgoing LAN traffic (no packet is sent)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect(("192.0.2.1", 9))
            names.add(sk.getsockname()[0])
    except OSError:
        pass
    if bind not in ("0.0.0.0", "::", ""):
        names.add(bind.lower())
    return names


def serve(config_path: str | None, host: str, port: int, password: str | None,
          open_browser: bool, extra_hosts=(), username: str | None = None) -> int:
    allowed = own_names(host) | {h.lower() for h in extra_hosts}
    app = App(config_path)
    override = (username or app.cfg.gui_username or config_mod.DEFAULT_GUI_USER, password) \
        if password is not None else None
    httpd = ThreadingHTTPServer((host, port), make_handler(app, override, allowed))
    ips = [n for n in allowed if _is_ip(n) and n.count(".") == 3 and not n.startswith("127.")]
    lan = sorted(ips, key=lambda n: (not n.startswith("192.168."), n))[0] if ips else None
    url = f"http://{lan if host in ('0.0.0.0', '::') and lan else ('127.0.0.1' if host in ('0.0.0.0', '::') else host)}:{port}/"
    print(f"nzb2seed GUI on {url}  (config: {app.cfg.path})  - Ctrl+C to stop", flush=True)
    user, pw = override or (app.cfg.gui_username, app.cfg.gui_password)
    if not pw:
        print("login switched off: open to this machine and private-network (LAN) addresses only",
              flush=True)
    elif not override and pw == config_mod.DEFAULT_GUI_PASSWORD:
        print(f"login: {user} / {pw} (the default - change it in Settings)", flush=True)
    else:
        print(f"login: {user} / (password set)", flush=True)
    if open_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    import signal

    def on_term(*_):
        raise KeyboardInterrupt      # so "kill" also saves the job list on the way out
    signal.signal(signal.SIGTERM, on_term)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        try:
            app.store.flush()
        except OSError:
            pass
    return 0
