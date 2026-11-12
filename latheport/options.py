"""Settings a conversion can take (quality, size, dpi...), and presets: named sets of them.

Each converter says which settings it takes (registry.converter(options=...)); a route shows
only those. A setting left unset keeps what Latheport does by default. Presets are built in or
saved by the user (to presets.json in Latheport's settings folder, shared by the window and
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

from .smart.languages import BY_NAME


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
    text: bool = False        # any text the user types (a watermark), up to TEXT_MAX characters
    area: bool = False        # a part of the picture: "left,top,width,height" in percent ("10,5,80,60")
    boxes: bool = False       # text on the picture: a list of text boxes (see check_box)
    form: str = ""            # one of FORMS: adjustments, an angle, hidden parts, drawings, a picture file
    row: bool = True          # a row in the window's Options panel (not: set in the editor)

    @property
    def metavar(self) -> str:
        """What the command line takes, for --help."""
        if self.form:
            return FORMS[self.form][1]
        return ("|".join(self.choices) or ("TIME" if self.time else "TEXT" if self.text else
                "LEFT,TOP,WIDTH,HEIGHT" if self.area else "JSON" if self.boxes else "N"))

    @property
    def kind(self) -> str:
        """What the setting takes, in words (latheport presets)."""
        if self.choices:
            return "|".join(self.choices)
        if self.form:
            return FORMS[self.form][2]
        return ("seconds or m:ss" if self.time else "any text" if self.text else "percent of the picture: L,T,W,H"
                if self.area else "text boxes (JSON)" if self.boxes else f"{self.low}-{self.high}")

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
        if self.text:
            return check_text(value, self.label)
        if self.area:
            return check_area(value, self.label)
        if self.boxes:
            return check_boxes(value)
        if self.form:
            return FORMS[self.form][0](value)
        if self.choices:
            v = str(value).strip().lower()
            if v not in self.choices and self.unit and v.removesuffix(self.unit.strip()).strip() in self.choices:
                v = v.removesuffix(self.unit.strip()).strip()  # "2x"
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


# Turn: every way a picture can lie (mirrored or not, then turned clockwise a quarter at a time)
TURNS = {"right": "A quarter right", "left": "A quarter left", "180": "Upside down", "mirror": "Mirrored",
         "flip": "Flipped upside down", "mirror-right": "Mirrored, a quarter right",
         "mirror-left": "Mirrored, a quarter left"}
_ORIENT = {None: (False, 0), "right": (False, 1), "180": (False, 2), "left": (False, 3), "mirror": (True, 0),
           "mirror-right": (True, 1), "flip": (True, 2), "mirror-left": (True, 3)}


def orientation(turn: str | None) -> tuple[bool, int]:
    """(mirrored first, quarter turns clockwise after) for a Turn setting."""
    return _ORIENT[turn or None]


def turn_of(mirrored: bool, quarters: int) -> str | None:
    """The Turn setting for an orientation (None: as it is)."""
    return next(k for k, v in _ORIENT.items() if v == (bool(mirrored), quarters % 4))


def sideways(turn: str | None) -> bool:
    """Whether the turn swaps width and height."""
    return orientation(turn)[1] % 2 == 1


def turn_words(turn: str) -> str:
    """The Turn as a note says it ("Turned a quarter right", "Mirrored, left to right")."""
    return {"right": "Turned a quarter right", "left": "Turned a quarter left", "180": "Turned upside down",
            "mirror": "Mirrored, left to right", "flip": "Flipped upside down",
            "mirror-right": "Mirrored and turned a quarter right",
            "mirror-left": "Mirrored and turned a quarter left"}[turn]


TEXT_MAX = 200
FONTS = ("sans", "serif", "bold", "hand")
EDGES = ("none", "outline", "shadow")
BOXES_MAX = 20


def check_text(value, label: str = "Text") -> str:
    """value as text to draw: one line, at most TEXT_MAX characters, no control characters."""
    text = " ".join(str(value).split())
    if len(text) > TEXT_MAX:
        raise ValueError(f"{label} is at most {TEXT_MAX} characters")
    if any(ord(c) < 32 or 0x7F <= ord(c) < 0xA0 for c in text):
        raise ValueError(f"{label} can't hold control characters")
    return text


def check_area(value, label: str = "Crop by hand") -> str:
    """"left,top,width,height" in percent of the picture, inside it: "10,5,80,60"."""
    try:
        nums = [float(p.strip().removesuffix("%")) for p in str(value).split(",")]
    except ValueError:
        nums = []
    if len(nums) != 4:
        raise ValueError(f"{label} is left,top,width,height in percent, like 10,5,80,60")
    left, top, w, h = nums
    if min(left, top) < 0 or min(w, h) < 0.1 or left + w > 100.001 or top + h > 100.001:
        raise ValueError(f"{label} has to be inside the picture: left + width and top + height at most 100")
    return ",".join(f"{round(n, 2):g}" for n in nums)


def area_box(area: str, width: int, height: int) -> tuple[int, int, int, int]:
    """An area ("10,5,80,60") as a pixel box (left, top, right, bottom) of a width x height picture,
    at least a pixel each way."""
    left, top, w, h = (float(n) / 100 for n in area.split(","))
    x0, y0 = min(round(left * width), width - 1), min(round(top * height), height - 1)
    return (x0, y0, max(x0 + 1, min(width, round((left + w) * width))),
            max(y0 + 1, min(height, round((top + h) * height))))


def check_box(box: dict) -> dict:
    """One text box: what it says, where its middle is (x, y: 0 to 1 across and down), how tall its
    letters are (size: a share of the picture's height), its font, colour and edge, and whether it's
    in the meme style (upper case)."""
    if not isinstance(box, dict):
        raise ValueError("A text box is a set of its text, x, y, size, font, colour and edge")  # noqa: TRY004 - callers turn ValueError into the message
    text = check_text(box.get("text", ""))
    if not text:
        raise ValueError("A text box needs some text")
    out = {"text": text}
    for key, low, high, default in (("x", 0, 1, 0.5), ("y", 0, 1, 0.5), ("size", 0.01, 0.5, 0.08)):
        try:
            v = float(box.get(key, default))
        except (TypeError, ValueError):
            raise ValueError(f"A text box's {key} is a number") from None
        if not low <= v <= high:
            raise ValueError(f"A text box's {key} is from {low:g} to {high:g}")
        out[key] = round(v, 4)
    font, edge = box.get("font", "sans"), box.get("edge", "shadow")
    if font not in FONTS:
        raise ValueError(f"A text box's font is one of: {', '.join(FONTS)}")
    if edge not in EDGES:
        raise ValueError(f"A text box's edge is one of: {', '.join(EDGES)}")
    colour = str(box.get("colour", "#ffffff")).lower()
    if not re.fullmatch(r"#[0-9a-f]{6}", colour):
        raise ValueError("A text box's colour is like #ffffff")
    out.update(font=font, colour=colour, edge=edge, meme=bool(box.get("meme", False)))
    return out


def check_boxes(value) -> str:
    """Text boxes from a list or its JSON, as JSON in one form (settings are kept as text, numbers
    and names: the routes are cached by them). boxes() reads it back."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise ValueError("Text is a list of text boxes, as JSON") from None
    if not isinstance(value, list):
        raise ValueError("Text is a list of text boxes")  # noqa: TRY004 - callers turn ValueError into the message
    if len(value) > BOXES_MAX:
        raise ValueError(f"Text is at most {BOXES_MAX} boxes")
    return json.dumps([check_box(b) for b in value], ensure_ascii=False, separators=(",", ":"))


def boxes(value) -> list[dict]:
    """The text boxes a "texts" setting holds (none when it's unset)."""
    return json.loads(value) if value else []


def text_box(text: str, at: str = "bottom", meme: bool = False) -> dict:
    """A text box at the top, middle or bottom: plain (white, shadowed) or meme (big, upper case,
    white with a black outline)."""
    y = {"top": 0.09, "middle": 0.5, "bottom": 0.91}[at]
    if meme:
        return check_box({"text": text, "x": 0.5, "y": y, "size": 0.11, "font": "bold", "colour": "#ffffff",
                          "edge": "outline", "meme": True})
    return check_box({"text": text, "x": 0.5, "y": y, "size": 0.07, "font": "sans", "colour": "#ffffff",
                      "edge": "shadow"})


# ---- Adjust: brightness, contrast, saturation, warmth and sharpness, each -100 to 100

ADJUSTMENTS = ("brightness", "contrast", "saturation", "warmth", "sharpness")


def check_adjust(value) -> str:
    """"brightness=10,contrast=-5" (or a dict of them) in one form: the known names in a fixed order,
    whole numbers from -100 to 100, the ones at 0 left out. "" when nothing's changed."""
    if isinstance(value, dict):
        pairs = list(value.items())
    else:
        pairs = []
        for part in filter(None, (q.strip() for q in str(value).split(","))):
            name, eq, num = part.partition("=")
            if not eq:
                raise ValueError("Adjust is like brightness=10,contrast=-5")
            pairs.append((name.strip().lower(), num.strip()))
    got = {}
    for name, num in pairs:
        if name not in ADJUSTMENTS:
            raise ValueError(f"Adjust takes {', '.join(ADJUSTMENTS)}")
        try:
            n = int(float(str(num).strip()))
        except ValueError:
            raise ValueError(f"Adjust's {name} is a whole number from -100 to 100") from None
        if not -100 <= n <= 100:
            raise ValueError(f"Adjust's {name} is from -100 to 100")
        got[name] = n
    return ",".join(f"{k}={got[k]}" for k in ADJUSTMENTS if got.get(k))


def adjustments(value) -> dict[str, int]:
    """The adjustments an "adjust" setting holds, every name there (0 for the ones not changed)."""
    got = dict.fromkeys(ADJUSTMENTS, 0)
    for part in filter(None, (value or "").split(",")):
        name, _, num = part.partition("=")
        got[name] = int(num)
    return got


STRAIGHTEN_MOST = 15.0  # degrees either way


def check_angle(value) -> str:
    """Straighten: degrees, -15 to 15, to a tenth ("2.5"); "" for none. Clockwise is more than 0."""
    try:
        a = round(float(str(value).strip().removesuffix("°").strip()), 1)
    except ValueError:
        raise ValueError("Straighten is an angle in degrees, like 2.5 or -1") from None
    if not -STRAIGHTEN_MOST <= a <= STRAIGHTEN_MOST:
        raise ValueError(f"Straighten is from -{STRAIGHTEN_MOST:g} to {STRAIGHTEN_MOST:g} degrees")
    return "" if a == 0 else f"{a:g}"


# ---- Hide: parts blurred, pixelated or covered; Draw: arrows, shapes, pen, highlighter and stickers.
# Both are placed on the picture as it lies once turned and straightened (before any crop), as shares
# of its width and height, so they stay on what they hide or point at when the crop changes.

HIDE_KINDS = ("blur", "pixelate", "cover")
MARK_KINDS = ("arrow", "line", "box", "ellipse", "pen", "highlight", "sticker")
STICKERS = ("check", "cross", "star", "heart", "number")
HIDES_MAX, MARKS_MAX, POINTS_MAX = 50, 200, 2000


def _share(v, what: str) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{what} is a number from 0 to 1") from None
    if not 0 <= f <= 1:
        raise ValueError(f"{what} is from 0 to 1")
    return round(f, 5)


def _colour(v, what: str) -> str:
    colour = str(v).lower()
    if not re.fullmatch(r"#[0-9a-f]{6}", colour):
        raise ValueError(f"{what} is like #ff0000")
    return colour


def check_hide(hide: dict) -> dict:
    """One part to hide: its box (x, y, w, h: shares of the picture), how (blur, pixelate or cover)
    and how strongly (1 to 100)."""
    if not isinstance(hide, dict):
        raise ValueError("A part to hide is a set of its x, y, w, h, how and strength")  # noqa: TRY004 - callers turn ValueError into the message
    x, y = _share(hide.get("x", 0), "A hidden part's x"), _share(hide.get("y", 0), "A hidden part's y")
    w, h = _share(hide.get("w", 0), "A hidden part's w"), _share(hide.get("h", 0), "A hidden part's h")
    if w < 0.002 or h < 0.002 or x + w > 1.00001 or y + h > 1.00001:
        raise ValueError("A hidden part has to be inside the picture, and not empty")
    how = hide.get("how", "pixelate")
    if how not in HIDE_KINDS:
        raise ValueError(f"A hidden part is one of: {', '.join(HIDE_KINDS)}")
    try:
        strength = int(hide.get("strength", 70))
    except (TypeError, ValueError):
        raise ValueError("A hidden part's strength is a whole number from 1 to 100") from None
    if not 1 <= strength <= 100:
        raise ValueError("A hidden part's strength is from 1 to 100")
    return {"x": x, "y": y, "w": min(w, round(1 - x, 5)), "h": min(h, round(1 - y, 5)), "how": how,
            "strength": strength}


def parse_hide(text: str) -> dict:
    """--hide "pixelate:10,10,20,20" (how, then left, top, width, height in percent; how may be left
    out, and a strength added: "blur/40:...")."""
    how, _, box = str(text).rpartition(":")
    how, _, strength = how.partition("/")
    try:
        x, y, w, h = (float(v.strip().removesuffix("%")) / 100 for v in box.split(","))
    except ValueError:
        raise ValueError("--hide is how:left,top,width,height in percent, like pixelate:10,10,20,20") from None
    return check_hide({"x": x, "y": y, "w": w, "h": h, "how": how.strip().lower() or "pixelate",
                       "strength": strength or 70})


def check_mark(mark: dict) -> dict:
    """One drawing: its kind, its points (shares of the picture: two for an arrow, line, box or
    ellipse, any number for the pen and highlighter, one for a sticker), colour, width (a share of
    the picture's shorter side), whether a box or ellipse is filled, and a sticker's name, size and number."""
    if not isinstance(mark, dict):
        raise ValueError("A drawing is a set of its kind, points and colour")  # noqa: TRY004 - callers turn ValueError into the message
    kind = mark.get("kind")
    if kind not in MARK_KINDS:
        raise ValueError(f"A drawing is one of: {', '.join(MARK_KINDS)}")
    pts = mark.get("points")
    if not isinstance(pts, list) or not pts or len(pts) > POINTS_MAX:
        raise ValueError(f"A drawing has 1 to {POINTS_MAX} points")
    points = []
    for pt in pts:
        if not isinstance(pt, (list, tuple)) or len(pt) != 2:
            raise ValueError("A drawing's point is [x, y]")
        points.append([_share(pt[0], "A drawing's x"), _share(pt[1], "A drawing's y")])
    need = {"sticker": 1, "pen": 1, "highlight": 1}.get(kind, 2)
    if len(points) < need or (kind in ("arrow", "line", "box", "ellipse") and len(points) != 2):
        raise ValueError(f"A{'n' if kind[0] in 'aeiou' else ''} {kind} has {'2 points' if need == 2 else 'a point'}")
    try:
        width = float(mark.get("width", 0.006))
        size = float(mark.get("size", 0.08))
    except (TypeError, ValueError):
        raise ValueError("A drawing's width and size are numbers") from None
    if not 0.001 <= width <= 0.1:
        raise ValueError("A drawing's width is from 0.001 to 0.1 of the picture")
    out = {"kind": kind, "points": points, "colour": _colour(mark.get("colour", "#ff3b30"), "A drawing's colour"),
           "width": round(width, 5)}
    if kind in ("box", "ellipse"):
        out["fill"] = bool(mark.get("fill", False))
    if kind == "sticker":
        name = mark.get("name", "check")
        if name not in STICKERS:
            raise ValueError(f"A sticker is one of: {', '.join(STICKERS)}")
        if not 0.01 <= size <= 0.5:
            raise ValueError("A sticker's size is from 0.01 to 0.5 of the picture")
        out.update(name=name, size=round(size, 4))
        if name == "number":
            try:
                n = int(mark.get("n", 1))
            except (TypeError, ValueError):
                raise ValueError("A numbered sticker's number is a whole number") from None
            if not 0 <= n <= 99:
                raise ValueError("A numbered sticker's number is from 0 to 99")
            out["n"] = n
    return out


def _json_list(value, what: str, most: int) -> list:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise ValueError(f"{what} is a list, as JSON") from None
    if not isinstance(value, list):
        raise ValueError(f"{what} is a list")  # noqa: TRY004 - callers turn ValueError into the message
    if len(value) > most:
        raise ValueError(f"{what} is at most {most} items")
    return value


def check_hides(value) -> str:
    return json.dumps([check_hide(h) for h in _json_list(value, "Hide", HIDES_MAX)], separators=(",", ":"))


def check_marks(value) -> str:
    return json.dumps([check_mark(m) for m in _json_list(value, "Draw", MARKS_MAX)], separators=(",", ":"))


def items(value) -> list[dict]:
    """The parts to hide or the drawings a "hides" or "marks" setting holds (none when it's unset)."""
    return json.loads(value) if value else []


def check_file(value) -> str:
    """A file's path, as typed or chosen (whether it's there is said when it's used)."""
    path = str(value).strip().strip('"')
    if len(path) > 1000 or any(ord(c) < 32 for c in path):
        raise ValueError("That isn't a file's path")
    return path


# ---- Pages: the pages a PDF keeps, in their order, each maybe turned ("1-3,5,7r")

TURN_MARKS = {"": 0, "r": 1, "u": 2, "l": 3}  # a quarter right, upside down, a quarter left
PAGES_MAX = 100_000


def parse_pages(value) -> list[tuple[int, int]]:
    """A page list as (page index from 0, quarter turns clockwise): "1-3,5,7r" (r a quarter right, l left,
    u upside down; a range may go backwards, "5-1"). ValueError for anything else."""
    text = str(value).replace(" ", "").lower()
    if not text:
        raise ValueError("Pages lists one page at least, like 1-3,5")
    out = []
    for part in text.split(","):
        m = re.fullmatch(r"(\d{1,6})(?:-(\d{1,6}))?([rlu]?)", part)
        if not m:
            raise ValueError("Pages is like 1-3,5,7r (r turns a page a quarter right, l left, u upside down)")
        a, b, quarters = int(m[1]), int(m[2] or m[1]), TURN_MARKS[m[3]]
        if min(a, b) < 1:
            raise ValueError("Pages count from 1")
        if len(out) + abs(b - a) + 1 > PAGES_MAX:
            raise ValueError(f"Pages lists at most {PAGES_MAX:,} pages")
        step = 1 if b >= a else -1
        out += [(i - 1, quarters) for i in range(a, b + step, step)]
    return out


def pages_text(plan: list[tuple[int, int]]) -> str:
    """A page list in one form: pages one after another, turned alike, as a range ("1-3,5,7r")."""
    runs: list[list[int]] = []
    for i, q in plan:
        if runs and runs[-1][2] == q and runs[-1][1] + 1 == i:
            runs[-1][1] = i
        else:
            runs.append([i, i, q])
    marks = {v: k for k, v in TURN_MARKS.items()}
    return ",".join((f"{a + 1}" if a == b else f"{a + 1}-{b + 1}") + marks[q] for a, b, q in runs)


def check_pages(value) -> str:
    return pages_text(parse_pages(value))


FORMS = {  # a setting's form: (its check, what --help shows, what it takes in words)
    "adjust": (check_adjust, "NAME=N,...", f"{', '.join(ADJUSTMENTS)}: -100 to 100 each"),
    "angle": (check_angle, "DEGREES", f"degrees, -{STRAIGHTEN_MOST:g} to {STRAIGHTEN_MOST:g}"),
    "hides": (check_hides, "JSON", "parts to hide (JSON)"),
    "marks": (check_marks, "JSON", "drawings (JSON)"),
    "file": (check_file, "FILE", "a picture file"),
    "pages": (check_pages, "LIST", "pages in order, like 1-3,5,7r"),
}


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
    Setting("rotate", "Turn", "Turn pictures that came out sideways or upside down, or mirror them",
            choices=tuple(TURNS), names=tuple(TURNS.values()), auto="As it is", remember=False, row=False),
    Setting("crop", "Crop to", "Crop pictures to a shape from the middle: square, 4:5 for posts, 16:9 wide, 9:16 for stories",
            choices=("square", "4:5", "16:9", "9:16"),
            names=("Square (1:1)", "4:5, for posts", "16:9, wide", "9:16, for stories and reels"),
            auto="No crop", remember=False, row=False),
    Setting("enhance", "Touch up", "Auto fix a dull or tinted photo, or clean up a photo of a page into white paper and dark writing",
            choices=("auto", "page"), names=("Auto fix colour and contrast", "Clean up a page"),
            auto="Off", remember=False, row=False),
    Setting("colour", "Colour", "Make pictures black and white, or sepia like an old photo",
            choices=("bw", "sepia"), names=("Black and white", "Sepia"), auto="As it is", remember=False, row=False),
    Setting("area", "Crop by hand", "The part of the picture or video to keep, as chosen in the crop window: left, top, width and height in percent",
            auto="All of it", area=True, remember=False, row=False),
    Setting("texts", "Text", "Text on the picture or video, as placed in the crop window", auto="None", boxes=True,
            remember=False, row=False),
    Setting("deepfry", "Deep fry", "The meme look: colours, contrast and sharpness turned way up, and crunched like a JPG saved over and over. Higher is crispier",
            1, 100, unit="%", auto="Off", slider=True, remember=False, row=False),
    Setting("adjust", "Adjust", "Brightness, contrast, saturation, warmth and sharpness, each from -100 to 100, like brightness=10,contrast=5",
            auto="As it is", form="adjust", remember=False, row=False),
    Setting("straighten", "Straighten", "Turn a crooked picture or video level by a few degrees (clockwise is more than 0), cropped in a little so no corner is empty",
            auto="Level as it is", form="angle", remember=False, row=False),
    Setting("hides", "Hide", "Parts of the picture or video blurred, pixelated or covered, as drawn in the editor",
            auto="None", form="hides", remember=False, row=False),
    Setting("marks", "Draw", "Arrows, lines, boxes, circles, pen and highlighter strokes and stickers, as drawn in the editor",
            auto="None", form="marks", remember=False, row=False),
    Setting("watermark", "Watermark", "Your own text in a corner of every picture and video, small and see-through, like your name or site",
            auto="None", text=True, row=False),
    Setting("watermark_image", "Watermark picture", "Your own picture (a logo, best as a PNG with a see-through background) small in a corner of every picture and video",
            auto="None", form="file", row=False),
    Setting("watermark_opacity", "Watermark opacity", "How solid the watermark is: 100 is fully solid, lower is more see-through",
            5, 100, unit="%", auto="60%", slider=True, row=False),
    Setting("watermark_at", "Watermark in", "The corner the watermark goes in",
            choices=("bottom-right", "bottom-left", "top-right", "top-left"),
            names=("Bottom right", "Bottom left", "Top right", "Top left"), auto="Bottom right", row=False),
    Setting("speed", "Speed", "Play sound and video faster or slower", unit="x",
            choices=("0.25", "0.5", "0.75", "1.25", "1.5", "2", "4"),
            names=("0.25x, a quarter", "0.5x, half", "0.75x", "1.25x", "1.5x", "2x, double", "4x"),
            auto="Normal", remember=False),
    Setting("pitch", "Pitch", "When the speed changes, keep voices sounding the same, or let them go higher and lower like a tape",
            choices=("change",), names=("Changes with the speed",), auto="Kept"),
    Setting("reverse", "Backwards", "Play sound and video backwards (up to 10 minutes long)",
            choices=("yes",), names=("Yes",), auto="No", remember=False),
    Setting("pages", "Pages", "The pages a PDF keeps, in this order, any turned: 1-3,5,7r (r a quarter right, l left, u upside down)",
            auto="All of them, as they are", form="pages", remember=False, row=False),
    Setting("split", "Split into", "Make a PDF of every this many pages, together in a folder (1: one PDF for each page)",
            1, PAGES_MAX, unit=" pages", auto="One PDF", common=(1, 2, 5, 10, 20, 50), remember=False),
    Setting("compress", "Compress", "Make the PDF smaller: the pictures in it saved again at 150 dpi (Smaller) or 200 dpi (Balanced); its text stays text",
            choices=("smaller", "balanced"), names=("Smaller: pictures at 150 dpi", "Balanced: pictures at 200 dpi"),
            auto="As it is"),
    Setting("searchable", "Make searchable", "Read the text of scanned pages (OCR) and add it to the PDF unseen, so it can be searched and copied; pages that have text already stay as they are",
            choices=("yes",), names=("Yes: read the text of scanned pages",), auto="No", remember=False),
    Setting("background", "Background", "Take the background out of pictures (see-through), or make it white, black or blurred like a phone's portrait mode; the subject is found by a smart tool",
            choices=("remove", "white", "black", "blur"),
            names=("Taken out (see-through)", "White", "Black", "Blurred, like portrait mode"), auto="As it is",
            remember=False, row=False),
    Setting("upscale", "Upscale", "Make pictures 2 or 4 times bigger with the detail drawn in (sharp edges, not blur), by a smart tool",
            choices=("2", "4"), names=("2x bigger", "4x bigger"), auto="Original size", remember=False),
    Setting("subtitles_from", "Subtitles from", "Take the video's own subtitle track, or write down what's said (speech recognition, a smart tool)",
            choices=("speech",), names=("What's said (speech recognition)",), auto="The video's subtitle track",
            remember=False),
    Setting("speech_subs", "Subtitles of what's said", "Write down what's said and add it to the video: burned into the picture, or as a subtitle track that can be turned off",
            choices=("burn", "track"), names=("Burned into the picture", "As a subtitle track"), auto="None",
            remember=False),
    Setting("language", "Language", "The language that's spoken, for speech recognition (it's heard from the first 30 seconds when not set)",
            choices=tuple(BY_NAME), names=tuple(BY_NAME.values()), auto="Heard from the recording"),
    Setting("detail", "Mesh detail", "How finely CAD parts are cut into triangles: finer is smoother and bigger",
            choices=("coarse", "normal", "fine"), auto="Normal"),
    Setting("page_size", "Page size", "The page an ebook is laid out on when it becomes a PDF",
            choices=("phone", "ereader", "a5", "a4", "letter"),
            names=("Phone", "E-reader (6 inch)", "A5", "A4", "Letter"), auto="A5"),
    Setting("text_size", "Text size", "How big an ebook's text is when it becomes a PDF",
            choices=("small", "normal", "large"), auto="Normal"),
)}

DEFAULT = "Default"  # Latheport's own settings: the preset that sets nothing

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
        if value in (None, "") or value == []:
            continue
        if key not in SETTINGS:
            raise ValueError(f"Unknown setting '{key}'")
        value = SETTINGS[key].check(value)
        if value not in ("", "[]"):  # no text, no boxes: not set
            out[key] = value
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
    if os.environ.get("LATHEPORT_PRESETS"):  # tests, or a portable setup
        return Path(os.environ["LATHEPORT_PRESETS"])
    base = os.environ.get("APPDATA") if sys.platform == "win32" else os.environ.get("XDG_CONFIG_HOME")
    return Path(base or Path.home() / ".config") / "Latheport" / "presets.json"


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
