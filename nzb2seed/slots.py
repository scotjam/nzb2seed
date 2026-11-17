"""Build slots handed out in a known order.

A plain semaphore gives a freed slot to whichever waiting build happens to look first,
so the Jobs tab could not show the order builds will start in. Here each waiting build
takes a place in line, and a slot goes to the first in line: by rank (automatic builds:
the newest torrent first, as fresh ones are where the upload is; builds started by hand:
their job number, so the order they were queued in - kept across a restart, when every
interrupted build comes back at once), then in the order they came.
"""
from __future__ import annotations

import itertools
import threading
import time


# Paused by you (the Jobs tab's Pause): no build takes a slot until it is cleared - in any
# line, automatic or by hand - and a running build stops before its next big step
# (space.wait_for_room). Set and cleared by the GUI, which keeps it across restarts.
PAUSED = threading.Event()

# A "Move to top" (when it was made) and a "Start now", kept on the job (job.extra) so a
# restart does not forget them: the build puts them back as it queues again.
QUEUE_MARKS = ("queue_front", "queue_forced")
# A build holding a slot says so on its job too ("slot", or "forced" if you started it
# past the limit): one a restart cut off part-way - SABnzbd already downloading for it -
# gets a slot back first, ahead of builds that had not started.
HOLDING = "queue_holding"


def queue_marks(job) -> dict:
    """A job's kept "Move to top" / "Start now" / slot it held, as FairSlots.enter takes them."""
    ex = (getattr(job, "extra", None) or {}) if job is not None else {}
    held = ex.get(HOLDING)
    return {"front": float("inf") if held else float(ex.get("queue_front") or 0),
            "forced": held == "forced" or bool(ex.get("queue_forced"))}


def _set_extra(job, ex: dict):
    job.extra = ex
    tell = getattr(job, "on_change", None)
    if callable(tell):
        tell()                                    # saved with the job (jobs.json)


def clear_queue_marks(job, forced: bool = False):
    """It has its slot: what was asked for it is done (a later wait queues afresh), and the
    job now says it holds one (see HOLDING)."""
    if job is None:
        return
    ex = {k: v for k, v in (getattr(job, "extra", None) or {}).items() if k not in QUEUE_MARKS}
    ex[HOLDING] = "forced" if forced else "slot"
    _set_extra(job, ex)


def let_go(job):
    """Its slot is handed back (finished, or waiting on you): nothing to take back first."""
    ex = getattr(job, "extra", None) if job is not None else None
    if ex and HOLDING in ex:
        _set_extra(job, {k: v for k, v in ex.items() if k != HOLDING})


class FairSlots:
    """``size`` builds at once; the rest wait in line, each with the job it is for."""

    def __init__(self, size: int):
        self.size = max(1, int(size))
        self.used = 0
        self._cond = threading.Condition()
        self._line: list[tuple[float, int, object]] = []   # (rank, ticket, who), first in line first
        self._seq = itertools.count(1)
        self._front: dict[int, float] = {}   # ticket -> when you moved it to the top (later: higher)
        self._forced: set[int] = set()       # tickets you started now, past the slot limit

    # ---- the same as a semaphore's, for code that only takes a slot if one is free
    def acquire(self, blocking: bool = True, timeout: float | None = None) -> bool:
        if not blocking:
            with self._cond:
                if PAUSED.is_set() or self._line or self.used >= self.size:
                    return False                       # never ahead of a build already waiting
                self.used += 1
                return True
        t = self.enter()
        try:
            if self.take(t, timeout):
                return True
        except BaseException:
            self.leave(t)
            raise
        self.leave(t)
        return False

    def release(self):
        with self._cond:
            self.used = max(0, self.used - 1)
            self._cond.notify_all()

    # ---- waiting in line
    def enter(self, who: object = None, rank: float = 0.0, front: float = 0.0, forced: bool = False) -> int:
        """A place in line: behind everyone of a lower or the same rank (the same rank:
        in the order they came). ``front``/``forced`` carry a "Move to top" (when it was
        made) or a "Start now" over from before a restart - see to_front and force."""
        with self._cond:
            t = next(self._seq)
            self._line.append((rank, t, who))
            if front:
                self._front[t] = front
            if forced:
                self._forced.add(t)
            self._order()
            self._cond.notify_all()
            return t

    def _order(self):
        # moved to the top by you first (the latest move on top), then by rank, then by job
        # number - the order they were queued in, which a restart keeps, as every interrupted
        # build queues again at once from its own thread - and only then as they came
        def job_no(who):
            n = getattr(who, "id", None)
            return n if isinstance(n, int) else float("inf")
        self._line.sort(key=lambda x: (-self._front.get(x[1], 0), x[0], job_no(x[2]), x[1]))

    def take(self, ticket: int, timeout: float | None = None) -> bool | str:
        """The slot, once this place is first in line and one is free (False: not yet;
        "forced" when it was started now, past the limit)."""
        with self._cond:
            ok = self._cond.wait_for(
                lambda: not PAUSED.is_set() and (ticket in self._forced or (
                    self.used < self.size and self._line and self._line[0][1] == ticket)), timeout)
            if not ok:
                return False
            forced = ticket in self._forced
            self._drop(ticket)
            self.used += 1
            self._cond.notify_all()                    # the next in line may fit too
            return "forced" if forced else True

    def leave(self, ticket: int):
        """Out of line without a slot (cancelled while waiting)."""
        with self._cond:
            self._drop(ticket)
            self._cond.notify_all()

    def _drop(self, ticket: int):
        self._line = [x for x in self._line if x[1] != ticket]
        self._front.pop(ticket, None)
        self._forced.discard(ticket)

    def _ticket(self, who: object) -> int | None:
        return next((t for _, t, w in self._line if w is who), None)

    def to_front(self, who: object, when: float | None = None) -> bool:
        """Move who is waiting to the top of the line: next to start when a slot frees up
        (False: not waiting in this line). ``when`` (wall-clock time, the latest move on
        top) is what the caller keeps, so the move survives a restart (enter's front)."""
        with self._cond:
            t = self._ticket(who)
            if t is None:
                return False
            when = time.time() if when is None else when
            # the latest move must sort on top even within one clock tick (Windows: ~15 ms)
            when = max([when] + [f + 1e-6 for f in self._front.values()])
            self._front[t] = when
            self._order()
            self._cond.notify_all()
            return True

    def front_of(self, who: object) -> float:
        """When who was moved to the top (0: it was not, or is not waiting here)."""
        with self._cond:
            t = self._ticket(who)
            return self._front.get(t, 0.0) if t is not None else 0.0

    def force(self, who: object) -> bool:
        """Start who is waiting now, as one more build on top of the slots in use; the
        builds in line still wait for a slot under the limit (False: not waiting here).
        Pause still holds it back, as it holds back everything."""
        with self._cond:
            t = self._ticket(who)
            if t is None:
                return False
            self._forced.add(t)
            self._cond.notify_all()
            return True

    def waiting(self) -> list:
        """Who is waiting, first in line first."""
        with self._cond:
            return [who for _, _, who in self._line]

    def line(self) -> list[tuple[float, object]]:
        """(rank, who) of everyone waiting, first in line first."""
        with self._cond:
            return [(r, who) for r, _, who in self._line]

    def resize(self, size: int):
        """Run this many at once from now on; builds running beyond it finish first."""
        with self._cond:
            self.size = max(1, int(size))
            self._cond.notify_all()
