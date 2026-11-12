"""What's been done in the editor: for each file its crop, texts, hidden parts, drawings and look, or
the edit every file shares; the history behind Undo and Redo; and the settings each file's conversion
takes from it."""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from pathlib import Path

from .. import options

FULL = (0.0, 0.0, 1.0, 1.0)
TIME = ("start", "end", "speed", "pitch", "reverse")  # a video's trim and pace: always the file's own
LOOK = ("rotate", "straighten", "adjust", "enhance", "colour", "deepfry", "background")  # settings for the whole picture


def written(texts: list[dict]) -> list[dict]:
    """The texts that have something in them (one being typed may not yet)."""
    return [t for t in texts if t["text"].strip()]


@dataclass
class Edit:
    box: tuple[float, float, float, float] = FULL   # the part kept: a share of the picture as turned
    lock: tuple[int, int] | None = None              # the shape the box keeps (width, height)
    texts: list[dict] = field(default_factory=list)
    look: dict = field(default_factory=dict)          # LOOK's settings, as options takes them
    hides: list[dict] = field(default_factory=list)   # parts hidden, on the picture as turned (options.check_hide)
    marks: list[dict] = field(default_factory=list)   # drawings, on the picture as turned (options.check_mark)

    def copy(self) -> Edit:
        return copy.deepcopy(self)

    def empty(self) -> bool:
        return (self.box == FULL and not self.lock and not written(self.texts) and not any(self.look.values())
                and not self.hides and not self.marks)

    def picture_settings(self) -> dict:
        """What changes the picture itself (as the stage shows it under the crop): the look and the
        hidden parts."""
        out = {k: v for k, v in self.look.items() if v}
        if self.hides:
            out["hides"] = options.check_hides(self.hides)
        return out

    def turned(self, size: tuple[int, int]) -> tuple[int, int]:
        """size as the picture lies once turned."""
        return (size[1], size[0]) if options.sideways(self.look.get("rotate")) else size

    def fitted_box(self, size: tuple[int, int]) -> tuple[float, float, float, float]:
        """The box for a picture of size (upright, not yet turned): kept to its shape on this picture, about
        as big as it is and around the same middle, inside the picture."""
        x, y, w, h = self.box
        if not self.lock:
            return self.box
        tw, th = self.turned(size)
        ratio = self.lock[0] / self.lock[1]
        area = max(w * tw * h * th, 1.0)
        pw = math.sqrt(area * ratio)
        ph = pw / ratio
        scale = min(1.0, tw / pw, th / ph)
        pw, ph = pw * scale, ph * scale
        cx, cy = (x + w / 2) * tw, (y + h / 2) * th
        px = min(max(cx - pw / 2, 0.0), tw - pw)
        py = min(max(cy - ph / 2, 0.0), th - ph)
        return (px / tw, py / th, pw / tw, ph / th)

    def settings(self, size: tuple[int, int] | None) -> dict:
        """What this edit asks of the conversion of a picture of size (None: not known; a locked box is
        then kept as it is)."""
        out = {k: v for k, v in self.look.items() if v}
        x, y, w, h = self.fitted_box(size) if size else self.box
        if (w < 0.999 or h < 0.999) and w > 0 and h > 0:
            out["area"] = options.check_area(",".join(f"{v * 100:.2f}" for v in (x, y, w, h)))
        if written(self.texts):
            out["texts"] = options.check_boxes(written(self.texts))
        if self.hides:
            out["hides"] = options.check_hides(self.hides)
        if self.marks:
            out["marks"] = options.check_marks(self.marks)
        return options.clean(out)

    def turn(self, quarters: int = 0, mirror: bool = False) -> None:
        """Turn the picture a quarter at a time (clockwise) or mirror it, the crop box, the hidden parts
        and the drawings going with it (and a straightening the other way when mirrored)."""
        m, q = options.orientation(self.look.get("rotate"))
        x, y, w, h = self.box
        if mirror:  # mirroring the picture as it lies: mirrored first, turned the other way
            m, q = not m, -q
            x = 1 - x - w
            if self.look.get("straighten"):
                self.look["straighten"] = options.check_angle(-float(self.look["straighten"]))
        for _ in range(quarters % 4):  # a quarter right: the left edge becomes the top
            x, y, w, h = 1 - y - h, x, h, w
            q += 1
        self.box = tuple(round(v, 6) for v in (x, y, w, h))
        for hide in self.hides:
            hx, hy, hw, hh = hide["x"], hide["y"], hide["w"], hide["h"]
            if mirror:
                hx = 1 - hx - hw
            for _ in range(quarters % 4):
                hx, hy, hw, hh = 1 - hy - hh, hx, hh, hw
            hide.update(x=round(hx, 5), y=round(hy, 5), w=round(hw, 5), h=round(hh, 5))
        for mark in self.marks:
            pts = mark["points"]
            if mirror:
                pts = [[1 - px, py] for px, py in pts]
            for _ in range(quarters % 4):
                pts = [[1 - py, px] for px, py in pts]
            mark["points"] = [[round(px, 5), round(py, 5)] for px, py in pts]
        turn = options.turn_of(m, q)
        if turn:
            self.look["rotate"] = turn
        else:
            self.look.pop("rotate", None)


class Edits:
    """Every file's edit: the one they share, and each file's own (an empty own edit: left as it is); and
    each video's trim and pace (Start at, End at, Speed, Pitch, Backwards), which are never shared."""

    def __init__(self, files: list[Path]):
        self.files = list(files)
        self.shared = Edit()
        self.own: dict[Path, Edit] = {}
        self.times: dict[Path, dict] = {}

    def set_time(self, path: Path, **change) -> None:
        """Change a video's trim or pace (None takes a setting off)."""
        got = dict(self.times.get(path, {}), **change)
        got = options.clean({k: v for k, v in got.items() if v is not None})
        if got.get("end") and options.parse_time(got["end"]) <= options.parse_time(got.get("start") or 0):
            got.pop("end")
        if got:
            self.times[path] = got
        else:
            self.times.pop(path, None)

    def of(self, path: Path) -> Edit:
        return self.own.get(path, self.shared)

    def scope(self, path: Path) -> str:
        """"all" (it follows the shared edit), "this" (its own) or "none" (left as it is)."""
        if path not in self.own:
            return "all"
        return "none" if self.own[path].empty() else "this"

    def set_scope(self, path: Path, scope: str) -> None:
        if scope == "all":
            self.own.pop(path, None)
        elif scope == "this":
            self.own[path] = (self.own[path] if self.scope(path) == "this" else self.shared).copy()
        else:
            self.own[path] = Edit()

    def snapshot(self) -> tuple:
        return copy.deepcopy((self.shared, self.own, self.times))

    def restore(self, snap: tuple) -> None:
        self.shared, self.own, self.times = copy.deepcopy(snap)

    def settings(self, sizes: dict[Path, tuple[int, int] | None]) -> dict[Path, dict]:
        """Each file's settings, for the files the edits change."""
        out = {}
        for path in self.files:
            got = {**self.of(path).settings(sizes.get(path)), **self.times.get(path, {})}
            if got:
                out[path] = got
        return out


class History:
    """Undo and Redo: the edits after each finished change (a drag, a text, a slider let go)."""
    DEPTH = 100

    def __init__(self, edits: Edits):
        self.edits = edits
        self.states = [edits.snapshot()]
        self.at = 0

    def record(self) -> None:
        snap = self.edits.snapshot()
        if snap == self.states[self.at]:
            return
        del self.states[self.at + 1:]
        self.states.append(snap)
        if len(self.states) > self.DEPTH:
            self.states.pop(0)
        self.at = len(self.states) - 1

    def can_undo(self) -> bool:
        return self.at > 0

    def can_redo(self) -> bool:
        return self.at < len(self.states) - 1

    def undo(self) -> None:
        if self.can_undo():
            self.at -= 1
            self.edits.restore(self.states[self.at])

    def redo(self) -> None:
        if self.can_redo():
            self.at += 1
            self.edits.restore(self.states[self.at])


def render(picture, look: dict):
    """picture (a Pillow image) with the look and the hidden parts as the conversion will do them
    (turned, straightened, hidden, adjusted, touched up, recoloured, deep fried): what the stage shows.
    Runs off the window's thread."""
    from .. import registry
    from ..converters import images
    settings = {k: v for k, v in look.items() if v}
    if not settings:
        return picture
    tokens = registry.running(options.clean(settings), [], [])
    try:
        return images.effects([images.prepare(picture.copy(), "png")], "png")[0]
    finally:
        registry.reset(tokens)
