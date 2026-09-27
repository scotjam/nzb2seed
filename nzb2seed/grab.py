"""Getting .nzb files without nzb2seed ever talking to an indexer.

Prowlarr always answers a Usenet download with a redirect to the indexer itself, so
the .nzb has to be fetched by something. That is SABnzbd (the download client, in the
same network as the other services): it is handed Prowlarr's link as a *paused* job,
fetches the NZB, and keeps a copy in the job's admin folder, where nzb2seed reads it
(to rank the post, compare it with posts tried before, or trim it to one file).

The job is then deleted straight away - nothing of it was downloaded - and the NZB is
kept here. A post that turns out to be wanted is added from those bytes, so the indexer
is never asked twice, and a build weighing a dozen candidates leaves nothing parked in
the queue while it decides.
"""
from __future__ import annotations

import glob
import gzip
import hashlib
import os
import re
import secrets
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .clients import PP_REPAIR, ApiError, Prowlarr, Release, SABnzbd
from .config import Config
from .pathmap import map_path
from .report import check_cancel

CHECK_PREFIX = "nzb2seed-check-"
FETCH_TIMEOUT = 180

# Every paused NZB-check job this process is holding, across all builds. Builds run side
# by side, and the sweep below deletes check jobs a killed build left behind - without
# this it would delete the ones a build running right now is waiting on, which looks like
# "it disappeared from the queue" to that build.
_held: set[str] = set()
_held_lock = threading.Lock()


def _hold_on(nzo: str):
    with _held_lock:
        _held.add(nzo)


def _let_go(nzo: str | None):
    if nzo:
        with _held_lock:
            _held.discard(nzo)


# query parameters that carry a key and must never be shown or logged
SECRET_PARAMS = {"apikey", "api_key", "r", "i", "passkey", "token", "key", "sig"}


def page_of(rel: Release) -> str:
    """ " - its page: <url>" for a release's page on its indexer, or "" if it has none.
    Keys are stripped: some indexers put your API key in the links they return."""
    url = (getattr(rel, "info_url", "") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return ""
    parts = urlsplit(url)
    keep = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in SECRET_PARAMS]
    return " - its page: " + urlunsplit(parts._replace(query=urlencode(keep)))


def told_to_wait(slot: dict) -> bool:
    """Is SABnzbd waiting to retry fetching this URL? Its queue shows that only as a label,
    "WAIT 57 sec" (translated into SABnzbd's language, hence the loose match)."""
    return any(re.search(r"^\S+\s+\d+\s+\S+$", str(label)) and not str(label).upper().startswith("PROPAGAT")
               for label in slot.get("labels") or [])


class NzbStore:
    """Every NZB SABnzbd fetched, kept on disk, so a retry (or a later build wanting the
    same post) uses the one already here instead of asking the indexer again - many
    indexers refuse the same NZB twice in 24 hours, and each fetch counts against your
    daily limit. Kept for good: an NZB is small, and fetching it again is what costs."""

    def __init__(self, root: str):
        self.root = root

    @classmethod
    def for_config(cls, cfg: Config) -> "NzbStore | None":
        if not getattr(cfg, "path", ""):
            return None
        return cls(os.path.join(os.path.dirname(os.path.abspath(cfg.path)), "cache", "nzb"))

    def _file(self, rel: Release) -> str:
        key = f"{rel.indexer}|{rel.guid}".encode("utf-8")
        return os.path.join(self.root, hashlib.sha1(key).hexdigest() + ".nzb.gz")

    def get(self, rel: Release) -> bytes | None:
        try:
            with gzip.open(self._file(rel)) as fh:
                data = fh.read()
        except (OSError, EOFError):
            return None
        return data or None

    def put(self, rel: Release, data: bytes):
        try:
            os.makedirs(self.root, exist_ok=True)
            path = self._file(rel)
            tmp = path + ".tmp"
            with gzip.open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        except OSError:
            pass                             # a cache: the build carries on without it


class Source:
    """Prowlarr for searching and .torrent files; SABnzbd for .nzb files.

    A post is looked at by having SABnzbd fetch its NZB as a paused job, reading the NZB
    SABnzbd saved, and then deleting that job again. The NZB is kept here, so a post that
    is wanted is downloaded from those same bytes - the indexer is never asked twice, and
    nothing is left parked in the queue in the meantime."""

    def __init__(self, cfg: Config, pr: Prowlarr, sab: SABnzbd):
        self.cfg, self.pr, self.sab = cfg, pr, sab
        self.held: dict[str, str] = {}       # release guid -> paused SABnzbd job holding its NZB
        self.data: dict[str, bytes] = {}     # release guid -> the NZB
        self.store = NzbStore.for_config(cfg)

    # Prowlarr's side, unchanged
    def search(self, *a, **kw):
        return self.pr.search(*a, **kw)

    def fetch(self, rel: Release) -> bytes:
        if rel.protocol != "usenet":
            return self.pr.fetch(rel)        # .torrent: Prowlarr fetches it itself
        if rel.guid not in self.data:
            kept = self.store.get(rel) if self.store else None
            if kept is not None:
                self.data[rel.guid] = kept   # fetched before: the indexer is not asked again
            else:
                self._hold(rel)
                if self.store:
                    self.store.put(rel, self.data[rel.guid])
        return self.data[rel.guid]

    def _hold(self, rel: Release):
        """Have SABnzbd fetch the NZB, parked. On failure the message links the release's
        page on its indexer, so the person can look (nzb2seed itself never opens it)."""
        name = CHECK_PREFIX + secrets.token_hex(4)
        nzo = self.sab.add_url(rel.download_url, name, self.cfg.sab_category, PP_REPAIR)
        self.held[rel.guid] = nzo
        _hold_on(nzo)
        end = time.time() + FETCH_TIMEOUT
        while True:
            check_cancel()
            slot = self.sab.queue_slot(nzo)
            if slot is None:
                h = self.sab.history_slot(nzo) or {}
                self.held.pop(rel.guid, None)
                _let_go(nzo)
                why = h.get("fail_message") or h.get("status") or "it disappeared from the queue"
                raise ApiError(f"SABnzbd could not get the NZB from {rel.indexer}: {why}{page_of(rel)}")
            if slot.get("status") != "Grabbing":
                path = self._nzb_path(name)
                if path:
                    break
            elif told_to_wait(slot):
                # SABnzbd only waits to retry a URL after the indexer (via Prowlarr) answered
                # with something that is not an NZB. Waiting out its retries would not
                # change that answer - and could count against the indexer's limit again
                self.drop(rel)
                raise ApiError(f"{rel.indexer} refused the NZB - SABnzbd got something other "
                               "than an NZB back and was going to try again later. This is "
                               "usually the indexer's limit, for instance the same NZB already "
                               "downloaded in the last 24 hours; it can also be a passing "
                               f"fault at the indexer{page_of(rel)}")
            if time.time() > end:
                self.drop(rel)
                raise ApiError(f"SABnzbd did not get the NZB from {rel.indexer} within "
                               f"{FETCH_TIMEOUT}s{page_of(rel)}")
            time.sleep(1)
        with gzip.open(path) as fh:
            self.data[rel.guid] = fh.read()
        # The NZB is read, so the job has done its work. Keeping it parked in the queue
        # was only ever a way to avoid asking the indexer twice - and the NZB is in hand
        # now, so a post that turns out to be wanted is added from these bytes instead.
        # Leaving them parked put one queue entry per candidate post in front of the
        # person, dozens of them once several builds run at once.
        self.held.pop(rel.guid, None)
        _let_go(nzo)
        try:
            self.sab.queue_do("delete", nzo)
        except ApiError:
            pass

    def _nzb_path(self, name: str) -> str | None:
        root = map_path(self.sab.incomplete_dir(), self.cfg.sab_to_local)
        hits = glob.glob(os.path.join(glob.escape(root), glob.escape(name) + "*", "__ADMIN__", "*.nzb.gz"))
        return hits[0] if hits else None

    def start(self, rel: Release, pp: int) -> str:
        """Download a post: the NZB already in hand is handed to SABnzbd as a job of its
        own. The indexer is not asked again - these are the bytes it sent the first time.
        Returns the SABnzbd job id."""
        nzb = self.fetch(rel)
        return self.sab.add_nzb(nzb, rel.title, self.cfg.sab_category, pp, self.cfg.sab_priority)

    def drop(self, rel: Release):
        """The post is not wanted: remove its paused job (nothing was downloaded)."""
        nzo = self.held.pop(rel.guid, None)
        _let_go(nzo)
        if nzo:
            try:
                self.sab.queue_do("delete", nzo)
            except ApiError:
                pass

    def close(self):
        """Remove every paused job still held (called when a build ends, however it ends)."""
        for guid in list(self.held):
            nzo = self.held.pop(guid)
            _let_go(nzo)
            try:
                self.sab.queue_do("delete", nzo)
            except ApiError:
                pass
        self.data.clear()


def remove_stray_checks(sab: SABnzbd) -> int:
    """Delete paused NZB-check jobs left behind by a build that was killed.

    Only jobs whose name nzb2seed gave them, only while nothing of them was downloaded,
    and never one a build running right now is holding - builds run side by side, and
    taking another build's job out from under it is indistinguishable, to that build,
    from the indexer failing."""
    try:
        q = sab._call("queue", limit="0").get("queue", {})
    except ApiError:
        return 0
    n = 0
    for s in q.get("slots", []):
        ours = str(s.get("filename", "")).startswith(CHECK_PREFIX)
        untouched = s.get("status") in ("Paused", "Grabbing") and str(s.get("percentage", "0")) in ("0", "0.0")
        with _held_lock:
            mine = s.get("nzo_id") in _held
        if ours and untouched and not mine:
            try:
                sab.queue_do("delete", s["nzo_id"])
                n += 1
            except ApiError:
                pass
    return n

