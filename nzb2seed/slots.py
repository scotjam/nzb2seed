"""Build slots handed out in a known order.

A plain semaphore gives a freed slot to whichever waiting build happens to look first,
so the Jobs tab could not show the order builds will start in. Here each waiting build
takes a place in line, and a slot goes to the first in line: by rank when one is given
(automatic builds: the newest torrent first, as fresh ones are where the upload is), and
in the order they came otherwise (builds started by hand).
"""
from __future__ import annotations

import itertools
import threading


# Paused by you (the Jobs tab's Pause): no build takes a slot until it is cleared - in any
# line, automatic or by hand - and a running build stops before its next big step
# (space.wait_for_room). Set and cleared by the GUI, which keeps it across restarts.
PAUSED = threading.Event()


class FairSlots:
    """``size`` builds at once; the rest wait in line, each with the job it is for."""

    def __init__(self, size: int):
        self.size = max(1, int(size))
        self.used = 0
        self._cond = threading.Condition()
        self._line: list[tuple[float, int, object]] = []   # (rank, ticket, who), first in line first
        self._seq = itertools.count(1)
        self._front: dict[int, int] = {}     # ticket -> when you moved it to the top (later: higher)
        self._forced: set[int] = set()       # tickets you started now, past the slot limit
        self._fseq = itertools.count(1)

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
    def enter(self, who: object = None, rank: float = 0.0) -> int:
        """A place in line: behind everyone of a lower or the same rank (the same rank:
        in the order they came)."""
        with self._cond:
            t = next(self._seq)
            self._line.append((rank, t, who))
            self._order()
            self._cond.notify_all()
            return t

    def _order(self):
        # moved to the top by you first (the latest move on top), then by rank, then as they came
        self._line.sort(key=lambda x: (-self._front.get(x[1], 0), x[0], x[1]))

    def take(self, ticket: int, timeout: float | None = None) -> bool:
        """The slot, once this place is first in line and one is free (False: not yet)."""
        with self._cond:
            ok = self._cond.wait_for(
                lambda: not PAUSED.is_set() and (ticket in self._forced or (
                    self.used < self.size and self._line and self._line[0][1] == ticket)), timeout)
            if not ok:
                return False
            self._drop(ticket)
            self.used += 1
            self._cond.notify_all()                    # the next in line may fit too
            return True

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

    def to_front(self, who: object) -> bool:
        """Move who is waiting to the top of the line: next to start when a slot frees up
        (False: not waiting in this line)."""
        with self._cond:
            t = self._ticket(who)
            if t is None:
                return False
            self._front[t] = next(self._fseq)
            self._order()
            self._cond.notify_all()
            return True

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
