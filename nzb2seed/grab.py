"""Getting .nzb files without nzb2seed ever talking to an indexer.

Prowlarr always answers a Usenet download with a redirect to the indexer itself, so
the .nzb has to be fetched by something. That is SABnzbd (the download client, in the
same network as the other services): it is handed Prowlarr's link as a *paused* job,
fetches the NZB, and keeps a copy in the job's admin folder, where nzb2seed reads it
(to rank the post, compare it with posts tried before, or trim it to one file). A post
nzb2seed then wants is simply resumed - no second grab from the indexer; the others are
deleted from the queue before anything is downloaded.
"""
from __future__ import annotations

import glob
import gzip
import os
import secrets
import time

from .clients import PP_REPAIR, ApiError, Prowlarr, Release, SABnzbd
from .config import Config
from .pathmap import map_path
from .report import check_cancel

CHECK_PREFIX = "nzb2seed-check-"
FETCH_TIMEOUT = 180


class Source:
    """Prowlarr for searching and .torrent files; SABnzbd for .nzb files."""

    def __init__(self, cfg: Config, pr: Prowlarr, sab: SABnzbd):
        self.cfg, self.pr, self.sab = cfg, pr, sab
        self.held: dict[str, str] = {}       # release guid -> paused SABnzbd job holding its NZB
        self.data: dict[str, bytes] = {}     # release guid -> the NZB

    # Prowlarr's side, unchanged
    def search(self, *a, **kw):
        return self.pr.search(*a, **kw)

    def fetch(self, rel: Release) -> bytes:
        if rel.protocol != "usenet":
            return self.pr.fetch(rel)        # .torrent: Prowlarr fetches it itself
        if rel.guid not in self.data:
            self._hold(rel)
        return self.data[rel.guid]

    def _hold(self, rel: Release):
        name = CHECK_PREFIX + secrets.token_hex(4)
        nzo = self.sab.add_url(rel.download_url, name, self.cfg.sab_category, PP_REPAIR)
        self.held[rel.guid] = nzo
        end = time.time() + FETCH_TIMEOUT
        while True:
            check_cancel()
            slot = self.sab.queue_slot(nzo)
            if slot is None:
                h = self.sab.history_slot(nzo) or {}
                self.held.pop(rel.guid, None)
                why = h.get("fail_message") or h.get("status") or "it disappeared from the queue"
                raise ApiError(f"SABnzbd could not get the NZB from {rel.indexer}: {why}")
            if slot.get("status") != "Grabbing":
                path = self._nzb_path(name)
                if path:
                    break
            if time.time() > end:
                self.drop(rel)
                raise ApiError(f"SABnzbd did not get the NZB from {rel.indexer} within {FETCH_TIMEOUT}s")
            time.sleep(1)
        with gzip.open(path) as fh:
            self.data[rel.guid] = fh.read()

    def _nzb_path(self, name: str) -> str | None:
        root = map_path(self.sab.incomplete_dir(), self.cfg.sab_to_local)
        hits = glob.glob(os.path.join(glob.escape(root), glob.escape(name) + "*", "__ADMIN__", "*.nzb.gz"))
        return hits[0] if hits else None

    def start(self, rel: Release, pp: int) -> str:
        """Download a post: the paused job holding its NZB is renamed, set to ``pp`` and
        resumed. Returns the SABnzbd job id."""
        nzo = self.held.pop(rel.guid, None)
        if nzo is None or self.sab.queue_slot(nzo) is None:
            self.data.pop(rel.guid, None)
            self.fetch(rel)
            nzo = self.held.pop(rel.guid)
        self.sab.queue_do("rename", nzo, rel.title)
        self.sab.ensure_pp(nzo, pp)
        self.sab.queue_do("priority", nzo, str(self.cfg.sab_priority))
        self.sab.queue_do("resume", nzo)
        return nzo

    def drop(self, rel: Release):
        """The post is not wanted: remove its paused job (nothing was downloaded)."""
        nzo = self.held.pop(rel.guid, None)
        if nzo:
            try:
                self.sab.queue_do("delete", nzo)
            except ApiError:
                pass

    def close(self):
        """Remove every paused job still held (called when a build ends, however it ends)."""
        for guid in list(self.held):
            nzo = self.held.pop(guid)
            try:
                self.sab.queue_do("delete", nzo)
            except ApiError:
                pass
        self.data.clear()


def remove_stray_checks(sab: SABnzbd) -> int:
    """Delete paused NZB-check jobs left behind by a build that was killed - only jobs
    whose name nzb2seed gave them, and only while nothing of them was downloaded."""
    try:
        q = sab._call("queue", limit="0").get("queue", {})
    except ApiError:
        return 0
    n = 0
    for s in q.get("slots", []):
        ours = str(s.get("filename", "")).startswith(CHECK_PREFIX)
        untouched = s.get("status") in ("Paused", "Grabbing") and str(s.get("percentage", "0")) in ("0", "0.0")
        if ours and untouched:
            try:
                sab.queue_do("delete", s["nzo_id"])
                n += 1
            except ApiError:
                pass
    return n

