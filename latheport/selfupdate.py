"""Update now: the release's Windows zip, downloaded beside the app when it's clicked, checked against the SHA-256 and
size GitHub lists for it, and unpacked into a folder beside the app (`Latheport.update-<version>`). Restart then
hands over to a copy of latheport-cli.exe (`--finish-update`, packaging/launcher/src/update.rs), which waits for
Latheport to close, puts the new folder in the old one's place and starts it. The new version writes a marker once
its window is open; only then is the old folder deleted. If it closes without opening, the old one goes back.

Only the installed app updates itself (a run from source has nothing to swap), and only into a folder it can
write to; otherwise the window shows the release's page as before."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Callable
from pathlib import Path

from .updates import Release

EXE = "Latheport.exe"
LAUNCHER = "latheport-cli.exe"
TOP = "Latheport/"  # the zip holds one folder
CHUNK = 256 * 1024
MOST_UNPACKED = 2 * 1024 ** 3  # what a release zip may unpack to (today about 230 MB)
READY = ".ready"  # in the unpacked folder once it's whole and checked
ASIDE = ".latheport-moved-aside"  # the launcher writes it into <app>.old / <app>.failed: only those are deleted


class UpdateError(Exception):
    """The update couldn't be downloaded or unpacked: said in words, and the old copy is untouched."""


def app_dir() -> Path | None:
    """The installed app's folder (the one Latheport.exe runs from), or None when running from source."""
    if not getattr(sys, "frozen", False):
        return None
    here = Path(sys.executable).resolve().parent
    return here if (here / EXE).is_file() else None


def why_not(app: Path | None) -> str | None:
    """Why this copy can't update itself (None: it can)."""
    if app is None:
        return "running from source"
    if not (app / LAUNCHER).is_file():
        return f"{LAUNCHER} isn't next to {EXE}"
    for folder in (app.parent, app):
        try:
            probe = tempfile.NamedTemporaryFile(dir=folder, prefix=".latheport-write-", delete=True)  # noqa: SIM115 - a probe, closed on the next line
            probe.close()
        except OSError:
            return f"{folder} can't be written to"
    return None


def staged(app: Path, version: str) -> Path:
    return app.with_name(f"{app.name}.update-{version}")


def ready(app: Path, version: str) -> bool:
    """A downloaded, checked and unpacked update waiting for a restart."""
    s = staged(app, version)
    return (s / READY).is_file() and (s / EXE).is_file()


def download(rel: Release, app: Path, progress: Callable[[int, int], None] | None = None,
             cancelled: Callable[[], bool] | None = None, timeout: float = 30.0) -> Path:
    """The zip, beside the app, checked; a part file while it comes, removed if anything goes wrong."""
    import urllib.error
    import urllib.request

    from . import __version__
    if not rel.zip_url or not rel.sha256 or rel.size <= 0:
        raise UpdateError("This release has no checked Windows download")
    part = app.with_name(f"{app.name}.update-{rel.version}.zip.part")
    done_path = part.with_suffix("")
    h = hashlib.sha256()
    got = 0
    try:
        req = urllib.request.Request(rel.zip_url, headers={"User-Agent": f"Latheport/{__version__}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(part, "wb") as out:
            while chunk := resp.read(CHUNK):
                if cancelled and cancelled():
                    raise UpdateError("Cancelled")
                got += len(chunk)
                if got > rel.size:
                    raise UpdateError("The download is bigger than the release says")
                h.update(chunk)
                out.write(chunk)
                if progress:
                    progress(got, rel.size)
        if got != rel.size:
            raise UpdateError(f"The download stopped at {got // 1_000_000} of {rel.size // 1_000_000} MB")
        if h.hexdigest() != rel.sha256:
            raise UpdateError("The download didn't match the checksum GitHub lists for it")
        os.replace(part, done_path)
        return done_path
    except (OSError, urllib.error.URLError) as e:
        part.unlink(missing_ok=True)
        raise UpdateError(f"The download failed: {getattr(e, 'reason', e)}") from None
    except BaseException:
        part.unlink(missing_ok=True)
        raise


def unpack(zip_path: Path, app: Path, version: str) -> Path:
    """The zip's Latheport folder into `<app>.update-<version>`, whole or not at all."""
    from .safety import safe_name
    dest = staged(app, version)
    work = dest.with_name(dest.name + ".part")
    shutil.rmtree(work, ignore_errors=True)
    try:
        with zipfile.ZipFile(zip_path) as z:
            members = z.infolist()
            total = sum(m.file_size for m in members)
            if total > MOST_UNPACKED:
                raise UpdateError("The download unpacks to more than a release can be")
            for m in members:
                name = m.filename.rstrip("/")
                if not name.startswith(TOP) and name + "/" != TOP:
                    raise UpdateError(f"The download has a file outside its Latheport folder: {m.filename}")
                if safe_name(name) != name:
                    raise UpdateError(f"The download has a file name that isn't safe: {m.filename}")
            for m in members:
                rel = m.filename[len(TOP):]
                if not rel:
                    continue
                target = work / rel
                if m.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(m) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out, CHUNK)
        if not (work / EXE).is_file():
            raise UpdateError(f"The download has no {EXE}")
        (work / READY).write_text(version, encoding="utf-8")
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(work, dest)
    except (OSError, zipfile.BadZipFile) as e:
        shutil.rmtree(work, ignore_errors=True)
        raise UpdateError(f"The download couldn't be unpacked: {e}") from None
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    zip_path.unlink(missing_ok=True)
    return dest


NO_WINDOW, NEW_GROUP, BREAKAWAY = 0x08000000, 0x00000200, 0x01000000


def restart(app: Path, version: str, pid: int | None = None) -> subprocess.Popen:
    """Hand over to a copy of the launcher (outside the app's folder, so the folder can be renamed); the caller
    then closes Latheport. Returns the launcher's process."""
    if not ready(app, version):
        raise UpdateError("The update isn't downloaded yet")
    work = Path(tempfile.mkdtemp(prefix="latheport-update-"))
    exe = work / "latheport-update.exe"
    shutil.copy2(app / LAUNCHER, exe)
    args = [str(exe), "--finish-update", str(pid or os.getpid()), str(staged(app, version)), str(app),
            str(work / "started"), version]
    # in its own folder: Latheport started from its shortcut runs in the app's folder, and Windows won't rename a
    # folder that is a running program's working folder
    try:  # out of any job Latheport runs in, so closing it doesn't end the update too
        return subprocess.Popen(args, creationflags=NO_WINDOW | NEW_GROUP | BREAKAWAY, close_fds=True, cwd=work)
    except OSError:
        return subprocess.Popen(args, creationflags=NO_WINDOW | NEW_GROUP, close_fds=True, cwd=work)


def started(marker: str) -> None:
    """The new version's window is open: the launcher may delete the old folder."""
    try:
        Path(marker).write_text("ok", encoding="utf-8")
    except OSError:
        pass


LEFTOVER_AGE = 600  # seconds: an update's helper folder in %TEMP% older than this is finished with


def tidy(app: Path, current: str, temp: Path | None = None) -> None:
    """What earlier updates left goes: downloads and unpacked updates for this version or older, the old copy
    (`<app>.old`) or the one that didn't start (`<app>.failed`), and the update helpers' folders in %TEMP%."""
    from .updates import newer
    prefix = f"{app.name}.update-"
    try:
        beside = list(app.parent.iterdir())
    except OSError:
        beside = []
    for p in beside:  # by name, not glob: an app folder named "Latheport [portable]" is no pattern
        if p.name in (f"{app.name}.old", f"{app.name}.failed") and (p / ASIDE).is_file():  # the update made it
            shutil.rmtree(p, ignore_errors=True)
        elif p.name.startswith(prefix):
            version = p.name[len(prefix):].split(".zip")[0].removesuffix(".part")
            if not newer(version, current):
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink(missing_ok=True)
    temp = Path(tempfile.gettempdir()) if temp is None else temp
    now = time.time()
    try:
        helpers = [p for p in temp.iterdir() if p.name.startswith("latheport-update-") and p.is_dir()]
    except OSError:
        helpers = []
    for p in helpers:
        try:
            if now - p.stat().st_mtime > LEFTOVER_AGE:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass
