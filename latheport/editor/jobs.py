"""The threads the editor and the Pages window start. Each is kept here until it ends, also after its window
closes: Python letting go of a running QThread ends the program, and so does a thread still running at exit
(the window's way out counts these too)."""

from __future__ import annotations

import time
import warnings

from PySide6.QtCore import QThread

RUNNING: set[QThread] = set()  # every one still going, its window open or not
CLOSE_WAIT_MS = 3000  # how long a closing window waits for all of its work together


def keep(jobs: list, job: QThread) -> None:
    """Hold job (in the window's own list too) until it finishes."""
    jobs.append(job)
    RUNNING.add(job)
    job.finished.connect(lambda j=job: (j in jobs and jobs.remove(j), RUNNING.discard(j)))


def let_go(jobs: list, wait_ms: int = CLOSE_WAIT_MS) -> None:
    """A window closing: ask each job to stop, wait for all of them at once (never wait_ms each), and let what's
    still going (a smart tool downloading) finish on its own, with what it says going nowhere."""
    for job in list(jobs):  # a copy: a job ending takes itself off the list from its own thread
        job.requestInterruption()
    until = time.monotonic() + wait_ms / 1000
    for job in list(jobs):
        if job.wait(max(0, round((until - time.monotonic()) * 1000))):
            continue
        for name in ("done", "got"):
            signal = getattr(job, name, None)
            if signal is not None:
                with warnings.catch_warnings():  # PySide warns when nothing was connected
                    warnings.simplefilter("ignore", RuntimeWarning)
                    try:
                        signal.disconnect()
                    except (RuntimeError, TypeError):
                        pass
