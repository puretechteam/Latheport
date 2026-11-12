"""Update now (selfupdate.py and the launcher's --finish-update), against a fake release on a local server: the
release's zip and checksum, the download checked and cancellable, the unpacking whole or not at all, the banner,
and the folder swap by the real launcher (built with cargo; a stand-in Latheport.exe from its examples), with the
old copy put back when the new one doesn't start. Never the real install: every folder is a scratch one."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from latheport import selfupdate, updates

LAUNCHER = Path(__file__).resolve().parent.parent / "packaging" / "launcher"


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


APP_ZIP = _zip({"Latheport/Latheport.exe": b"MZ new app", "Latheport/latheport-cli.exe": b"MZ launcher",
                "Latheport/_internal/data.bin": os.urandom(600_000), "Latheport/version.txt": b"9.0.0"})


class Server:
    """GitHub's latest-release answer and the zip it lists, from 127.0.0.1."""

    def __init__(self, zip_bytes: bytes = APP_ZIP, served: bytes | None = None, digest: str | None = None,
                 url: str | None = None, version: str = "9.0.0"):
        self.zip, self.served, self.version = zip_bytes, zip_bytes if served is None else served, version
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/latest":
                    body = json.dumps(outer.answer()).encode()
                elif self.path.startswith("/dl/"):
                    body = outer.served
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        self.digest = digest if digest is not None else "sha256:" + hashlib.sha256(zip_bytes).hexdigest()
        self.url = url or f"{self.base}/dl/Latheport-{version}-windows.zip"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def answer(self) -> dict:
        v = self.version
        return {"tag_name": f"v{v}", "html_url": f"https://github.com/puretechteam/Latheport/releases/tag/v{v}",
                "assets": [{"name": f"Latheport-{v}-windows.zip", "size": len(self.zip), "digest": self.digest,
                            "browser_download_url": self.url}]}

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server(monkeypatch):
    s = Server()
    monkeypatch.setenv("LATHEPORT_UPDATE_URL", s.base + "/latest")
    yield s
    s.close()


def _app(tmp_path: Path) -> Path:
    app = tmp_path / "Latheport"
    app.mkdir()
    (app / "Latheport.exe").write_bytes(b"MZ old app")
    (app / "latheport-cli.exe").write_bytes(b"MZ launcher")
    return app


# ---- the release, its zip and checksum

def test_the_release_lists_its_zip_and_checksum(server):
    r = updates.release()
    assert r.version == "9.0.0" and r.zip_url.endswith("/dl/Latheport-9.0.0-windows.zip")
    assert r.size == len(APP_ZIP) and r.sha256 == hashlib.sha256(APP_ZIP).hexdigest()
    assert r.page == "https://github.com/puretechteam/Latheport/releases/tag/v9.0.0"


@pytest.mark.parametrize("digest, url", [("", None), ("md5:" + "0" * 32, None), (None, "https://example.com/x.zip"),
                                         (None, "https://github.com/someone/else/releases/download/v9/x.zip")])
def test_a_zip_without_a_checksum_or_from_elsewhere_isnt_offered(monkeypatch, digest, url):
    s = Server(digest=digest, url=url)
    monkeypatch.setenv("LATHEPORT_UPDATE_URL", s.base + "/latest")
    try:
        r = updates.release()
        assert r.version == "9.0.0" and r.zip_url == ""  # the page still shows; Update now doesn't
    finally:
        s.close()


def test_the_real_downloads_are_accepted_from_github_only():
    assert updates._download_ok("https://github.com/puretechteam/Latheport/releases/download/v1.5.0/x.zip", updates.URL)
    assert not updates._download_ok("https://github.com/puretechteam/Latheport.evil/releases/download/x.zip", updates.URL)
    assert not updates._download_ok("http://127.0.0.1:9/x.zip", updates.URL)  # a local server only in the tests


# ---- download and unpack

def test_download_and_unpack_beside_the_app(server, tmp_path):
    app = _app(tmp_path)
    seen = []
    z = selfupdate.download(updates.release(), app, lambda got, total: seen.append((got, total)))
    assert z.read_bytes() == APP_ZIP and seen[-1] == (len(APP_ZIP), len(APP_ZIP)) and len(seen) >= 2
    staged = selfupdate.unpack(z, app, "9.0.0")
    assert staged == tmp_path / "Latheport.update-9.0.0" and selfupdate.ready(app, "9.0.0")
    assert (staged / "version.txt").read_bytes() == b"9.0.0" and (staged / "_internal" / "data.bin").stat().st_size == 600_000
    assert not z.exists() and (app / "Latheport.exe").read_bytes() == b"MZ old app"  # the running copy untouched


def test_a_download_that_doesnt_match_is_refused_and_removed(monkeypatch, tmp_path):
    app = _app(tmp_path)
    for served, words in ((APP_ZIP[:-1] + b"X", "checksum"), (APP_ZIP[:1000], "stopped at")):
        s = Server(served=served)
        monkeypatch.setenv("LATHEPORT_UPDATE_URL", s.base + "/latest")
        try:
            with pytest.raises(selfupdate.UpdateError, match=words):
                selfupdate.download(updates.release(), app)
        finally:
            s.close()
        assert sorted(p.name for p in tmp_path.iterdir()) == ["Latheport"]  # no part file left


def test_cancel_stops_the_download_and_leaves_nothing(server, tmp_path):
    app = _app(tmp_path)
    with pytest.raises(selfupdate.UpdateError, match="Cancelled"):
        selfupdate.download(updates.release(), app, cancelled=lambda: True)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["Latheport"]


@pytest.mark.parametrize("files, words", [
    ({"Latheport/Latheport.exe": b"x", "Latheport/../evil.txt": b"x"}, "isn't safe|outside"),
    ({"Latheport/Latheport.exe": b"x", "Other/readme.txt": b"x"}, "outside"),
    ({"Latheport/readme.txt": b"x"}, "no Latheport.exe")])
def test_a_zip_that_isnt_a_latheport_folder_is_refused_whole(tmp_path, files, words):
    app = _app(tmp_path)
    z = tmp_path / "u.zip"
    z.write_bytes(_zip(files))
    with pytest.raises(selfupdate.UpdateError, match=words):
        selfupdate.unpack(z, app, "9.0.0")
    assert not (tmp_path / "Latheport.update-9.0.0").exists() and not (tmp_path / "evil.txt").exists()
    assert not (tmp_path / "Latheport.update-9.0.0.part").exists()


def test_only_the_installed_app_with_a_folder_it_can_write_updates(tmp_path):
    assert selfupdate.app_dir() is None  # the tests run from source
    assert selfupdate.why_not(None) == "running from source"
    app = _app(tmp_path)
    assert selfupdate.why_not(app) is None
    (app / "latheport-cli.exe").unlink()
    assert "latheport-cli.exe" in selfupdate.why_not(app)


def test_old_updates_are_tidied_but_a_newer_one_is_kept(tmp_path):
    app = _app(tmp_path)
    for name in ("Latheport.update-1.4.0", "Latheport.update-9.0.0", "Latheport.update-1.5.0.part"):
        (tmp_path / name).mkdir()
    (tmp_path / "Latheport.update-1.5.0.zip.part").write_bytes(b"x")
    selfupdate.tidy(app, "1.5.0")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["Latheport", "Latheport.update-9.0.0"]


def test_the_update_flags_arent_files():
    from latheport.instance import update_flags
    assert update_flags(["--updated", "C:/t/started", "a.png"]) == (["a.png"], {"updated": "C:/t/started"})
    assert update_flags(["--update-failed", "9.0.0"]) == ([], {"update-failed": "9.0.0"})


# ---- the banner

@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    from PySide6 import QtCore

    from latheport import gui
    settings = QtCore.QSettings(str(tmp_path / "settings.ini"), QtCore.QSettings.IniFormat)
    monkeypatch.setattr(gui, "QSettings", lambda *a: settings)
    monkeypatch.setenv("LATHEPORT_PRESETS", str(tmp_path / "presets.json"))
    w = gui.Window()
    yield w
    w.close()


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _pump(qapp, until, seconds=20.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()
        if until():
            return True
        time.sleep(0.02)
    return False


def test_from_source_the_banner_shows_the_page_only(window, server):
    window.show_update("9.0.0", "https://github.com/puretechteam/Latheport/releases/tag/v9.0.0", updates.release())
    assert window.update_link.isVisibleTo(window) and not window.update_now.isVisibleTo(window)


def test_update_now_downloads_then_restart_hands_over(window, server, qapp, tmp_path, monkeypatch):
    from latheport import gui
    app = _app(tmp_path)
    monkeypatch.setattr(selfupdate, "app_dir", lambda: app)
    handed, quit_ = [], []
    monkeypatch.setattr(selfupdate, "restart", lambda a, v, pid=None: handed.append((a, v)))
    monkeypatch.setattr(gui.QApplication, "quit", lambda *a: quit_.append(1))
    rel = updates.release()
    window.show_update(rel.version, rel.page, rel)
    mb = round(len(APP_ZIP) / 1_000_000)
    assert window.update_now.isVisibleTo(window) and window.update_now.text() == f"Update now ({max(1, mb)} MB)"
    window.update_now.click()
    assert window.update_cancel.isVisibleTo(window) and "Downloading" in window.update_note.text()
    assert _pump(qapp, lambda: window.update_restart.isVisibleTo(window)), window.update_note.text()
    assert window.update_note.text() == "Latheport 9.0.0 is ready" and selfupdate.ready(app, "9.0.0")
    window.update_restart.click()
    assert handed == [(app, "9.0.0")] and quit_ == [1]


def test_cancel_in_the_banner_goes_back_to_update_now(window, server, qapp, tmp_path, monkeypatch):
    app = _app(tmp_path)
    monkeypatch.setattr(selfupdate, "app_dir", lambda: app)
    real = selfupdate.download
    monkeypatch.setattr(selfupdate, "download", lambda r, a, p=None, c=None: (time.sleep(0.3), real(r, a, p, c))[1])
    rel = updates.release()
    window.show_update(rel.version, rel.page, rel)
    window.update_now.click()
    window.update_cancel.click()
    assert _pump(qapp, lambda: window.update_now.isVisibleTo(window))
    assert not selfupdate.ready(app, "9.0.0") and sorted(p.name for p in tmp_path.iterdir() if p.name != "settings.ini"
                                                         and p.name != "presets.json") == ["Latheport"]


def test_after_skipping_releases_it_goes_straight_to_the_latest(window, qapp, tmp_path, monkeypatch):
    """9.0.0 was turned down with Not now and even downloaded, then 9.1.0 came out: the banner offers 9.1.0, and
    Restart goes to 9.1.0, never the stale 9.0.0 (each zip is the whole app, so any version jumps to the latest)."""
    from latheport import gui
    app = _app(tmp_path)
    monkeypatch.setattr(selfupdate, "app_dir", lambda: app)
    old = tmp_path / "Latheport.update-9.0.0"
    old.mkdir()
    (old / "Latheport.exe").write_bytes(b"MZ 9.0.0")
    (old / selfupdate.READY).write_text("9.0.0", encoding="utf-8")
    window.settings.setValue("update/skip", "9.0.0")
    newest = _zip({"Latheport/Latheport.exe": b"MZ 9.1.0", "Latheport/version.txt": b"9.1.0"})
    s = Server(zip_bytes=newest, version="9.1.0")
    monkeypatch.setenv("LATHEPORT_UPDATE_URL", s.base + "/latest")
    handed = []
    monkeypatch.setattr(selfupdate, "restart", lambda a, v, pid=None: handed.append(v))
    monkeypatch.setattr(gui.QApplication, "quit", lambda *a: None)
    try:
        rel = updates.release()
        assert rel.version == "9.1.0" and updates.newer("9.1.0", "1.4.0")
        window.show_update(rel.version, rel.page, rel)
        assert window.update_bar.isVisibleTo(window) and window.update_note.text() == "Latheport 9.1.0 is out"
        assert window.update_now.isVisibleTo(window) and not window.update_restart.isVisibleTo(window)
        window.update_now.click()
        assert _pump(qapp, lambda: window.update_restart.isVisibleTo(window)), window.update_note.text()
        assert window.update_note.text() == "Latheport 9.1.0 is ready"
        window.update_restart.click()
        assert handed == ["9.1.0"]
        assert (tmp_path / "Latheport.update-9.1.0" / "version.txt").read_bytes() == b"9.1.0"
        selfupdate.tidy(app, "9.1.0")  # once 9.1.0 runs, the 9.0.0 that was never used goes
        assert not old.exists()
    finally:
        s.close()


def test_a_failed_download_says_why_and_offers_the_page(window, monkeypatch, qapp, tmp_path):
    s = Server(served=b"not the zip")
    monkeypatch.setenv("LATHEPORT_UPDATE_URL", s.base + "/latest")
    app = _app(tmp_path)
    monkeypatch.setattr(selfupdate, "app_dir", lambda: app)
    try:
        rel = updates.release()
        window.show_update(rel.version, rel.page, rel)
        window.update_now.click()
        assert _pump(qapp, lambda: "didn't download" in window.update_note.text())
        assert window.update_link.isVisibleTo(window) and not window.update_restart.isVisibleTo(window)
    finally:
        s.close()


def test_after_an_update_the_new_version_says_it_opened(window, tmp_path):
    marker = tmp_path / "started"
    window.after_update({"updated": str(marker)})
    assert marker.read_text(encoding="utf-8") == "ok" and "Updated to Latheport" in window.update_note.text()
    window.after_update({"update-failed": "9.0.0"})
    assert "9.0.0 didn't start" in window.update_note.text()


# ---- the swap, by the real launcher

def _cargo_build() -> Path | None:
    cargo = shutil.which("cargo")
    if sys.platform != "win32" or cargo is None:
        return None
    r = subprocess.run([cargo, "build", "--release", "--examples", "--bins"], cwd=LAUNCHER, capture_output=True,
                       timeout=600, check=False)
    return LAUNCHER / "target" / "release" if r.returncode == 0 else None


@pytest.fixture(scope="module")
def built():
    out = _cargo_build()
    if out is None:
        pytest.skip("needs Rust's cargo on Windows to build the launcher")
    return out


def _installed(tmp_path: Path, built: Path, version: str, crash: bool = False) -> tuple[Path, Path]:
    app, new = tmp_path / "Latheport", tmp_path / "Latheport.update-9.0.0"
    for folder, v in ((app, version), (new, "9.0.0")):
        folder.mkdir()
        shutil.copy2(built / "examples" / "fake_app.exe", folder / "Latheport.exe")
        shutil.copy2(built / "latheport-cli.exe", folder / "latheport-cli.exe")
        (folder / "version.txt").write_text(v, encoding="utf-8")
    (new / selfupdate.READY).write_text("9.0.0", encoding="utf-8")
    if crash:
        (new / "crash").write_text("", encoding="utf-8")
    return app, new


def _old_latheport() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.0)"])


def test_the_launcher_swaps_in_the_new_version_and_keeps_it(built, tmp_path):
    app, new = _installed(tmp_path, built, "1.5.0")
    old = _old_latheport()
    proc = selfupdate.restart(app, "9.0.0", pid=old.pid)
    assert proc.wait(timeout=60) == 0
    assert (app / "version.txt").read_text(encoding="utf-8") == "9.0.0"
    assert not new.exists() and not (tmp_path / "Latheport.old").exists()  # the old copy went once the new one opened
    log = (app / "started.log").read_text(encoding="utf-8")
    assert log.startswith("--updated ") and Path(log.split(" ", 1)[1].strip()).read_text(encoding="utf-8") == "ok"
    assert old.wait(timeout=10) == 0 and old.returncode == 0  # it waited for the old one to close
    time.sleep(1.0)  # the stand-in app closes


def test_the_launcher_puts_the_old_version_back_when_the_new_one_doesnt_start(built, tmp_path):
    app, _ = _installed(tmp_path, built, "1.5.0", crash=True)
    old = _old_latheport()
    proc = selfupdate.restart(app, "9.0.0", pid=old.pid)
    assert proc.wait(timeout=60) == 1
    assert old.wait(timeout=10) == 0
    assert (app / "version.txt").read_text(encoding="utf-8") == "1.5.0"  # the old copy is back
    assert (tmp_path / "Latheport.failed" / "version.txt").read_text(encoding="utf-8") == "9.0.0"
    assert (tmp_path / "Latheport.failed" / selfupdate.ASIDE).is_file()  # marked, so the next start may tidy it
    time.sleep(1.5)  # the old copy, started again, logs how
    assert "--update-failed 9.0.0" in (app / "started.log").read_text(encoding="utf-8")


def _wait_pid(pid: int, timeout: float = 90) -> int | None:
    import psutil
    try:
        return psutil.Process(pid).wait(timeout=timeout)
    except psutil.NoSuchProcess:
        return None


def test_the_swap_works_when_latheport_ran_in_its_own_folder(built, tmp_path):
    """Latheport started from its Start menu shortcut (or a double-click) runs with the app's folder as its working
    folder, and Windows won't rename a folder that is any running program's working folder. Restart used to hand
    that folder on to the update helper, so every rename failed and the update said it had failed."""
    app, new = _installed(tmp_path, built, "1.5.0")
    root = Path(__file__).resolve().parent.parent
    code = ("import sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path; from latheport import selfupdate\n"
            "p = selfupdate.restart(Path(sys.argv[2]), '9.0.0'); print(p.pid, p.args[5])")
    r = subprocess.run([sys.executable, "-c", code, str(root), str(app)], cwd=app, capture_output=True, text=True,
                       timeout=60, check=True)  # this "Latheport" runs in the app's folder, then closes
    pid, marker = r.stdout.split(maxsplit=1)
    _wait_pid(int(pid))
    log = (Path(marker.strip()).parent / "update.log").read_text(encoding="utf-8")
    assert (app / "version.txt").read_text(encoding="utf-8") == "9.0.0", log
    assert not new.exists() and not (tmp_path / "Latheport.old").exists(), log
    assert Path((app / "cwd.log").read_text(encoding="utf-8")) == app  # the new one runs in its own folder
    time.sleep(1.0)


def test_the_update_helper_started_in_the_app_folder_still_swaps(built, tmp_path):
    """Whatever folder the helper is started in, it leaves it first, so the app's folder can be renamed."""
    app, new = _installed(tmp_path, built, "1.5.0")
    work = tmp_path / "work"
    work.mkdir()
    shutil.copy2(app / "latheport-cli.exe", work / "latheport-update.exe")
    old = _old_latheport()
    proc = subprocess.run([str(work / "latheport-update.exe"), "--finish-update", str(old.pid), str(new), str(app),
                           str(work / "started"), "9.0.0"], cwd=app, timeout=90, check=False)
    log = (work / "update.log").read_text(encoding="utf-8")
    assert proc.returncode == 0, log
    assert (app / "version.txt").read_text(encoding="utf-8") == "9.0.0" and not (tmp_path / "Latheport.old").exists()
    assert old.wait(timeout=10) == 0
    time.sleep(1.0)


def test_what_earlier_updates_left_is_tidied(tmp_path):
    """The old copy and one that didn't start (about 230 MB each) and the helpers' folders in %TEMP% go; a helper
    folder that may still be in use (newer than ten minutes) stays. An app folder with [ ] in its name is no glob."""
    side = tmp_path / "apps"
    side.mkdir()
    app = side / "Latheport [portable]"
    app.mkdir()
    for name in ("Latheport [portable].old", "Latheport [portable].failed", "Latheport [portable].update-1.4.0",
                 "Latheport [portable].update-9.0.0", "Latheport.old"):
        (side / name).mkdir()
        if name.endswith((".old", ".failed")):
            (side / name / selfupdate.ASIDE).write_text("", encoding="utf-8")  # moved aside by an update
    temp = tmp_path / "temp"
    stale, fresh, other = temp / "latheport-update-a", temp / "latheport-update-b", temp / "something-else"
    for d in (stale, fresh, other):
        d.mkdir(parents=True)
        (d / "update.log").write_text("x", encoding="utf-8")
    hour_ago = time.time() - 3600
    os.utime(stale, (hour_ago, hour_ago))
    os.utime(other, (hour_ago, hour_ago))
    selfupdate.tidy(app, "1.5.0", temp=temp)
    assert sorted(p.name for p in side.iterdir()) == ["Latheport [portable]", "Latheport [portable].update-9.0.0",
                                                      "Latheport.old"]  # another app's .old isn't ours
    assert sorted(p.name for p in temp.iterdir()) == ["latheport-update-b", "something-else"]


def test_a_folder_of_the_users_own_named_like_an_old_copy_is_never_deleted(tmp_path):
    """Only folders the launcher moved aside (and marked) go: a backup the user named Latheport.old stays."""
    app = tmp_path / "Latheport"
    app.mkdir()
    for name in ("Latheport.old", "Latheport.failed"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "my photos.txt").write_text("keep", encoding="utf-8")
    selfupdate.tidy(app, "1.5.0", temp=tmp_path / "temp")
    assert (tmp_path / "Latheport.old" / "my photos.txt").exists() and (tmp_path / "Latheport.failed").exists()


def test_the_launcher_leaves_a_folder_of_the_users_own_alone(built, tmp_path):
    """A Latheport.old the update didn't make is in the way: nothing is moved or deleted, and Latheport says the
    update didn't go through."""
    app, new = _installed(tmp_path, built, "1.5.0")
    mine = tmp_path / "Latheport.old"
    mine.mkdir()
    (mine / "my photos.txt").write_text("keep", encoding="utf-8")
    old = _old_latheport()
    proc = selfupdate.restart(app, "9.0.0", pid=old.pid)
    assert proc.wait(timeout=60) == 1
    assert old.wait(timeout=10) == 0
    assert (mine / "my photos.txt").read_text(encoding="utf-8") == "keep"
    assert (app / "version.txt").read_text(encoding="utf-8") == "1.5.0" and new.exists()
    time.sleep(1.5)  # the old copy, started again, logs how
    assert "--update-failed 9.0.0" in (app / "started.log").read_text(encoding="utf-8")
