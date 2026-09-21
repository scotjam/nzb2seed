"""Which releases to chase first, and which never to chase.

Two lists, both yours:

* **priority rules**, in order. The first rule a release matches decides how far up the
  queue it goes, so a rule near the top is built before anything below it. A release that
  matches nothing is still built, at the back - unless you tick "only build what a rule
  matches", which turns the list into the whole appetite.
* **the blocklist**, which is checked first. Anything it matches is not built at all.

A rule is a set of conditions, all of which have to hold; inside one condition any of the
values will do. Leave a condition empty and it does not constrain anything:

    trackers      any of these trackers, or anything     ["tracker.example.org"]
                  under them (some trackers announce
                  from a new subdomain each time)
    types         any of these kinds of release         ["movie", "tv season"]
    groups        any of these release groups           ["REMUXGRP", "OTHERGRP"]
    categories    any of these qBittorrent categories   ["kids tv"]
    keywords      the name contains any of these        ["remux", "2160p"]
    not_keywords  the name contains none of these       ["german", "hdcam"]
    min_gb        at least this big                     20
    max_gb        no bigger than this                   80
    max_age_min   this new, in minutes since it appeared on the tracker   30
    min_seeders   at least this many people already on it                  5
    max_seeders   no more than this many (a swarm nobody needs)            40
    min_leechers  at least this many people still wanting it               3
    max_leechers  no more than this many
    min_grabs     grabbed at least this many times                         10
    max_grabs     no more than this many

The age, seeders and grabs come from Prowlarr, which knows what the tracker published and
who is on it. They are only looked up when a rule asks for them, and the answer is cached.

The age is what a tracker's own ratio economy rewards: being early on a release is worth
more than the release itself, so "under 30 minutes old" is usually the strongest rule
anyone writes.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import worth

GB = worth.GB
COUNTS = ("seeders", "leechers", "grabs")
CONDITIONS = (("trackers", "types", "groups", "categories", "keywords", "not_keywords",
               "min_gb", "max_gb", "max_age_min")
              + tuple(f"{edge}_{what}" for what in COUNTS for edge in ("min", "max")))
# these need Prowlarr to be asked about the release
FROM_TRACKER = ("max_age_min",) + tuple(f"{e}_{w}" for w in COUNTS for e in ("min", "max"))


@dataclass
class Release:
    """What a rule is matched against. ``age_min`` is how long ago it appeared."""
    name: str
    size: int
    tracker: str = ""
    category: str = ""
    first_seen: float = 0.0
    published: float = 0.0        # when the tracker posted it, if Prowlarr was asked
    seeders: int = -1             # -1 = not known
    leechers: int = -1
    grabs: int = -1

    @property
    def age_min(self) -> float:
        """How old the release is. The tracker's own posting time when it is known, and
        otherwise when it reached the inbox - which is a few seconds later at worst."""
        at = self.published or self.first_seen
        return (time.time() - at) / 60 if at else 0.0

    @property
    def type(self) -> str:
        return worth.kind_of(self.name)

    @property
    def group(self) -> str:
        return worth.group_of(self.name)


def _any_of(values, wanted) -> bool:
    return not wanted or any(str(v).strip().lower() == str(values).strip().lower()
                             for v in wanted)


def _any_host(host: str, wanted) -> bool:
    """A tracker condition matches the host itself or anything under it, because some
    trackers announce from a different subdomain every time."""
    if not wanted:
        return True
    host = (host or "").strip().lower()
    for v in wanted:
        v = str(v).strip().lower().lstrip(".")
        if v and (host == v or host.endswith("." + v)):
            return True
    return False


def matches(rule: dict, rel: Release, now: float | None = None) -> bool:
    """Does this release satisfy every condition the rule sets?"""
    name = (rel.name or "").lower()
    if not _any_host(rel.tracker, rule.get("trackers")):
        return False
    if not _any_of(rel.type, rule.get("types")):
        return False
    if not _any_of(rel.group, rule.get("groups")):
        return False
    if not _any_of(rel.category, rule.get("categories")):
        return False
    words = [w.strip().lower() for w in (rule.get("keywords") or []) if w.strip()]
    if words and not any(w in name for w in words):
        return False
    banned = [w.strip().lower() for w in (rule.get("not_keywords") or []) if w.strip()]
    if banned and any(w in name for w in banned):
        return False
    if rule.get("min_gb") and rel.size < float(rule["min_gb"]) * GB:
        return False
    if rule.get("max_gb") and rel.size > float(rule["max_gb"]) * GB:
        return False
    if rule.get("max_age_min"):
        at = rel.published or rel.first_seen
        age = ((now or time.time()) - at) / 60 if at else 0.0
        if age > float(rule["max_age_min"]):
            return False
    # a count nzb2seed could not find out never refuses a release
    for what in COUNTS:
        have = getattr(rel, what, -1)
        if have < 0:
            continue
        low, high = rule.get(f"min_{what}"), rule.get(f"max_{what}")
        if low and have < int(low):
            return False
        if high and have > int(high):
            return False
    return True


def describe(rule: dict) -> str:
    """The rule in words, for a log line."""
    bits = []
    for key, label in (("trackers", "on"), ("types", "a"), ("groups", "from"),
                       ("categories", "in"), ("keywords", "named like")):
        if rule.get(key):
            bits.append(f"{label} {' or '.join(str(x) for x in rule[key])}")
    if rule.get("not_keywords"):
        bits.append("not named like " + " or ".join(str(x) for x in rule["not_keywords"]))
    if rule.get("min_gb"):
        bits.append(f"at least {rule['min_gb']:g} GB")
    if rule.get("max_gb"):
        bits.append(f"at most {rule['max_gb']:g} GB")
    if rule.get("max_age_min"):
        bits.append(f"under {rule['max_age_min']:g} minutes old")
    for what in COUNTS:
        low, high = rule.get(f"min_{what}"), rule.get(f"max_{what}")
        if low and high:
            bits.append(f"{int(low)} to {int(high)} {what}")
        elif low:
            bits.append(f"at least {int(low)} {what}")
        elif high:
            bits.append(f"at most {int(high)} {what}")
    return (rule.get("name") or "rule") + (": " + ", ".join(bits) if bits else ": anything")


@dataclass
class Decision:
    build: bool
    priority: int                     # 0 is the front of the queue; higher is further back
    why: str = ""
    rule: dict | None = field(default=None)


def decide(cfg, rel: Release, now: float | None = None) -> Decision:
    """Block, prioritise, or leave it to the back of the queue.

    The blocklist is checked first, so a block always wins over a priority rule however
    high that rule sits."""
    for rule in cfg.demand_block or []:
        if rule.get("enabled", True) and matches(rule, rel, now):
            return Decision(False, 0, "blocked by " + describe(rule), rule)
    order = [r for r in (cfg.demand_rules or []) if r.get("enabled", True)]
    for i, rule in enumerate(order):
        if matches(rule, rel, now):
            return Decision(True, i, f"priority {i + 1}: " + describe(rule), rule)
    if getattr(cfg, "demand_only_rules", False) and order:
        return Decision(False, len(order), "no priority rule matches it, and only what a "
                                           "rule matches is being built")
    return Decision(True, len(order), "")


def needs_lookup(cfg) -> bool:
    """Does any rule ask for something only the tracker knows? If not, nothing is looked
    up and no search is spent."""
    for rule in list(cfg.demand_rules or []) + list(cfg.demand_block or []):
        if rule.get("enabled", True) and any(rule.get(k) for k in FROM_TRACKER):
            return True
    return False


def wants_counts(cfg) -> bool:
    """Seeders and grabs go stale; the posting time does not. Only ask again for the first."""
    for rule in list(cfg.demand_rules or []) + list(cfg.demand_block or []):
        if rule.get("enabled", True) and any(rule.get(f"{e}_{w}")
                                             for w in COUNTS for e in ("min", "max")):
            return True
    return False


def sort_key(cfg, rel: Release, arrived: float, now: float | None = None):
    """Queue order: priority first, then whichever arrived earlier."""
    return (decide(cfg, rel, now).priority, arrived)


# Rules about a tracker are not proposed. The ratio worth building is the one on the
# trackers you are weakest on, and "chase the tracker that already pays" spends your
# effort where it is least needed - while "block the tracker that does not" gives up on
# exactly the place the work was for. What a release *is* - its kind, its size, its group,
# how fresh it is, who is waiting for it - travels across trackers, so that is what these
# propose. A tracker rule can still be written by hand.
NOT_PROPOSED = ("tracker", "tracker+type")


def propose(stats: list[dict], least: int = 8, good: float = 0.75, dead_max: int = 20,
            top: int = 12) -> list[dict]:
    """Rules worth *adding*, from the kinds of release that pay best - never from a tracker.

    Each proposal carries the numbers behind it, and says nothing about where a release
    came from, only what it is."""
    out = []
    for row in stats:
        if row["what"] in NOT_PROPOSED:
            continue
        if row["n"] < least or row["ratio"] < good or row["dead"] > dead_max:
            continue
        kind, value = row["what"], row["value"]
        rule = {"name": f"{value} ({row['ratio']:.2f}x)", "enabled": True}
        if kind == "tracker":
            rule["trackers"] = [value]
        elif kind == "type":
            rule["types"] = [value]
        elif kind == "group":
            rule["groups"] = [value]
        elif kind == "size":
            lo = {"<1G": 0, "1-5G": 1, "5-15G": 5, "15-30G": 15, "30-60G": 30, ">60G": 60}
            rule["min_gb"] = lo.get(value, 0)
        else:
            continue
        out.append({**row, "rule": rule})
    out.sort(key=lambda r: (-r["ratio"], r["dead"]))
    return out[:top]
