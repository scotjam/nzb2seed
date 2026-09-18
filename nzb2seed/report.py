"""Where pipeline output goes: the console by default, or a GUI job.

The sink is per-thread, so each GUI job thread reports into its own log.
"""
from __future__ import annotations

import sys
import threading

# Set by the CLI: may the console ask questions? (False with --yes or without a terminal)
interactive = False


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

    def ask(self, prompt: str, choices: list[dict]) -> int | None:
        self.end_progress()
        print(f"\n    ? {prompt}")
        for i, c in enumerate(choices, 1):
            note = f"   <- {c['note']}" if c.get("note") else ""
            print(f"  {i:>3}  {c['indexer'][:16]:16}  {c['size_text']:>10}  G:{c.get('grabs')}  {c['title']}{note}")
        if not interactive:
            print("    (not asking: running without a terminal or with --yes)")
            return None
        while True:
            try:
                s = input("    Number of the NZB to download, empty = stop: ").strip()
            except EOFError:
                return None
            if not s:
                return None
            if s.isdigit() and 1 <= int(s) <= len(choices):
                return int(s) - 1
            print(f"    choose between 1 and {len(choices)}")

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


def ask(prompt: str, choices: list[dict]) -> int | None:
    """Ask the person to pick one of ``choices``; None = stop. Blocks until answered."""
    return sink().ask(prompt, choices)


def check_cancel():
    if sink().cancelled():
        raise Cancelled("cancelled")
