"""How often each release group, at each resolution, turns out to be on Usenet.

Every build that ends records it: found (built, or nearly built - the post was there) or
not found ("no Usenet post could supply it"). An automatic torrent from a group and
resolution never once found on Usenet after enough tries is stopped before it costs any
indexer search; ones likely to be found are built first. The table is kept in
usenet_odds.json next to the config and, the first time, seeded from the jobs and the
Automatic tab's history.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

from . import matching

MIN_TRIES = 5            # never found in this many tries: stop trying automatically
_RES = re.compile(r"(?<![a-z0-9])(2160|1080|720|576|480)p(?![a-z0-9])")
NOT_ON_USENET = re.compile(r"no Usenet post could supply|: none found|no post of this group", re.I)
_lock = threading.Lock()


def key(title: str) -> str | None:
    """"grpa|1080p" - None when the name shows no group (nothing to learn about)."""
    from .pipeline import group_of
    name = title[len("auto: "):] if title.startswith("auto: ") else title
    group = group_of(name)
    if not group:
        return None
    m = _RES.search(matching.norm(name))
    return f"{group.lower()}|{m.group(0) if m else 'any'}"


class Odds:
    def __init__(self, path: str):
        self.path = path

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict):
        tmp = f"{self.path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
        os.replace(tmp, self.path)

    def exists(self) -> bool:
        return os.path.exists(self.path)

    def record(self, title: str, found: bool):
        k = key(title)
        if not k:
            return
        with _lock:
            data = self._load()
            row = data.setdefault(k, {"found": 0, "missing": 0})
            row["found" if found else "missing"] += 1
            row["last"] = time.time()
            self._save(data)

    def rows(self) -> list[dict]:
        out = []
        for k, v in self._load().items():
            group, res = k.split("|", 1)
            n = v.get("found", 0) + v.get("missing", 0)
            out.append({"group": group, "res": res, "found": v.get("found", 0), "missing": v.get("missing", 0),
                        "rate": v.get("found", 0) / n if n else 0.0, "never": is_never(v)})
        return sorted(out, key=lambda r: (-(r["found"] + r["missing"]), r["group"], r["res"]))

    def of(self, title: str) -> dict:
        k = key(title)
        return self._load().get(k, {}) if k else {}

    def rate(self, title: str) -> float:
        """Likelihood it is on Usenet, with a gentle prior: an unknown group counts as even."""
        v = self.of(title)
        return (v.get("found", 0) + 1) / (v.get("found", 0) + v.get("missing", 0) + 2)

    def never(self, title: str) -> str | None:
        """Why not to try: the group and resolution were never found on Usenet in enough tries."""
        v = self.of(title)
        if is_never(v):
            return (f"{key(title).replace('|', ' ').upper()} has never been found on Usenet "
                    f"({v.get('missing', 0)} tries, none found)")
        return None

    def seed(self, outcomes: list[tuple[str, bool]]):
        """The first time: learn from the history there already is."""
        with _lock:
            if os.path.exists(self.path):
                return
            data: dict = {}
            for title, found in outcomes:
                k = key(title)
                if k:
                    row = data.setdefault(k, {"found": 0, "missing": 0})
                    row["found" if found else "missing"] += 1
            self._save(data)


def is_never(v: dict) -> bool:
    return v.get("found", 0) == 0 and v.get("missing", 0) >= MIN_TRIES


def odds_for(cfg) -> Odds:
    return Odds(os.path.join(os.path.dirname(os.path.abspath(cfg.path)), "usenet_odds.json"))
