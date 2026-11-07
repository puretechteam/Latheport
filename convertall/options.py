"""Settings a conversion can take (quality, size, dpi...), and presets: named sets of them.

Each converter says which settings it takes (registry.converter(options=...)); a route shows
only those. A setting left unset keeps what ConvertAll does by default. Presets are built in or
saved by the user (to presets.json in ConvertAll's settings folder, shared by the window and
the command line).
"""

from __future__ import annotations

import copy
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    help: str
    low: int = 0              # a number from low to high...
    high: int = 0
    unit: str = ""
    choices: tuple[str, ...] = ()   # ...or one of these
    names: tuple[str, ...] = ()     # how the window shows each choice (default: capitalized)
    auto: str = "Auto"        # what "not set" does, shown in its place
    common: tuple[int, ...] = ()    # numbers most people pick, offered in the window's list
    slider: bool = False      # shown as a slider in the window (a number with no usual values)
    time: bool = False        # a point in a recording: seconds, m:ss or h:mm:ss (kept as text, "1:30")
    remember: bool = True     # kept for the next conversion and in presets (a trim is for this file only)

    def shown(self, choice: str) -> str:
        """A choice as the window shows it."""
        if self.names and choice in self.choices:
            return self.names[self.choices.index(choice)]
        return choice if choice[:1].isdigit() else choice.capitalize()

    def check(self, value):
        """value as this setting holds it (an int, one of the choices, or for a time "m:ss"); ValueError
        if it can't be."""
        if self.time:
            return show_time(parse_time(value, self.label))
        if self.choices:
            v = str(value).strip().lower()
            if v not in self.choices:
                raise ValueError(f"{self.label} is one of: {', '.join(self.choices)}")
            return v
        try:
            n = int(str(value).strip().lower().removesuffix(self.unit.strip()).strip())  # "1920px" too
        except ValueError:
            raise ValueError(f"{self.label} is a whole number") from None
        if not self.low <= n <= self.high:
            raise ValueError(f"{self.label} is from {self.low} to {self.high}")
        return n


def parse_time(value, label: str = "Time") -> float:
    """Seconds from "90", "90s", "1:30", "1:02:03.5" (or a number); ValueError for anything else."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
    else:
        text = str(value).strip().lower().removesuffix("s").strip()
        m = re.fullmatch(r"(?:(\d{1,3}):)?(?:(\d{1,2}):)?(\d{1,6}(?:[.,]\d{1,3})?)", text)
        if not m:
            raise ValueError(f"{label} is a time like 90, 1:30 or 1:02:03")
        parts = [p for p in (m[1], m[2]) if p is not None]
        seconds = float(m[3].replace(",", "."))
        if parts and seconds >= 60:
            raise ValueError(f"{label} is a time like 90, 1:30 or 1:02:03")
        for i, p in enumerate(reversed(parts)):
            seconds += int(p) * 60 ** (i + 1)
    if not 0 <= seconds <= 100 * 3600:
        raise ValueError(f"{label} is a time from 0 to 100 hours")
    return round(seconds, 3)


def show_time(seconds: float) -> str:
    """1:30, 1:02:03, 0:05.5: how a time is shown and kept."""
    whole = int(seconds)
    frac = round(seconds - whole, 3)
    tail = f"{frac:.3f}".rstrip("0").rstrip(".")[1:] if frac else ""
    h, rest = divmod(whole, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}{tail}" if h else f"{m}:{s:02d}{tail}"


SETTINGS: dict[str, Setting] = {s.key: s for s in (
    Setting("quality", "Quality", "Picture quality for JPG, WebP, AVIF and HEIC: higher looks better, lower is smaller",
            1, 100, auto="Auto (92 for JPG)", slider=True),
    Setting("max_size", "Max size", "Scale pictures down so their longest side is at most this many pixels",
            16, 20000, unit=" px", auto="Original", common=(3840, 2560, 1920, 1280, 1024, 800, 640, 512, 256)),
    Setting("bit_depth", "Colour depth", "Bits a channel in a PNG or TIFF of a RAW, EXR, HDR or XCF picture: 8 is about a third of the size, 16 keeps the finest steps of colour",
            choices=("8", "16"), names=("8 bits (smaller)", "16 bits"), auto="As much as the picture has"),
    Setting("dpi", "Resolution", "How sharp PDF pages are drawn as pictures", 36, 1200, unit=" dpi", auto="150 dpi",
            common=(72, 96, 150, 200, 300, 600)),
    Setting("video_quality", "Video quality", "Smaller files, or better pictures", choices=("smaller", "balanced", "best"),
            auto="Balanced"),
    Setting("video_size", "Video size", "Shrink video (and GIFs) so the shorter side is at most this",
            choices=("360p", "480p", "720p", "1080p", "1440p", "2160p"), auto="Original"),
    Setting("fps", "Frame rate", "Frames a second for video and animations", 1, 120, unit=" fps", auto="Original (12 for GIF)",
            common=(60, 30, 25, 24, 15, 12, 10)),
    Setting("bitrate", "Sound bitrate", "Kilobits a second for MP3, AAC, Ogg, Opus and other lossy sound",
            32, 512, unit=" kbps", auto="Format's own", common=(64, 96, 128, 160, 192, 256, 320)),
    Setting("start", "Start at", "Cut the sound or video so it starts here (seconds, or m:ss)", auto="The beginning",
            time=True, remember=False),
    Setting("end", "End at", "Cut the sound or video so it ends here (seconds, or m:ss)", auto="The end",
            time=True, remember=False),
    Setting("target_size", "Fit in", "Make the video or sound file at most this big: 10 MB for Discord, 25 MB for email",
            1, 4000, unit=" MB", auto="Any size", common=(8, 10, 25, 50, 100, 500)),
    Setting("loudness", "Loudness", "Even out how loud it is (EBU R128), the way streaming services and TV do",
            choices=("streaming", "broadcast"), names=("Even, for streaming (-14 LUFS)", "Even, for TV (-23 LUFS)"),
            auto="As it is"),
    Setting("detail", "Mesh detail", "How finely CAD parts are cut into triangles: finer is smoother and bigger",
            choices=("coarse", "normal", "fine"), auto="Normal"),
    Setting("page_size", "Page size", "The page an ebook is laid out on when it becomes a PDF",
            choices=("phone", "ereader", "a5", "a4", "letter"),
            names=("Phone", "E-reader (6 inch)", "A5", "A4", "Letter"), auto="A5"),
    Setting("text_size", "Text size", "How big an ebook's text is when it becomes a PDF",
            choices=("small", "normal", "large"), auto="Normal"),
)}

DEFAULT = "Default"  # ConvertAll's own settings: the preset that sets nothing

PRESETS: dict[str, dict] = {
    DEFAULT: {},
    "Smaller for email and web": {"quality": 75, "max_size": 1920, "dpi": 100, "video_quality": "smaller",
                                  "video_size": "720p", "bitrate": 128, "detail": "coarse"},
    "Best quality": {"quality": 95, "dpi": 300, "video_quality": "best", "bitrate": 320, "detail": "fine"},
    "For phones and social": {"quality": 85, "max_size": 2048, "video_size": "1080p", "fps": 30, "bitrate": 192},
    "Ready for 3D printing": {"detail": "fine"},
}


def clean(values: dict) -> dict:
    """Only known settings, each checked; unset ones (None, "") left out. ValueError on a bad one."""
    out = {}
    for key, value in values.items():
        if value in (None, ""):
            continue
        if key not in SETTINGS:
            raise ValueError(f"Unknown setting '{key}'")
        out[key] = SETTINGS[key].check(value)
    if "start" in out and "end" in out and parse_time(out["end"]) <= parse_time(out["start"]):
        raise ValueError("End at comes after Start at")
    return out


def lasting(values: dict) -> dict:
    """The settings that are kept for next time and in presets (not a trim, which is for one file)."""
    return {k: v for k, v in values.items() if k not in SETTINGS or SETTINGS[k].remember}


def applying(values: dict, keys) -> dict:
    """The part of values a route takes (its settings keys)."""
    return {k: v for k, v in values.items() if k in set(keys)}


# ---- presets saved by the user

def presets_file() -> Path:
    if os.environ.get("CONVERTALL_PRESETS"):  # tests, or a portable setup
        return Path(os.environ["CONVERTALL_PRESETS"])
    base = os.environ.get("APPDATA") if sys.platform == "win32" else os.environ.get("XDG_CONFIG_HOME")
    return Path(base or Path.home() / ".config") / "ConvertAll" / "presets.json"


_read_cache: list = [None, {}]  # (path, modified, size) and what it held: read again only when it changes


def _stored() -> dict:
    """presets.json as it is, entries we can't use included."""
    path = presets_file()
    try:
        st = path.stat()
        stamp = (str(path), st.st_mtime_ns, st.st_size)
        if _read_cache[0] == stamp:
            return copy.deepcopy(_read_cache[1])
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    data = data if isinstance(data, dict) else {}
    _read_cache[:] = [stamp, copy.deepcopy(data)]
    return data


def user_presets() -> dict[str, dict]:
    out = {}
    for name, values in _stored().items():
        try:
            out[str(name)] = clean(values) if isinstance(values, dict) else {}
        except ValueError:
            continue  # a hand-edited file with a bad value: skip that preset, keep the rest
    return out


def all_presets() -> dict[str, dict]:
    return {**PRESETS, **user_presets()}


def save_preset(name: str, values: dict) -> None:
    name = name.strip()
    if not name:
        raise ValueError("A preset needs a name")
    if any(_same(name, b) for b in PRESETS):
        raise ValueError(f"“{name}” is a built-in preset; pick another name")
    values = lasting(clean(values))
    # everything else stays as it was, entries this version can't read included; "web" replaces "Web"
    presets = {k: v for k, v in _stored().items() if not _same(k, name)}
    presets[name] = values
    _write(presets)


def delete_preset(name: str) -> None:
    stored = _stored()
    presets = {k: v for k, v in stored.items() if not _same(k, name)}
    if len(presets) != len(stored):
        _write(presets)


def _same(a: str, b: str) -> bool:
    return a.strip().casefold() == b.strip().casefold()


def _write(presets: dict) -> None:
    path = presets_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not _stored():
        try:  # a file we can't read at all is kept aside, not written over
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            path.replace(path.with_suffix(".unreadable.json"))
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(presets, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)  # never a half-written file
    # what we just wrote is what's there, even when two saves share a timestamp and a size
    st = path.stat()
    _read_cache[:] = [(str(path), st.st_mtime_ns, st.st_size), copy.deepcopy(presets)]


def preset(name: str) -> dict:
    """A preset's settings by name (any case); KeyError if there's none."""
    presets = all_presets()
    for key, values in presets.items():
        if _same(key, name):
            return dict(values)
    raise KeyError(name)
