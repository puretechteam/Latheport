"""Watch folders: a file that lands in a watched folder is converted into the folder's
``Converted`` subfolder, once it has stopped growing and no other program is writing it (a download
or a copy still under way is left alone until it's whole). The originals are never touched, and files that were there before
watching started are left as they are.

The window polls each folder every second (gui.py); ``latheport watch`` runs the same loop in a
terminal until Ctrl+C."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

OUT_DIR = "Converted"
SETTLE_S = 2.0  # a file whose size and time haven't changed for this long is whole
# names that are still being written: browsers' and torrent clients' partial downloads, Office's lock files
PARTIAL = (".crdownload", ".part", ".partial", ".download", ".tmp", ".!ut", ".opdownload")


@dataclass
class Watch:
    folder: Path
    target: str
    options: dict = field(default_factory=dict)

    @property
    def out(self) -> Path:
        return self.folder / OUT_DIR


def _busy(path: Path) -> bool:
    """Is another program still writing it? Windows' own copy sets the full size at the start and the
    time at the end, so a big file being copied looks finished; asking to write is refused until the
    copy lets go. (Nothing is written, and a read-only file is only asked to be read.)"""
    try:
        with open(path, "r+b" if os.access(path, os.W_OK) else "rb"):
            return False
    except OSError:
        return True


def _ignored(name: str) -> bool:
    low = name.lower()
    return low.startswith(("~$", ".")) or low.endswith(PARTIAL) or low in ("desktop.ini", "thumbs.db")


class Watcher:
    """What's new in one folder. poll() returns the files that are ready to convert, each once
    (again only if it's replaced by a changed file of the same name)."""

    def __init__(self, watch: Watch, clock: Callable[[], float] = time.monotonic, settle: float = SETTLE_S):
        self.watch, self.clock, self.settle = watch, clock, settle
        self.done: dict[str, tuple] = dict(self._scan())  # what was there already: left alone
        self.waiting: dict[str, tuple[tuple, float]] = {}  # name -> (signature, since)

    def _scan(self):
        try:
            with os.scandir(self.watch.folder) as it:
                for entry in it:
                    if _ignored(entry.name):
                        continue
                    try:
                        if not entry.is_file():
                            continue
                        st = entry.stat()
                    except OSError:
                        continue
                    yield entry.name, (st.st_size, st.st_mtime_ns)
        except OSError:  # the folder was removed or renamed: nothing to do until it's back
            return

    def poll(self) -> list[Path]:
        now = self.clock()
        current = dict(self._scan())
        ready = []
        for name in list(self.done):
            if name not in current:
                del self.done[name]
        for name in list(self.waiting):
            if name not in current:
                del self.waiting[name]
        for name, sig in current.items():
            if self.done.get(name) == sig:
                continue
            seen = self.waiting.get(name)
            if seen is None or seen[0] != sig:
                self.waiting[name] = (sig, now)  # new, or still changing: wait for it to rest
            elif now - seen[1] >= self.settle and sig[0] > 0:
                if _busy(self.watch.folder / name):
                    self.waiting[name] = (sig, now)
                    continue
                del self.waiting[name]
                self.done[name] = sig
                ready.append(self.watch.folder / name)
        return sorted(ready)


def run(watches: list[Watch], convert: Callable[[Path, Watch], None], stop: Callable[[], bool],
        interval: float = 1.0, sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic) -> None:
    """Poll every watch until stop() is true, handing each ready file to convert(path, watch)."""
    watchers = [Watcher(w, clock) for w in watches]
    while not stop():
        for w in watchers:
            for path in w.poll():
                if stop():
                    return
                convert(path, w.watch)
        sleep(interval)
