"""Is a newer Latheport out? One small request to GitHub's releases page, at most once a day, from
the window only (it can be turned off in ☰). Nothing is downloaded or installed by itself: the window
shows the release, and Update now (selfupdate.py) only when it's clicked."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from . import __version__

URL = "https://api.github.com/repos/puretechteam/Latheport/releases/latest"
DOWNLOADS = "https://github.com/puretechteam/Latheport/releases/download/"
ZIP = "Latheport-{version}-windows.zip"
EVERY_S = 24 * 3600


def _parts(version: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", version.split("-")[0].split("+")[0])[:3])


def newer(latest: str, current: str = __version__) -> bool:
    try:
        return _parts(latest) > _parts(current)
    except ValueError:
        return False


def due(last_checked: float, now: float) -> bool:
    return not 0 <= now - last_checked < EVERY_S


@dataclass(frozen=True)
class Release:
    version: str
    page: str
    zip_url: str = ""  # the Windows zip, when the release has one GitHub lists a SHA-256 for
    size: int = 0
    sha256: str = ""


def _download_ok(url: str, api: str) -> bool:
    """The zip comes from this repo's own release downloads (or, in the tests, the local server they set)."""
    if url.startswith(DOWNLOADS):
        return True
    m = re.match(r"(http://(?:127\.0\.0\.1|localhost):\d+)/", api)
    return bool(m) and url.startswith(m.group(1) + "/")


def release(timeout: float = 5.0) -> Release | None:
    """The latest release, with its Windows zip if it has one, or None when it can't be told."""
    # here, not at the top: Python's web and email code is 30% of what opening the window imports
    import http.client
    import urllib.request

    api = url = os.environ.get("LATHEPORT_UPDATE_URL", URL)  # tests point it at a local server
    if not url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
        return None
    req = urllib.request.Request(url, headers={"User-Agent": f"Latheport/{__version__}",
                                               "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read(256 * 1024))
    except (OSError, ValueError, http.client.HTTPException):  # a captive portal or a broken proxy answers oddly
        return None
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        return None
    tag, page = str(data.get("tag_name", "")), str(data.get("html_url", ""))
    if not re.fullmatch(r"v?\d+(\.\d+){1,2}", tag) or not page.startswith("https://github.com/puretechteam/"):
        return None
    version = tag.lstrip("v")
    want = ZIP.format(version=version)
    for asset in data.get("assets") or []:
        if not isinstance(asset, dict) or asset.get("name") != want:
            continue
        url, digest, size = str(asset.get("browser_download_url", "")), str(asset.get("digest") or ""), asset.get("size")
        algo, _, hexdigest = digest.partition(":")
        if (algo == "sha256" and re.fullmatch(r"[0-9a-f]{64}", hexdigest) and isinstance(size, int) and size > 0
                and _download_ok(url, api)):
            return Release(version, page, url, size, hexdigest)
    return Release(version, page)
