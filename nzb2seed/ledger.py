"""Which SABnzbd jobs nzb2seed submitted - so a later build can reuse a finished
download of the same NZB instead of downloading it again, and so only nzb2seed's own
downloads are ever reused (and later cleaned up)."""
from __future__ import annotations

import json
import os
import threading
import time

_lock = threading.Lock()


class Ledger:
    def __init__(self, path: str):
        self.path = path

    def _load(self) -> list[dict]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                return json.load(fh).get("jobs", [])
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            return []

    def _save(self, jobs: list[dict]):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = f"{self.path}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"jobs": jobs}, fh, indent=1)
        os.replace(tmp, self.path)

    def record(self, nzo: str, title: str, guid: str = "", indexer: str = "", size: int | None = None,
               torrent: str = "", ids: list[str] | None = None):
        with _lock:
            jobs = [j for j in self._load() if j.get("nzo") != nzo]
            job = {"nzo": nzo, "title": title, "guid": guid, "indexer": indexer,
                   "size": size, "torrent": torrent, "submitted": time.time()}
            if ids:
                job["ids"] = ids            # nzbinfo.post_ids: which post this was
            jobs.append(job)
            self._save(jobs)

    def covering(self, ids: list[str]) -> list[dict]:
        """Earlier downloads that held every file of the post with these ``ids``, newest first."""
        want = set(ids)
        if not want:
            return []
        return [j for j in self.all() if j.get("ids") and want <= set(j["ids"])]

    def all(self) -> list[dict]:
        return sorted(self._load(), key=lambda j: -(j.get("submitted") or 0))

    def matches(self, title: str, guid: str = "", size: int | None = None) -> list[dict]:
        """Earlier submissions of the same NZB, newest first: same indexer entry (guid), or
        same title and size (the same post on another indexer). Entries recorded without a
        size match on the title alone - the files are verified before anything is reused."""
        out = []
        for j in self._load():
            if guid and j.get("guid") == guid:
                out.append(j)
            elif j.get("title") == title and (j.get("size") is None or size is None or j.get("size") == size):
                out.append(j)
        return sorted(out, key=lambda j: -(j.get("submitted") or 0))
