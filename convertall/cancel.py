"""Stopping a conversion part-way: a flag the steps look at, and the engines it kills.

The window's job holds a Token and runs its conversions inside using(token). Steps call check()
where stopping is safe (between files, between route steps, between chunks of an extraction);
engines.run() hands every program it starts to track(), so cancel() ends them at once instead of
waiting for them to finish. The token also counts what an extraction has written (expect() and
moved()), for the window to show how far a big one has got."""

from __future__ import annotations

import contextlib
import contextvars
import subprocess
import sys
import threading
import time

from .errors import ConversionError


class Cancelled(ConversionError):
    """The user stopped it. Not a failure: nothing is reported as wrong with the file."""

    def __init__(self, message: str = "Stopped"):
        super().__init__(message)


class Token:
    def __init__(self):
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._procs: set[subprocess.Popen] = set()
        self.listener = None  # listener(done, total): bytes written so far, of what was expected
        self.expected = self.done = 0
        self._told = -1.0

    def expect(self, total: int) -> None:
        """The bytes the file being worked on will write (an archive's declared size)."""
        self.expected, self.done, self._told = total, 0, -1.0

    def moved(self, n: int) -> None:
        """n more bytes written; the listener hears at most four times a second, and at the end."""
        self.done += n
        if self.listener is None:
            return
        now = time.monotonic()
        if now - self._told >= 0.25 or self.done >= self.expected:
            self._told = now
            self.listener(self.done, self.expected)

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()
        with self._lock:
            procs = list(self._procs)
        for proc in procs:
            _kill(proc)

    def check(self) -> None:
        if self._event.is_set():
            raise Cancelled()

    @contextlib.contextmanager
    def track(self, proc: subprocess.Popen):
        with self._lock:
            self._procs.add(proc)
        try:
            if self._event.is_set():  # cancelled while it was starting
                _kill(proc)
            yield proc
        finally:
            with self._lock:
                self._procs.discard(proc)


def _kill_windows_tree(root: int) -> None:
    """root and what it started, from Windows' own list of programs: taskkill /T does the same
    but took 1.5 s here, with 350 programs running, and Cancel promises one."""
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):  # PROCESSENTRY32W
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("pid", wintypes.DWORD),
                    ("heap", ctypes.c_size_t), ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                    ("parent", wintypes.DWORD), ("priority", wintypes.LONG), ("flags", wintypes.DWORD),
                    ("exe", ctypes.c_wchar * 260)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.restype = handle
    k32.Process32FirstW.argtypes = k32.Process32NextW.argtypes = (handle, ctypes.POINTER(Entry))
    k32.OpenProcess.restype = handle
    k32.CloseHandle.argtypes = (handle,)
    k32.TerminateProcess.argtypes = (handle, wintypes.UINT)
    k32.GetProcessTimes.argtypes = (handle, *[ctypes.POINTER(ctypes.c_ulonglong)] * 4)

    def started(pid: int) -> int:
        """When pid began, 0 if that can't be asked."""
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return 0
        times = [ctypes.c_ulonglong() for _ in range(4)]
        ok = k32.GetProcessTimes(h, *[ctypes.byref(t) for t in times])
        k32.CloseHandle(h)
        return times[0].value if ok else 0

    children: dict[int, list[int]] = {}
    snapshot = k32.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    if snapshot and snapshot != handle(-1).value:
        entry = Entry()
        entry.size = ctypes.sizeof(Entry)
        more = k32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            children.setdefault(entry.parent, []).append(entry.pid)
            more = k32.Process32NextW(snapshot, ctypes.byref(entry))
        k32.CloseHandle(snapshot)
    tree = [root]
    for pid in tree:  # grows as it goes: parents first, so none starts another while we work
        born = started(pid)
        # Windows reuses numbers: something older than this "parent" had another one, long gone
        tree += [c for c in children.get(pid, []) if c not in tree and born and started(c) >= born]
    for pid in tree:
        h = k32.OpenProcess(1, False, pid)  # PROCESS_TERMINATE
        if h:
            k32.TerminateProcess(h, 1)
            k32.CloseHandle(h)


def _kill(proc: subprocess.Popen) -> None:
    """The program and everything it started: LibreOffice's soffice.exe runs soffice.bin, and
    killing only the first would leave the second converting (and holding the files)."""
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        _kill_windows_tree(proc.pid)
    if proc.poll() is None:
        proc.kill()


_current: contextvars.ContextVar[Token | None] = contextvars.ContextVar("cancel", default=None)


@contextlib.contextmanager
def using(token: Token):
    """Run the conversions inside with this token: its cancel() reaches every step."""
    reset = _current.set(token)
    try:
        yield token
    finally:
        _current.reset(reset)


def check() -> None:
    """Raise Cancelled if the conversion running on this thread was stopped."""
    token = _current.get()
    if token is not None:
        token.check()


def expect(total: int) -> None:
    """Tell the running conversion's token how many bytes this file will write."""
    token = _current.get()
    if token is not None:
        token.expect(total)


def moved(n: int) -> None:
    """n more bytes written by the running conversion."""
    token = _current.get()
    if token is not None:
        token.moved(n)


@contextlib.contextmanager
def track(proc: subprocess.Popen):
    """Kill proc if the conversion running on this thread is stopped while it runs."""
    token = _current.get()
    if token is None:
        yield proc
    else:
        with token.track(proc):
            yield proc
