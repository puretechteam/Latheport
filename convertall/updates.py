"""Is a newer ConvertAll out? One small request to GitHub's releases page, at most once a day, from
the window only (it can be turned off in ☰). Nothing is downloaded or installed: the window
shows a link to the release."""

from __future__ import annotations

import json
import os
import re

from . import __version__

URL = "https://api.github.com/repos/puretechteam/ConvertAll/releases/latest"
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


def latest(timeout: float = 5.0) -> tuple[str, str] | None:
    """(version, page) of the latest release, or None when it can't be told (offline, rate limited)."""
    # here, not at the top: Python's web and email code is 30% of what opening the window imports
    import http.client
    import urllib.request

    url = os.environ.get("CONVERTALL_UPDATE_URL", URL)  # tests point it at a local server
    if not url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
        return None
    req = urllib.request.Request(url, headers={"User-Agent": f"ConvertAll/{__version__}",
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
    return tag.lstrip("v"), page
