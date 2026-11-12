"""Watch folders, the queue of files waiting their turn, and the once-a-day update check."""

import json
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from PIL import Image

from latheport import updates, watch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def picture(path: Path, size=(8, 8)) -> Path:
    Image.new("RGB", size, (200, 30, 30)).save(path)
    return path


# ---- the watcher

def test_files_already_there_are_left_alone(tmp_path):
    picture(tmp_path / "old.png")
    clock = Clock()
    w = watch.Watcher(watch.Watch(tmp_path, "jpg"), clock)
    clock.t = 10
    assert w.poll() == []


def test_a_new_file_is_ready_once_it_stops_changing(tmp_path):
    clock = Clock()
    w = watch.Watcher(watch.Watch(tmp_path, "jpg"), clock)
    new = picture(tmp_path / "new.png")
    assert w.poll() == []           # just seen
    clock.t = 1.0
    assert w.poll() == []           # not quiet for 2 s yet
    with open(new, "ab") as f:      # still being written
        f.write(b"\0" * 10)
    clock.t = 2.5
    assert w.poll() == []           # it changed: the wait starts again
    clock.t = 4.6
    assert w.poll() == [new]
    clock.t = 10
    assert w.poll() == []           # once


def test_partial_downloads_hidden_and_empty_files_wait(tmp_path):
    clock = Clock()
    w = watch.Watcher(watch.Watch(tmp_path, "jpg"), clock)
    for name in ("a.png.crdownload", "b.part", "~$report.docx", ".hidden.png"):
        (tmp_path / name).write_bytes(b"x")
    (tmp_path / "empty.png").write_bytes(b"")
    (tmp_path / "sub").mkdir()
    w.poll()
    clock.t = 5
    assert w.poll() == []


def test_a_replaced_file_is_converted_again_and_the_converted_folder_never(tmp_path):
    clock = Clock()
    w = watch.Watcher(watch.Watch(tmp_path, "jpg"), clock)
    p = picture(tmp_path / "a.png")
    w.poll()
    clock.t = 3
    assert w.poll() == [p]
    (tmp_path / watch.OUT_DIR).mkdir()
    picture(tmp_path / watch.OUT_DIR / "a.jpg")
    picture(p, (16, 16))
    os.utime(p, ns=(1, 2))
    clock.t = 4
    w.poll()
    clock.t = 7
    assert w.poll() == [p]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' sharing rules")
def test_a_file_another_program_is_still_writing_waits(tmp_path):
    """Windows' own copy sets the full size at the start and the time at the end, so a big file being
    copied looks finished: only the other program's open handle says it isn't."""
    import ctypes
    from ctypes import wintypes
    clock = Clock()
    w = watch.Watcher(watch.Watch(tmp_path, "png"), clock)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                   wintypes.DWORD, wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    path = picture(tmp_path / "copying.png")
    handle = kernel.CreateFileW(str(path), 0x40000000, 1, None, 3, 0, None)  # write access, others may only read
    assert handle not in (None, wintypes.HANDLE(-1).value)
    try:
        for _ in range(4):
            w.poll()
            clock.t += 3
        assert w.poll() == []
    finally:
        kernel.CloseHandle(handle)
    w.poll()
    clock.t += 3
    assert w.poll() == [path]


def test_a_read_only_file_is_not_taken_for_a_busy_one(tmp_path):
    clock = Clock()
    w = watch.Watcher(watch.Watch(tmp_path, "png"), clock)
    path = picture(tmp_path / "locked.png")
    path.chmod(0o444)
    try:
        w.poll()
        clock.t += 3
        assert w.poll() == [path]
    finally:
        path.chmod(0o644)


def test_a_watched_folder_that_goes_away_is_no_error(tmp_path):
    d = tmp_path / "gone"
    d.mkdir()
    w = watch.Watcher(watch.Watch(d, "jpg"), Clock())
    d.rmdir()
    assert w.poll() == []


def test_run_converts_into_the_converted_folder(tmp_path):
    from latheport import pipeline
    made = []
    ticks = iter(range(100))
    clock = Clock()

    def fake_sleep(_):
        clock.t += 1.0
        if next(ticks) == 0:
            picture(tmp_path / "drop.png")

    def convert(path, w):
        made.append(pipeline.convert(path, w.target, w.out))

    watch.run([watch.Watch(tmp_path, "jpg")], convert, lambda: bool(made), sleep=fake_sleep, clock=clock)
    assert made and made[0].parent == tmp_path / "Converted" and made[0].suffix == ".jpg"
    assert (tmp_path / "drop.png").exists()  # the original stays


# ---- update check

def test_versions_compare_as_numbers():
    assert updates.newer("1.10.0", "1.9.3")
    assert updates.newer("2.0", "1.9.9")
    assert not updates.newer("1.4.0", "1.4.0")
    assert not updates.newer("1.3.9", "1.4.0")


def test_once_a_day():
    assert updates.due(0, updates.EVERY_S)
    assert not updates.due(1000, 1000 + 3600)
    assert updates.due(5000, 100)  # a clock set back: ask again rather than never


@pytest.fixture
def release_server(monkeypatch):
    answer = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(answer["data"]).encode()
            self.send_response(answer.get("status", 200))
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            answer["agent"] = self.headers.get("User-Agent")

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("LATHEPORT_UPDATE_URL", f"http://127.0.0.1:{server.server_port}/latest")
    yield answer
    server.shutdown()
    server.server_close()


def test_latest_release_is_read_and_nothing_else_is_sent(release_server):
    release_server["data"] = {"tag_name": "v9.1.0", "html_url": "https://github.com/puretechteam/Latheport/releases/tag/v9.1.0"}
    assert updates.release() == updates.Release("9.1.0", "https://github.com/puretechteam/Latheport/releases/tag/v9.1.0")
    assert release_server["agent"].startswith("Latheport/")


@pytest.mark.parametrize("data", [
    {"tag_name": "v9.1.0", "html_url": "https://evil.example/download"},       # a page that isn't ours
    {"tag_name": "v9.1.0", "html_url": "https://github.com/puretechteam/Latheport", "prerelease": True},
    {"tag_name": "nightly", "html_url": "https://github.com/puretechteam/Latheport"},
    ["not", "a", "release"],
])
def test_odd_answers_are_ignored(release_server, data):
    release_server["data"] = data
    assert updates.release() is None


def test_an_answer_that_is_not_http_is_no_error(monkeypatch):
    """A captive portal or a broken proxy: http.client's own errors aren't OSErrors."""
    listener = socket.create_server(("127.0.0.1", 0))

    def answer():
        conn, _ = listener.accept()
        conn.recv(4096)
        conn.sendall(b"this is not http" + bytes((13, 10, 13, 10)))
        conn.close()

    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    monkeypatch.setenv("LATHEPORT_UPDATE_URL", f"http://127.0.0.1:{listener.getsockname()[1]}/latest")
    try:
        assert updates.release(timeout=5) is None
    finally:
        thread.join(5)
        listener.close()


def test_no_network_is_no_error(monkeypatch):
    monkeypatch.setenv("LATHEPORT_UPDATE_URL", "http://127.0.0.1:9/latest")  # nothing listens there
    assert updates.release(timeout=1) is None
    monkeypatch.setenv("LATHEPORT_UPDATE_URL", "ftp://example.com/latest")
    assert updates.release() is None


# ---- the window

@pytest.fixture
def window(tmp_path, monkeypatch):
    QtCore = pytest.importorskip("PySide6.QtCore")
    from latheport import gui
    from latheport.qtdraw import app as qt_app
    app = qt_app()
    settings = QtCore.QSettings(str(tmp_path / "settings.ini"), QtCore.QSettings.IniFormat)
    monkeypatch.setattr(gui, "QSettings", lambda *a: settings)
    win = gui.Window()
    win.app = app
    yield win
    win.quitting = True
    win.close()


class FakeJob:
    def __init__(self, files, target, *rest):
        self.files, self.target, self.cancelled = files, target, False


def test_files_dropped_while_converting_wait_their_turn(window, tmp_path, monkeypatch):
    from latheport import gui
    a, b, c = (picture(tmp_path / f"{n}.png") for n in "abc")
    started = []
    monkeypatch.setattr(gui.Job, "start", lambda job: started.append(job))  # the job is made, never run
    window.set_files([a])
    window.picker.set_current("webp")
    window.start_convert()
    window.set_files([b])
    window.set_files([c])
    assert [e.state for e in window.queue] == ["converting", "waiting", "waiting"]
    assert [e.files for e in window.queue] == [[a], [b], [c]]
    assert all(e.target == "webp" for e in window.queue)  # what's converting now, since they can become it
    assert window.queue_view.count() == 3 and window.queue_view.isVisibleTo(window)
    window.remove_waiting([2])
    window.remove_waiting([0])  # the one converting can't be taken out
    assert [e.files for e in window.queue] == [[a], [b]]
    window.job_done([tmp_path / "a.webp"], [], [], [])
    assert window.queue[0].state == "done"
    window.next_in_queue()
    assert started[-1].files == [b] and window.files == [b] and window.queue[1].state == "converting"


def test_cancel_stops_the_queue_too(window, tmp_path):
    a, b = picture(tmp_path / "a.png"), picture(tmp_path / "b.png")
    window.set_files([a])
    from latheport import gui
    window.job = gui.Job([a], "webp", [])
    window.entry = gui.QueueEntry([a], "webp", state="converting")
    window.set_files([b])
    window.job.token.cancel()
    window.job_done([], [], [], [])
    assert {e.state for e in window.queue} == {"stopped"}


def test_watched_folder_converts_into_converted(window, tmp_path, monkeypatch):
    folder = tmp_path / "inbox"
    folder.mkdir()
    window.set_watch_list([{"folder": str(folder), "target": "jpg"}])
    assert window.watch_timer.isActive() and len(window.watchers) == 1
    clock = Clock()
    window.watchers[0].clock = clock
    started = []
    monkeypatch.setattr(window, "run_job", lambda job, entry=None: started.append(job))
    p = picture(folder / "scan.png")
    window.poll_watched()
    clock.t = 3
    window.poll_watched()
    assert started and started[0].files == [p] and started[0].out_dir == folder / "Converted"
    assert window.queue_view.isVisibleTo(window)
    window.set_watch_list([])
    assert not window.watch_timer.isActive()


def test_closing_while_watching_goes_to_the_notification_area_only_when_asked(window, tmp_path, monkeypatch):
    from latheport import gui
    folder = tmp_path / "inbox"
    folder.mkdir()
    window.set_watch_list([{"folder": str(folder), "target": "pdf"}])
    monkeypatch.setattr(gui.QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    window.show()
    assert not window._to_tray()  # off by default
    window.settings.setValue("watch/tray", "true")
    assert window._to_tray()
    assert not window.isVisible() and window.tray is not None
    window._quit_from_tray()
    assert window.quitting


def test_update_link_shows_and_the_check_can_be_turned_off(window, monkeypatch):
    from latheport import gui
    asked = []

    class Fake:
        def __init__(self, *a):
            self.found = type("S", (), {"connect": lambda _s, fn: asked.append(fn)})()
            self.finished = type("S", (), {"connect": lambda _s, fn: None})()

        def isRunning(self):
            return False

        def start(self):
            asked[-1]("9.0.0", "https://github.com/puretechteam/Latheport/releases/tag/v9.0.0")
    monkeypatch.setattr(gui, "UpdateJob", Fake)
    window.check_updates(now=10 * updates.EVERY_S)
    assert window.update_bar.isVisibleTo(window) and "9.0.0" in window.update_note.text()
    window.update_job = None
    window.check_updates(now=10 * updates.EVERY_S + 60)  # asked already today
    assert len(asked) == 1
    window.update_action.setChecked(False)
    assert not window.update_bar.isVisibleTo(window)
    window.check_updates(now=20 * updates.EVERY_S)
    assert len(asked) == 1


def test_not_now_hides_that_version_for_good(window):
    window.show_update("9.0.0", "https://github.com/puretechteam/Latheport/releases/tag/v9.0.0")
    assert window.update_bar.isVisibleTo(window)
    window.dismiss_update()
    assert not window.update_bar.isVisibleTo(window)
    window.show_update("9.0.0", "https://github.com/puretechteam/Latheport/releases/tag/v9.0.0")
    assert not window.update_bar.isVisibleTo(window)  # not that one again
    window.show_update("9.1.0", "https://github.com/puretechteam/Latheport/releases/tag/v9.1.0")
    assert window.update_bar.isVisibleTo(window) and "9.1.0" in window.update_note.text()  # a newer one is


def test_opening_the_window_doesnt_load_the_web_code():
    """Only the update check needs it, once a day, and it was 30% of what the window imports."""
    code = "import sys, latheport.gui; print('http.client' in sys.modules, 'email.parser' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.split() == ["False", "False"]
