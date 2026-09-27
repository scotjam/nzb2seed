"""What changed, for the pages that are open: "jobs", "auto" or "settings".

Something that changes publishes its topic; the page's live feed (/api/events) waits for
a change and tells the page which topics changed, and the page fetches just those. A
burst of changes - a build writing its log line after line - reaches the page as one.
"""
from __future__ import annotations

import threading


class Events:
    def __init__(self):
        self.cond = threading.Condition()
        self.version = 0
        self.last: dict[str, int] = {}          # topic -> version it last changed at

    def publish(self, topic: str):
        with self.cond:
            self.version += 1
            self.last[topic] = self.version
            self.cond.notify_all()

    def wait(self, since: int, timeout: float) -> tuple[int, list[str]]:
        """Block until something changed after ``since`` (or ``timeout`` seconds pass):
        (the version now, the topics that changed since then)."""
        with self.cond:
            self.cond.wait_for(lambda: self.version > since, timeout)
            return self.version, sorted(t for t, v in self.last.items() if v > since)
