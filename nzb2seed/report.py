"""Where pipeline output goes: the console by default, or a GUI job.

The sink is per-thread, so each GUI job thread reports into its own log.
"""
from __future__ import annotations

import threading


class Cancelled(Exception):
    pass


class ConsoleSink:
    def __init__(self):
        self._progress = False

    def emit(self, kind: str, text: str):
        self.end_progress()
        prefix = {"step": "\n==> ", "info": "    ", "warn": "    ! ", "line": ""}[kind]
        print(prefix + text, flush=True)

    def progress(self, text: str):
        print("\r    " + text[:146].ljust(146), end="", flush=True)
        self._progress = True

    def end_progress(self):
        if self._progress:
            print()
            self._progress = False

    def pieces(self, states: str):
        pass

    def cancelled(self) -> bool:
        return False


_local = threading.local()
_console = ConsoleSink()


def sink():
    return getattr(_local, "sink", None) or _console


def use(s):
    _local.sink = s


def step(text: str):
    sink().emit("step", text)


def info(text: str):
    sink().emit("info", text)


def warn(text: str):
    sink().emit("warn", text)


def line(text: str):
    """A pre-formatted line (e.g. from assemble's move/delete log)."""
    sink().emit("line", text)


def progress(text: str):
    """A status line that replaces the previous one."""
    sink().progress(text)


def end_progress():
    sink().end_progress()


def pieces(states: str):
    """Piece map: one char per piece - '.' unchecked, '#' verified, 'x' failed."""
    sink().pieces(states)


def check_cancel():
    if sink().cancelled():
        raise Cancelled("cancelled")
