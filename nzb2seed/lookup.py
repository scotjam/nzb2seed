"""What the tracker says about a release: when it was posted, and who wants it.

autobrr hands nzb2seed a .torrent and nothing else, so the things worth writing rules
about - how old the release is, how many people are on it - have to be asked for. Prowlarr
knows: a search result carries ``publishDate``, ``seeders``, ``leechers`` and ``grabs``.

Asking costs an indexer search, so it is only done when a rule actually needs it, and the
answer is kept. One search answers everything at once, so there is one lifetime for the
lot: two hours, which is short enough for a seeder count to be worth acting on and long
enough that a busy inbox is not spending a search per release.

The exception costs nothing: **when a release was posted cannot change**, so a rule that
only asks about age is answered from however old an entry, without a new search.

nzb2seed still never talks to an indexer itself - Prowlarr does the asking, as it does for
every other search.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

_lock = threading.Lock()
FRESH_FOR = 2 * 3600.0       # how long an answer is believed before asking again
KEEP_FOR = 30 * 86400        # how long a cached answer is kept at all


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def when(s) -> float:
    """An ISO timestamp as a unix time, or 0."""
    if not s:
        return 0.0
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


@dataclass
class Info:
    """What a tracker knows about one release."""
    published: float = 0.0       # unix time it appeared on the tracker
    seeders: int = -1            # -1 = not known
    leechers: int = -1
    grabs: int = -1
    indexer: str = ""
    asked: float = 0.0           # when this was looked up

    @property
    def age_min(self) -> float:
        return (time.time() - self.published) / 60 if self.published else 0.0

    @property
    def stale(self) -> bool:
        return time.time() - self.asked > FRESH_FOR


class Cache:
    """The answers, in a file, so a restart does not pay for them again."""

    def __init__(self, path: str):
        self.path = path
        self._mem: dict[str, Info] | None = None

    def _load(self) -> dict[str, Info]:
        if self._mem is not None:
            return self._mem
        out: dict[str, Info] = {}
        try:
            with open(self.path, encoding="utf-8") as fh:
                for key, row in (json.load(fh) or {}).items():
                    out[key] = Info(**row)
        except (OSError, ValueError, TypeError):
            out = {}
        self._mem = out
        return out

    def _save(self):
        rows = {k: asdict(v) for k, v in (self._mem or {}).items()
                if time.time() - v.asked < KEEP_FOR}
        tmp = f"{self.path}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(rows, fh)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def get(self, name: str) -> Info | None:
        return self._load().get(_norm(name))

    def put(self, name: str, info: Info):
        with _lock:
            self._load()[_norm(name)] = info
            self._save()

    def clear(self) -> int:
        with _lock:
            n = len(self._load())
            self._mem = {}
            self._save()
            return n


def find(pr, cache: Cache, name: str, want_counts: bool = False, log=None) -> Info | None:
    """Ask Prowlarr about this release, or hand back what was asked before.

    A cached answer is reused outright unless ``want_counts`` and its seeder count has gone
    stale; the posting date alone is always worth reusing, because it cannot change."""
    held = cache.get(name)
    if held and not (want_counts and held.stale):
        return held
    if pr is None:
        return held
    try:
        hits = [r for r in pr.search(name, [], []) if _norm(r.title) == _norm(name)]
    except Exception as e:                      # a lookup must never stop a build
        if log:
            log(f"could not ask Prowlarr about {name}: {e}")
        return held
    if not hits:
        info = Info(asked=time.time())          # remember the miss, so it is not asked again
        cache.put(name, info)
        return info
    best = max(hits, key=lambda r: (r.seeders or 0, r.grabs or 0))
    info = Info(published=when(best.publish_date),
                seeders=best.seeders if best.seeders is not None else -1,
                leechers=getattr(best, "leechers", None) if getattr(best, "leechers", None) is not None else -1,
                grabs=best.grabs if best.grabs is not None else -1,
                indexer=best.indexer or "", asked=time.time())
    cache.put(name, info)
    return info
