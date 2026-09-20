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
from .clients import ApiError, Autobrr, Prowlarr, QBittorrent, Release, SABnzbd
from . import inbox as inbox_mod
from . import metadata
from . import season as season_mod
from . import series as series_mod
from . import episodes as episodes_mod
from . import retention as retention_mod
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
        episodes_mod.configure_cache(self.cfg.path)
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

    def autobrr_memo(self, memo=None) -> dict:
        """What nzb2seed changed in autobrr, so unticking a filter puts it back:
        {"switched_off": [action ids], "filters_were": {filter id: enabled before}}."""
        path = os.path.join(os.path.dirname(os.path.abspath(self.cfg.path)), "autobrr.json")
        if memo is None:
            try:
                with open(path, encoding="utf-8") as fh:
                    d = json.load(fh)
            except (OSError, ValueError):
                d = {}
            return {"switched_off": list(d.get("switched_off", [])),
                    "filters_were": {str(k): bool(v) for k, v in (d.get("filters_were") or {}).items()}}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"switched_off": sorted(set(memo["switched_off"])),
                       "filters_were": memo["filters_were"]}, fh)
        return memo

    def start_inbox(self):
        """The automatic-build watcher (idle while switched off on the Automatic tab)."""
        self.inbox = inbox_mod.Inbox(self, os.path.join(os.path.dirname(os.path.abspath(self.cfg.path)),
                                                        "inbox.json"))

    def start_retention(self):
        """Hourly sweep that removes builds past their retention age (idle while off)."""
        def loop():
            while True:
                time.sleep(3600)
                cfg = self.cfg
                if not cfg.retention_enabled:
                    continue
                try:
                    out = retention_mod.sweep(cfg, self._qbit(), log=self._retention_log)
                except Exception as e:                       # never let the sweep kill its thread
                    print(f"retention sweep failed: {e}", flush=True)
                    continue
                if out["removed"]:
                    self._retention_log(
                        f"removed {len(out['removed'])} build(s), freeing "
                        f"{retention_mod.gb(out['bytes'])}")
        threading.Thread(target=loop, name="retention", daemon=True).start()

    def _retention_log(self, text: str):
        print(f"[retention] {text}", flush=True)

    def _qbit(self) -> QBittorrent | None:
        cfg = self.cfg
        if not cfg.qbit_url:
            return None
        qb = QBittorrent(cfg.qbit_url, cfg.qbit_user, cfg.qbit_pass)
        qb.login()
        return qb

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


def data_roots(cfg) -> list[str]:
    """The folders the GUI's folder chooser may show: the configured data folders (where
    finished torrents and downloads live), never the rest of the disk."""
    roots = [p[0] for p in (cfg.local_to_qbit or [])] + [p[1] for p in (cfg.sab_to_local or [])]
    roots += [cfg.output_dir] if cfg.output_dir else []
    out = []
    for r in roots:
        r = os.path.realpath(r)
        if os.path.isdir(r) and r not in out:
            out.append(r)
    return out


def local_folder(cfg, path: str) -> str:
    """A path as this machine sees it. A network path from another computer -
    '\\\\nas\\share\\rest', '//nas/share/rest' or 'smb://nas/share/rest' - is found by its
    share name among the configured data folders (a share is usually a folder on one of
    the data disks). Relative paths are refused: nzb2seed runs here, not on your PC."""
    p = path.strip().strip('"').strip()
    m = re.match(r"^(?:smb:)?(?:\\\\|//)([^\\/]+)[\\/]+([^\\/]+)[\\/]*(.*)$", p, re.I)
    if m:
        share, rest = m.group(2), [x for x in re.split(r"[\\/]+", m.group(3)) if x]
        for root in data_roots(cfg):
            for base in (root, os.path.dirname(root)):
                cand = base if os.path.basename(base).casefold() == share.casefold() else None
                if cand is None and os.path.isdir(base):
                    cand = next((os.path.join(base, n) for n in os.listdir(base)
                                 if n.casefold() == share.casefold() and os.path.isdir(os.path.join(base, n))), None)
                if cand:
                    return os.path.join(cand, *rest)
        raise ValueError(f"{path} is a network path, and no shared folder called {share!r} was found in "
                         "the configured data folders - give the folder as the NAS sees it")
    if not os.path.isabs(p):
        raise ValueError(f"{path} is not a full path on the NAS (nzb2seed runs there, not on your PC)")
    return p


def within_roots(cfg, path: str) -> bool:
    real = os.path.realpath(path)
    return any(real == r or real.startswith(r.rstrip(os.sep) + os.sep) for r in data_roots(cfg))


def _opts(d: dict) -> Options:
    return Options(pp=d.get("pp") or None, output_dir=d.get("output_dir") or None,
                   no_cleanup=bool(d.get("no_cleanup")), local_verify=bool(d.get("local_verify")),
                   no_qbit=bool(d.get("no_qbit")), start=bool(d.get("start")),
                   dry_run=bool(d.get("dry_run")),
                   retry_bad=None if d.get("retry_bad") is None else bool(d.get("retry_bad")),
                   fetch_missing=bool(d.get("fetch_missing")))


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
            if path == "/api/retention":
                try:
                    return self._json(retention_mod.state(app.cfg, app._qbit()))
                except (ApiError, OSError) as e:
                    return self._json({**retention_mod.state(app.cfg, None), "warning": str(e)})
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
                if path == "/api/retention/sweep":
                    # the Settings button: preview by default, remove only when asked to.
                    # Both work while retention is still off - seeing what would go is how
                    # you decide whether to switch it on, and the age can be tried without
                    # saving it.
                    dry = bool(body.get("dry_run", True))
                    # 0 is a real age ("everything"), so only a missing value falls back
                    days = None if body.get("days") is None else float(body["days"])
                    qb = app._qbit()
                    out = retention_mod.sweep(app.cfg, qb, log=app._retention_log,
                                              dry_run=dry, force=True, days=days)
                    return self._json({**retention_mod.state(app.cfg, qb, days), **out})
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
                    show = metadata.tvmaze_show(int(body["show_id"]))
                    return self._json(episodes_mod.seasons_summary(show, self._source(body)))
                if path == "/api/metadata/clear_cache":
                    return self._json({"removed": episodes_mod.clear_cache()})
                if path == "/api/season/options":
                    return self.season_options(body)
                if path == "/api/season/grab":
                    return self.season_grab(body)
                if path == "/api/series/grab":
                    return self.series_grab(body)
                if path.startswith("/api/auto/"):
                    return self._json(self.auto(path.rsplit("/", 1)[1], body))
                if path == "/api/series/options":
                    return self.series_options(body)
                if path == "/api/folders":
                    return self.list_folders(body)
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

        def _source(self, body) -> str:
            src = (body.get("source") or app.cfg.episode_source or "all").lower()
            if src not in episodes_mod.SOURCES:
                raise ValueError(f"unknown episode source {src!r}")
            return src

        def _with_names(self, body, cfg=None):
            """The config with episode-name matching as the Seasons tab's checkbox says."""
            return dataclasses.replace(cfg or app.cfg, match_episode_names=bool(body.get("match_names")))

        def season_options(self, body):
            show = metadata.tvmaze_show(int(body["show_id"]))
            sn = int(body["season"])
            eps, used = episodes_mod.season_episodes(show, sn, self._source(body))
            wanted = [e["number"] for e in eps]
            pr = Prowlarr(app.cfg.prowlarr_url, app.cfg.prowlarr_key)
            cfg = self._with_names(body)
            opts = season_mod.find_options(cfg, pr, show["name"], sn, wanted, season_mod.names_for(cfg, show))
            return self._json({"show": show, "episodes": eps, "links": metadata.links(show),
                               "source": episodes_mod.SOURCES.get(used or "", ""),
                               "options": [o.summary(wanted) for o in opts]})

        def _library(self, body) -> tuple[bool, str | None]:
            """(skip owned episodes?, the show's folder) - the folder must lie inside one of the
            configured data folders."""
            if not body.get("skip_owned"):
                return False, None
            lib = (body.get("library") or "").strip() or None
            lib = local_folder(app.cfg, lib) if lib else None
            if lib and not within_roots(app.cfg, lib):
                raise ValueError("choose a folder inside one of the configured data folders")
            return True, lib

        def season_grab(self, body):
            sid, sn = int(body["show_id"]), int(body["season"])
            key = [str(k) for k in body.get("keys") or []] or body["key"]     # a priority list, or one
            packing = body.get("packing") or None
            label = body.get("label") or f"season {sn}"
            skip_owned, lib = self._library(body)
            source = self._source(body)

            def run(cfg):
                cfg = self._with_names(body, cfg)
                skip = None
                if skip_owned:
                    report.step("Looking for episodes you already have")
                    skip = series_mod.owned(cfg, metadata.tvmaze_show(sid), lib).get(sn, set())
                return season_mod.grab_season(cfg, sid, sn, key, packing, skip=skip, source=source)
            job = app.start_job(label, "season", run)
            return self._json({"id": job.id})

        def series_options(self, body):
            """Every release group/resolution over all seasons, for the priority list."""
            show = metadata.tvmaze_show(int(body["show_id"]))
            skip_owned, lib = self._library(body)
            source = self._source(body)
            lists, used = episodes_mod.episode_lists(show, source)
            cfg = self._with_names(body)
            have = series_mod.owned(cfg, show, lib) if skip_owned else {}
            pr = Prowlarr(app.cfg.prowlarr_url, app.cfg.prowlarr_key)      # searching only
            plans = series_mod.options_for(cfg, pr, show, lists, have)
            out = series_mod.summary(plans)
            for srow in out["seasons"]:
                srow["source"] = episodes_mod.SOURCES.get(used.get(srow["season"], ""), "")
            return self._json(out)

        def series_grab(self, body):
            sid = int(body["show_id"])
            packing = body.get("packing") or None
            plan_only = bool(body.get("plan_only"))
            skip_owned, lib = self._library(body)
            label = (body.get("label") or f"show {sid}") + (" - plan" if plan_only else " - all seasons")
            res = (body.get("res") or "").strip().lower() or None
            source = self._source(body)
            priority = [str(k) for k in body.get("priority") or []]
            job = app.start_job(label, "season", lambda cfg: series_mod.grab_series(
                self._with_names(body, cfg), sid, packing, library=lib, skip_owned=skip_owned, plan_only=plan_only, prefer_res=res,
                source=source, priority=priority))
            return self._json({"id": job.id})

        # ------------------------------------------------ the Automatic tab
        AUTO_FIELDS = {"enabled": ("auto_enabled", bool), "folder": ("auto_folder", str),
                       "autobrr_folder": ("auto_autobrr_folder", str), "wait_hours": ("auto_wait_hours", float),
                       "retry_minutes": ("auto_retry_minutes", float), "start": ("auto_start", bool),
                       "parallel": ("auto_parallel", int), "searches_per_hour": ("auto_searches_per_hour", int),
                       "autobrr_url": ("autobrr_url", str), "autobrr_key": ("autobrr_key", str)}

        def _auto_state(self):
            cfg = app.cfg
            box = getattr(app, "inbox", None)
            return {"settings": {k: getattr(cfg, f) for k, (f, _) in self.AUTO_FIELDS.items() if k != "autobrr_key"},
                    "has_key": bool(cfg.autobrr_key), "items": box.items() if box else []}

        def _autobrr(self, body=None):
            url = ((body or {}).get("autobrr_url") or app.cfg.autobrr_url or "").strip()
            key = ((body or {}).get("autobrr_key") or app.cfg.autobrr_key or "").strip()
            if not url or not key:
                raise ValueError("enter autobrr's URL and API key (autobrr: Settings, API keys)")
            return Autobrr(url, key)

        def auto(self, what, body):
            cfg = app.cfg
            if what == "state":
                return self._auto_state()
            if what == "save":
                changes = {}
                for k, (field, typ) in self.AUTO_FIELDS.items():
                    if k in body and not (k == "autobrr_key" and not body[k]):
                        changes[field] = typ(body[k]) if typ is not bool else bool(body[k])
                if changes.get("auto_folder"):
                    changes["auto_folder"] = local_folder(cfg, changes["auto_folder"])
                if changes.get("auto_enabled") and not (changes.get("auto_folder") or cfg.auto_folder):
                    raise ValueError("set the inbox folder first")
                new = dataclasses.replace(cfg, **changes)
                if new.auto_folder:
                    os.makedirs(new.auto_folder, exist_ok=True)
                config_mod.save(new)
                app.cfg = new
                return self._auto_state()
            if what == "detect":
                found = inbox_mod.detect_autobrr_folder()
                if not found:
                    raise ValueError("no autobrr container found on this machine - enter both paths yourself")
                return {"folder": found[0], "autobrr_folder": found[1]}
            if what == "filters":
                ab = self._autobrr(body)
                folder = body.get("autobrr_folder") or cfg.auto_autobrr_folder
                out = []
                for f in ab.filters():
                    full = ab.filter(f["id"])
                    ours = ab.ours(full, folder)
                    out.append({"id": f["id"], "name": f.get("name", ""), "enabled": f.get("enabled", True),
                                "indexers": [i.get("name") or i.get("identifier") for i in full.get("indexers") or []],
                                # actions that neither nzb2seed nor a download client: shown as "other"
                                "actions": [a.get("name") or a.get("type") for a in full.get("actions") or []
                                            if a not in ours and a not in ab.grabbers(full)],
                                "ours": bool(ours), "folder_ok": all(a.get("watch_folder") == folder for a in ours),
                                "grabbers": [{"id": a["id"], "name": a.get("name") or a.get("type"),
                                              "enabled": bool(a.get("enabled"))} for a in ab.grabbers(full)]})
                return {"version": ab.version(), "filters": out}
            if what == "apply":
                folder = cfg.auto_autobrr_folder
                if not folder:
                    raise ValueError("set the folder as autobrr sees it first")
                ab = self._autobrr(body)
                want = {int(x) for x in body.get("filters") or []}
                replace = {int(k): bool(v) for k, v in (body.get("replace") or {}).items()}
                memo = app.autobrr_memo()
                switched, were = memo["switched_off"], memo["filters_were"]
                added = removed = fixed = off = back = on = stopped = 0
                for f in ab.filters():
                    fid, was = f["id"], bool(f.get("enabled"))
                    full = ab.filter(f["id"])
                    ours = ab.ours(full, folder)
                    if f["id"] in want and not ours:
                        ab.add_action(f["id"], folder)
                        added += 1
                    elif f["id"] in want:
                        for a in ours:
                            if a.get("watch_folder") != folder:
                                ab.set_folder(a, folder)
                                fixed += 1
                    else:
                        for a in ours:
                            ab.delete_action(a["id"])
                            removed += 1
                    # the filter's own download actions: off while nzb2seed builds it from
                    # Usenet, back on (the ones nzb2seed switched off) when it stops
                    for a in ab.grabbers(full):
                        if fid in want and replace.get(fid, True) and a.get("enabled"):
                            ab.toggle(a["id"])
                            switched.append(a["id"])
                            off += 1
                        elif (fid not in want or not replace.get(fid, True)) and a["id"] in switched \
                                and not a.get("enabled"):
                            ab.toggle(a["id"])
                            switched.remove(a["id"])
                            back += 1
                    # the filter itself: enabled in autobrr while nzb2seed builds its releases,
                    # and disabled there when you untick it - so unticking always stops the
                    # filter, never leaves autobrr acting on it on its own
                    if fid in want:
                        were.setdefault(str(fid), was)
                        if not was:
                            ab.set_enabled(fid, True)
                            on += 1
                    elif str(fid) in were:
                        if was:
                            ab.set_enabled(fid, False)
                            stopped += 1
                        del were[str(fid)]
                app.autobrr_memo({"switched_off": switched, "filters_were": were})
                return {"added": added, "removed": removed, "fixed": fixed, "off": off, "back": back,
                        "on": on, "stopped": stopped}
            if what == "retry":
                app.inbox.retry(str(body.get("infohash")))
                return self._auto_state()
            if what == "forget":
                app.inbox.forget(str(body.get("infohash")))
                return self._auto_state()
            raise ValueError(f"unknown request {what!r}")

        def list_folders(self, body):
            roots = data_roots(app.cfg)
            path = (body.get("path") or "").strip()
            if not path:
                return self._json({"path": "", "parent": None, "roots": roots,
                                   "dirs": [{"name": r, "path": r} for r in roots]})
            if not within_roots(app.cfg, path) or not os.path.isdir(path):
                return self._err("not a folder inside the configured data folders")
            real = os.path.realpath(path)
            try:
                names = sorted((n for n in os.listdir(real)
                                if not n.startswith(".") and os.path.isdir(os.path.join(real, n))), key=str.casefold)
            except OSError as e:
                return self._err(f"cannot list {path}: {e.strerror}")
            parent = os.path.dirname(real)
            return self._json({"path": real, "roots": roots,
                               "parent": parent if within_roots(app.cfg, parent) else "",
                               "dirs": [{"name": n, "path": os.path.join(real, n)} for n in names]})

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
            sources = [local_folder(app.cfg, s) for s in body.get("sources", []) if s.strip()]
            if not sources:
                return self._err("add at least one folder holding the NZB download")
            for src in sources:
                if not os.path.isdir(src):
                    return self._err(f"{src} does not exist on the NAS")
            tpath = (body.get("torrent_path") or "").strip()
            tpath = local_folder(app.cfg, tpath) if tpath else tpath
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
    app.start_inbox()
    app.start_retention()
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
