"""The stage: the picture on dark grey, zoomed and panned, with what the tool in use draws on it.

Crop: a box with handles over a dimmed surround, a grid of thirds while it moves, a line through the
middle when it's centred, edges that snap to the picture's, and a label with its size in pixels that
names the shape (1:1, 4:5, 16:9...) when it is one, snapping to it.
Text, as in Photoshop: click on the picture and type there; click a text to type in it or drag it
(it snaps to the middle); Enter or Esc when done; Delete removes the one chosen.
Hide: drag a box over what to hide (blurred, pixelated or covered); drag one to move it, its corners
to size it. Draw: drag an arrow, line, box or ellipse (Shift keeps lines at 45 degrees and boxes
square), draw with the pen or highlighter, click to place a sticker; Select picks one up to move it.
Everywhere: the wheel zooms where the pointer is, Space-drag or the middle button pans, and holding
Compare shows the picture as it was.
"""

from __future__ import annotations

import copy
import itertools
import json
import math
import re

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QImage,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QApplication, QWidget

from .. import options, overlay
from ..errors import ConversionError
from .look import INK
from .model import FULL, Edit, written

SNAP = 7            # screen pixels: an edge or a middle this close snaps to it
NEAR = 0.015        # a free box this close to a known shape becomes it
HANDLE = 9
MARGIN = 28         # around the picture when it's fitted
MOST_ZOOM = 8.0     # times the fitted size
SHAPES = [("1:1", 1, 1), ("4:5", 4, 5), ("3:4", 3, 4), ("2:3", 2, 3), ("3:2", 3, 2), ("4:3", 4, 3),
          ("16:9", 16, 9), ("9:16", 9, 16)]
STYLE = {"font": "sans", "size": 0.07, "colour": "#ffffff", "edge": "shadow"}  # a new text's look, to start
MARK_STYLE = {"colour": "#ff3b30", "width": 0.006, "fill": False, "name": "check", "size": 0.08}  # a new drawing's
HIDE_STYLE = {"how": "pixelate", "strength": 70}  # a new hidden part's
WATERMARK = {"text": "", "at": "bottom-right", "image": "", "opacity": 60}
CHECK = 10          # the checkerboard's squares, in screen pixels
_CHECKS = None      # its tile, made once there's an application
TYPING_KEYS = (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down, Qt.Key_Home, Qt.Key_End, Qt.Key_PageUp,
               Qt.Key_PageDown, Qt.Key_Delete)  # no caret to move yet: while typing, these do nothing
SMALLEST = 6        # screen pixels: a box or line dragged shorter than this was a click


def _dist(p: QPointF, a: QPointF, b: QPointF) -> float:
    """How far p is from the line from a to b."""
    dx, dy = b.x() - a.x(), b.y() - a.y()
    t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((p.x() - a.x()) * dx + (p.y() - a.y()) * dy) / (dx * dx + dy * dy)))
    return math.hypot(p.x() - a.x() - t * dx, p.y() - a.y() - t * dy)


class Stage(QWidget):
    changed = Signal()      # the edit changed (live, while dragging or typing)
    committed = Signal()    # a change finished: one step for Undo
    picked = Signal(int)    # the text chosen (-1: none)
    zoomed = Signal(float)  # how big a real pixel is on screen
    chose = Signal(int)     # the hidden part or drawing chosen (-1: none)
    retouched = Signal()    # a part was hidden, moved or taken off: the picture is made again
    space_tapped = Signal() # Space pressed and let go without looking around: a video plays or pauses

    def __init__(self):
        super().__init__()
        self.setObjectName("stage")
        self.picture: QImage | None = None   # the picture as the result will look (turned, recoloured...)
        self.original: QImage | None = None  # and as the file is, for Compare
        self.real = (0, 0)                   # the real picture's size as turned, in pixels
        self.edit = Edit()
        self.mode = "crop"
        self.before = False
        self.hint = ""
        self.mark = dict(WATERMARK)          # the watermark, shown where it will be
        self.draw_kind = "arrow"             # what Draw makes: a MARK_KINDS, or "select" to pick one up
        self.mark_style = dict(MARK_STYLE)
        self.hide_style = dict(HIDE_STYLE)
        self.chosen = -1                     # the hidden part (Hide) or drawing (Draw) chosen
        self.straightening = False           # a grid over the picture while Straighten is dragged
        self._drawn: tuple | None = None     # (key, QImage): the drawings, kept until they change
        self.video = False                   # a video: Space on its own plays and pauses
        self._space_moved = False            # Space was held to look around, not tapped
        self.zoom, self.pan = 1.0, QPointF(0, 0)
        self.sel = -1
        self.editing = -1                    # the text being typed in
        self.fresh = False                   # the next key replaces its words (a meme's "TOP TEXT")
        self.style = dict(STYLE)             # how the next text looks (the bar sets it)
        self.drag: tuple | None = None       # (what, where it started, the box or text then)
        self.panning: QPointF | None = None
        self.space = False
        self.guides: set[str] = set()        # "v", "h": a middle line is showing
        self.shape = ""
        self._layer: tuple | None = None     # (key, QImage): the texts drawn, kept until they change
        self.caret = True
        self.blink = QTimer(self)
        self.blink.setInterval(530)
        self.blink.timeout.connect(self._blink)
        self.setMouseTracking(True)
        self.setMinimumSize(480, 320)
        self.setFocusPolicy(Qt.StrongFocus)

    # -- what it shows

    def open_file(self, picture: QImage | None, original: QImage | None, real: tuple[int, int], edit: Edit) -> None:
        """A file: its picture as it will look, as it is, its real size (turned) and its edit."""
        self.commit()
        self.picture, self.original, self.real, self.edit = picture, original, real, edit
        self.sel = self.chosen = -1
        self._layer = self._drawn = None
        self.zoom, self.pan = 1.0, QPointF(0, 0)
        if edit.lock and picture is not None:
            self._fit_lock()
        self._name_shape()
        self.zoomed.emit(self.scale())
        self.update()

    def set_live(self, picture: QImage) -> None:
        """A video's frame while it plays: the picture changes, nothing drawn over it has to be made again."""
        self.picture = picture
        self.update()

    def set_picture(self, picture: QImage | None, real: tuple[int, int]) -> None:
        """The same file looking different (turned, recoloured): the box stays a share of it."""
        self.picture, self.real = picture, real
        self._layer = self._drawn = None
        self._name_shape()
        self.zoomed.emit(self.scale())
        self.update()

    # -- where things are

    def _fit_scale(self) -> float:
        w, h = self.real
        if not w or not h:
            return 1.0
        return min((self.width() - 2 * MARGIN) / w, (self.height() - 2 * MARGIN) / h)

    def scale(self) -> float:
        """Screen pixels a real pixel takes."""
        return self._fit_scale() * self.zoom

    def image_rect(self) -> QRectF:
        if self.picture is None:
            return QRectF()
        s = self.scale()
        iw, ih = self.real[0] * s, self.real[1] * s
        return QRectF((self.width() - iw) / 2 + self.pan.x(), (self.height() - ih) / 2 + self.pan.y(), iw, ih)

    def box_rect(self) -> QRectF:
        r = self.image_rect()
        x, y, w, h = self.edit.box
        return QRectF(r.x() + x * r.width(), r.y() + y * r.height(), w * r.width(), h * r.height())

    def _set_box(self, b: QRectF) -> None:
        r = self.image_rect()
        box = ((b.x() - r.x()) / r.width(), (b.y() - r.y()) / r.height(), b.width() / r.width(), b.height() / r.height())
        self.edit.box = tuple(round(min(1.0, max(0.0, v)), 6) for v in box)
        self._name_shape()

    def _name_shape(self) -> None:
        _, _, w, h = self.edit.box
        if not self.real[0] or not h:
            self.shape = ""
            return
        ratio = (w * self.real[0]) / max(1e-9, h * self.real[1])
        self.shape = next((n for n, a, b in SHAPES if abs(ratio / (a / b) - 1) < 0.004), "")

    def pixel_size(self) -> tuple[int, int]:
        _, _, w, h = self.edit.box
        return round(w * self.real[0]), round(h * self.real[1])

    # -- zoom and pan

    def zoom_to(self, zoom: float, at: QPointF | None = None) -> None:
        """Zoom (1 = fitted) keeping the point at (the middle by default) where it is."""
        zoom = min(MOST_ZOOM, max(1.0, zoom))
        r = self.image_rect()
        at = at or QPointF(self.width() / 2, self.height() / 2)
        u = QPointF((at.x() - r.x()) / max(r.width(), 1), (at.y() - r.y()) / max(r.height(), 1))
        self.zoom = zoom
        r2 = self.image_rect()
        self.pan += QPointF(at.x() - (r2.x() + u.x() * r2.width()), at.y() - (r2.y() + u.y() * r2.height()))
        self._clamp_pan()
        self.zoomed.emit(self.scale())
        self.update()

    def fit(self) -> None:
        self.zoom, self.pan = 1.0, QPointF(0, 0)
        self.zoomed.emit(self.scale())
        self.update()

    def _clamp_pan(self) -> None:
        if self.zoom <= 1.0:
            self.pan = QPointF(0, 0)
            return
        s = self.scale()
        room_x = max(0.0, (self.real[0] * s - self.width()) / 2 + MARGIN)
        room_y = max(0.0, (self.real[1] * s - self.height()) / 2 + MARGIN)
        self.pan = QPointF(min(room_x, max(-room_x, self.pan.x())), min(room_y, max(-room_y, self.pan.y())))

    def wheelEvent(self, e) -> None:
        steps = e.angleDelta().y() / 120
        if steps:
            self.zoom_to(self.zoom * 1.15 ** steps, e.position())

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._clamp_pan()
        self.zoomed.emit(self.scale())

    # -- shapes

    def set_lock(self, lock: tuple[int, int] | None) -> None:
        """Lock the box to a shape: the biggest box of it around the box's middle, inside the picture."""
        self.edit.lock = lock
        if lock and self.picture is not None:
            self._fit_lock(biggest=True)
        self._name_shape()
        self.changed.emit()
        self.committed.emit()
        self.update()

    def _fit_lock(self, biggest: bool = False) -> None:
        r, b = self.image_rect(), self.box_rect()
        ratio = self.edit.lock[0] / self.edit.lock[1]
        if biggest:
            w = min(r.width(), r.height() * ratio)
        else:  # about as big as it is
            w = min(r.width(), r.height() * ratio, (b.width() * b.height() * ratio) ** 0.5)
        h = w / ratio
        c = b.center()
        self._set_box(self._clamp_move(QRectF(c.x() - w / 2, c.y() - h / 2, w, h), r))

    def reset_box(self) -> None:
        self.edit.box = FULL
        if self.edit.lock:
            self._fit_lock(biggest=True)
        self._name_shape()
        self.changed.emit()
        self.committed.emit()
        self.update()

    # -- the mouse

    def _handle_at(self, p: QPointF) -> str | None:
        return self._handle_on(self.box_rect(), p)

    @staticmethod
    def _handle_on(b: QRectF, p: QPointF) -> str | None:
        """The handle of box b at p ("n", "se"...), "move" inside it, or None."""
        xs = {"w": b.left(), "c": b.center().x(), "e": b.right()}
        ys = {"n": b.top(), "c": b.center().y(), "s": b.bottom()}
        for yk, y in ys.items():
            for xk, x in xs.items():
                if (xk, yk) != ("c", "c") and abs(p.x() - x) <= HANDLE and abs(p.y() - y) <= HANDLE:
                    return (yk if yk != "c" else "") + (xk if xk != "c" else "")
        return "move" if b.contains(p) else None

    def _bounds(self) -> list:
        """Where each text is on the screen (left, top, right, bottom), or None for one with no words yet."""
        b = self.box_rect()
        size = (max(1, round(b.width())), max(1, round(b.height())))
        shown = [i for i, t in enumerate(self.edit.texts) if t["text"].strip()]
        out: list = [None] * len(self.edit.texts)
        for i, (x0, y0, x1, y1) in zip(shown, overlay.text_bounds(size, [self.edit.texts[i] for i in shown])):
            out[i] = (b.x() + x0, b.y() + y0, b.x() + x1, b.y() + y1)
        return out

    def _text_at(self, p: QPointF) -> int:
        bounds = self._bounds()
        for i in reversed(range(len(bounds))):
            if bounds[i] is not None:
                x0, y0, x1, y1 = bounds[i]
                if QRectF(x0 - 6, y0 - 6, x1 - x0 + 12, y1 - y0 + 12).contains(p):
                    return i
        return -1

    def mousePressEvent(self, e) -> None:
        if self.picture is None or self.before:
            return
        p = e.position()
        if e.button() == Qt.MiddleButton or (e.button() == Qt.LeftButton and (self.space or self.mode == "adjust")):
            self._space_moved = self._space_moved or self.space
            self.panning = p - self.pan
            self.setCursor(Qt.ClosedHandCursor)
            return
        if e.button() != Qt.LeftButton:
            return
        self.setFocus()
        if self.mode == "text":
            self._text_press(p)
        elif self.mode == "hide":
            self._hide_press(p)
        elif self.mode == "draw":
            self._draw_press(p)
        elif self.mode == "crop":
            what = self._handle_at(p)
            if what:
                self.drag = (what, p, self.box_rect())
        self.update()

    def _text_press(self, p: QPointF) -> None:
        i = self._text_at(p)
        if i < 0 or i != self.editing:
            was_typing = self.editing >= 0
            self.commit()
            i = self._text_at(p)
            if i < 0 and was_typing:  # a click away from the text being typed: that one's done
                self.sel = -1
                self.picked.emit(-1)
                return
        if i >= 0:  # a text: type in it, or drag it
            self.sel = self.editing = i
            self.fresh = False
            t = self.edit.texts[i]
            self.drag = ("text", p, (t["x"], t["y"]))
        elif self.box_rect().contains(p):  # somewhere else on the picture: a new text there
            self.new_text(p)
        else:
            self.sel = -1
        self.picked.emit(self.sel)
        self._caret_on()

    def mouseMoveEvent(self, e) -> None:
        p = e.position()
        if self.panning is not None:
            self.pan = p - self.panning
            self._clamp_pan()
            self.update()
            return
        if self.drag is None:
            self._hover(p)
            return
        what, start, then = self.drag
        if what == "text":
            self._move_text(p - start, then)
        elif what.startswith("hide-"):
            self._hide_drag(what, p, start, then)
        elif what.startswith("mark-"):
            self._mark_drag(what, p, start, then, bool(e.modifiers() & Qt.ShiftModifier))
        elif what == "move":
            self._move_box(then.translated(p - start))
        else:
            self._resize(what, p, then)
        self.changed.emit()
        self.update()

    def _hover(self, p: QPointF) -> None:
        if self.space or self.mode == "adjust":
            self.setCursor(Qt.OpenHandCursor if self.zoom > 1 or self.space else Qt.ArrowCursor)
        elif self.mode == "hide":
            on = self._handle_on(self._hide_rect(self.edit.hides[self.chosen]), p) if 0 <= self.chosen < len(self.edit.hides) else None
            self.setCursor(self._cursor(on) if on and on != "move" else Qt.SizeAllCursor if self._hide_at(p) >= 0
                           else Qt.CrossCursor if self.image_rect().contains(p) else Qt.ArrowCursor)
        elif self.mode == "draw":
            if self.draw_kind == "select":
                self.setCursor(Qt.SizeAllCursor if self._mark_at(p) >= 0 else Qt.ArrowCursor)
            else:
                self.setCursor(Qt.CrossCursor if self.image_rect().contains(p) else Qt.ArrowCursor)
        elif self.mode == "crop":
            self.setCursor(self._cursor(self._handle_at(p)))
        else:
            self.setCursor(Qt.SizeAllCursor if self._text_at(p) >= 0 else
                           Qt.IBeamCursor if self.box_rect().contains(p) else Qt.ArrowCursor)

    @staticmethod
    def _cursor(handle: str | None):
        return {"move": Qt.SizeAllCursor, "n": Qt.SizeVerCursor, "s": Qt.SizeVerCursor, "e": Qt.SizeHorCursor,
                "w": Qt.SizeHorCursor, "nw": Qt.SizeFDiagCursor, "se": Qt.SizeFDiagCursor, "ne": Qt.SizeBDiagCursor,
                "sw": Qt.SizeBDiagCursor}.get(handle or "", Qt.ArrowCursor)

    def mouseReleaseEvent(self, e) -> None:
        if self.panning is not None:
            self.panning = None
            self._hover(e.position())
            return
        if self.drag is not None:
            what = self.drag[0]
            moved = what != "text" or (e.position() - self.drag[1]).manhattanLength() > 2
            if what in ("hide-new", "mark-new") and self._too_small(what):  # a click: nothing made
                (self.edit.hides if what == "hide-new" else self.edit.marks).pop()
                self._drawn = None
                self._choose(-1)
                moved = False
            elif what.startswith(("hide-", "mark-")) and (e.position() - self.drag[1]).manhattanLength() == 0 \
                    and what not in ("hide-new", "mark-new"):
                moved = False  # chosen, not moved
            self.drag = None
            self.guides.clear()
            if moved:
                if what.startswith("hide-"):
                    self.retouched.emit()
                self.changed.emit()
                self.committed.emit()
            self.update()

    def mouseDoubleClickEvent(self, e) -> None:
        if self.mode in ("crop", "adjust") and e.button() == Qt.LeftButton:
            self.fit() if self.zoom > 1 else self.zoom_to(2.0, e.position())

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key_Space and not e.isAutoRepeat() and self.editing < 0:
            self.space, self._space_moved = True, False
            self.setCursor(Qt.OpenHandCursor)
            return
        if e.key() == Qt.Key_Space and self.editing < 0:
            return
        if self.mode == "text" and self.editing >= 0:
            self._type(e)
        elif self.mode == "text" and self.sel >= 0 and e.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.remove_text(self.sel)
        elif self.mode in ("hide", "draw") and self.chosen >= 0 and e.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.remove_chosen()
        elif self.mode in ("hide", "draw") and self.chosen >= 0 and e.key() == Qt.Key_Escape:
            self._choose(-1)
        else:
            super().keyPressEvent(e)

    def keyReleaseEvent(self, e) -> None:
        if e.key() == Qt.Key_Space and not e.isAutoRepeat() and self.space:
            self.space = False
            self.unsetCursor()
            if self.video and not self._space_moved:  # tapped, not held to look around: play or pause
                self.space_tapped.emit()
            return
        super().keyReleaseEvent(e)

    def _type(self, e) -> None:
        t = self.edit.texts[self.editing]
        if e.key() in (Qt.Key_Escape, Qt.Key_Return, Qt.Key_Enter):  # done: the bar is for the next text now
            self.commit()
            self.sel = -1
            self.picked.emit(-1)
            return
        if e.key() == Qt.Key_Backspace:
            t["text"] = "" if self.fresh else t["text"][:-1]
        elif e.matches(QKeySequence.Paste):  # on the one line a text takes
            pasted = re.sub(r"\s+", " ", QApplication.clipboard().text())
            t["text"] = (("" if self.fresh else t["text"]) + "".join(c for c in pasted if c.isprintable()))[:options.TEXT_MAX]
        elif e.text() and e.text().isprintable():
            t["text"] = (("" if self.fresh else t["text"]) + e.text())[:options.TEXT_MAX]
        elif e.key() in TYPING_KEYS:  # kept in the text: they'd switch file in the window
            e.accept()
            return
        else:
            super().keyPressEvent(e)
            return
        self.fresh = False
        self._layer = None
        self.caret = True
        self.changed.emit()
        self.update()

    # -- moving and sizing the box, with the snaps

    def _clamp_move(self, b: QRectF, r: QRectF) -> QRectF:
        dx = max(0.0, r.left() - b.left()) - max(0.0, b.right() - r.right())
        dy = max(0.0, r.top() - b.top()) - max(0.0, b.bottom() - r.bottom())
        return b.translated(dx, dy)

    def _move_box(self, b: QRectF) -> None:
        r = self.image_rect()
        b = self._clamp_move(b, r)
        self.guides.clear()
        if abs(b.center().x() - r.center().x()) <= SNAP:
            b.moveCenter(QPointF(r.center().x(), b.center().y()))
            self.guides.add("v")
        if abs(b.center().y() - r.center().y()) <= SNAP:
            b.moveCenter(QPointF(b.center().x(), r.center().y()))
            self.guides.add("h")
        for edge, to in ((b.left(), r.left()), (b.right(), r.right())):
            if 0 < abs(edge - to) <= SNAP:
                b.translate(to - edge, 0)
        for edge, to in ((b.top(), r.top()), (b.bottom(), r.bottom())):
            if 0 < abs(edge - to) <= SNAP:
                b.translate(0, to - edge)
        self._set_box(self._clamp_move(b, r))

    def _resize(self, handle: str, p: QPointF, then: QRectF) -> None:
        r = self.image_rect()
        x0, y0, x1, y1 = then.left(), then.top(), then.right(), then.bottom()
        px = min(max(p.x(), r.left()), r.right())
        py = min(max(p.y(), r.top()), r.bottom())
        for edge in (r.left(), r.right()):
            if abs(px - edge) <= SNAP:
                px = edge
        for edge in (r.top(), r.bottom()):
            if abs(py - edge) <= SNAP:
                py = edge
        if "w" in handle:
            x0 = min(px, x1 - 16)
        if "e" in handle:
            x1 = max(px, x0 + 16)
        if "n" in handle:
            y0 = min(py, y1 - 16)
        if "s" in handle:
            y1 = max(py, y0 + 16)
        b = QRectF(QPointF(x0, y0), QPointF(x1, y1))
        ratio = self.edit.lock[0] / self.edit.lock[1] if self.edit.lock else None
        if ratio is None and len(handle) == 2:  # free: a corner near a known shape snaps to it
            have = b.width() / b.height()
            near = min(SHAPES, key=lambda s: abs(have / (s[1] / s[2]) - 1))
            if abs(have / (near[1] / near[2]) - 1) < NEAR:
                ratio = near[1] / near[2]
        if ratio:
            b = self._keep_ratio(handle, b, ratio, then, r)
        self._set_box(b)
        self.guides.clear()

    def _keep_ratio(self, handle: str, b: QRectF, ratio: float, then: QRectF, r: QRectF) -> QRectF:
        """b made ratio wide for its height, from the corner or edge across from the handle, inside r."""
        if len(handle) == 2:
            w = max(b.width(), b.height() * ratio)
            ax = then.right() if "w" in handle else then.left()
            ay = then.bottom() if "n" in handle else then.top()
            room_x = ax - r.left() if "w" in handle else r.right() - ax
            room_y = ay - r.top() if "n" in handle else r.bottom() - ay
            w = min(w, room_x, room_y * ratio)
            h = w / ratio
            return QRectF(ax - w if "w" in handle else ax, ay - h if "n" in handle else ay, w, h)
        c = then.center()
        if handle in ("e", "w"):
            h = min(b.width() / ratio, r.height())
            w = h * ratio
            nb = QRectF(b.right() - w if handle == "w" else b.left(), c.y() - h / 2, w, h)
        else:
            w = min(b.height() * ratio, r.width())
            h = w / ratio
            nb = QRectF(c.x() - w / 2, b.bottom() - h if handle == "n" else b.top(), w, h)
        return self._clamp_move(nb, r)

    # -- texts

    def _move_text(self, delta: QPointF, then: tuple[float, float]) -> None:
        b = self.box_rect()
        t = self.edit.texts[self.sel]
        x = min(1.0, max(0.0, then[0] + delta.x() / b.width()))
        y = min(1.0, max(0.0, then[1] + delta.y() / b.height()))
        self.guides.clear()
        if abs((x - 0.5) * b.width()) <= SNAP:
            x = 0.5
            self.guides.add("v")
        if abs((y - 0.5) * b.height()) <= SNAP:
            y = 0.5
            self.guides.add("h")
        t["x"], t["y"] = round(x, 4), round(y, 4)
        self._layer = None

    def new_text(self, p: QPointF) -> None:
        """A text where p is, looking as the bar says, empty and being typed in."""
        b = self.box_rect()
        x = min(1.0, max(0.0, (p.x() - b.x()) / b.width()))
        y = min(1.0, max(0.0, (p.y() - b.y()) / b.height()))
        self.edit.texts.append(dict(self.style, text="", x=round(x, 4), y=round(y, 4), meme=False))
        self.sel = self.editing = len(self.edit.texts) - 1
        self.fresh = False
        self._layer = None
        self.changed.emit()
        self.update()

    def add_text(self, box: dict, typing: bool = False) -> None:
        """A whole text box (a meme caption); typing: its words are replaced by the next key."""
        self.edit.texts.append(options.check_box(box))
        self.sel = len(self.edit.texts) - 1
        if typing:
            self.editing, self.fresh = self.sel, True
            self.setFocus()
            self._caret_on()
        self._layer = None
        self.picked.emit(self.sel)
        self.changed.emit()
        self.update()

    def commit(self) -> None:
        """Done typing: a text left with no words goes; one step for Undo."""
        was = self.editing
        if 0 <= self.editing < len(self.edit.texts) and not self.edit.texts[self.editing]["text"].strip():
            del self.edit.texts[self.editing]
            self.sel = -1
            self._layer = None
            self.changed.emit()
        self.editing, self.fresh = -1, False
        self.blink.stop()
        if was >= 0:
            self.committed.emit()
        self.update()

    def update_text(self, commit: bool = True, **changes) -> None:
        """A change to the text chosen; commit=False while a slider is still being dragged (one Undo step, at the end)."""
        if not 0 <= self.sel < len(self.edit.texts):
            return
        self.edit.texts[self.sel].update(changes)
        self._layer = None
        self.changed.emit()
        if self.editing < 0 and commit:
            self.committed.emit()
        self.update()

    def remove_text(self, i: int) -> None:
        if 0 <= i < len(self.edit.texts):
            del self.edit.texts[i]
            self.editing, self.fresh = -1, False
            self.blink.stop()
            self.sel = -1
            self._layer = None
            self.picked.emit(-1)
            self.changed.emit()
            self.committed.emit()
            self.update()

    def _caret_on(self) -> None:
        self.caret = True
        if self.editing >= 0:
            self.blink.start()
        else:
            self.blink.stop()

    def _blink(self) -> None:
        self.caret = not self.caret
        self.update()

    # -- hidden parts and drawings: placed on the picture as turned, as shares of its width and height

    def _share(self, p: QPointF) -> list[float]:
        r = self.image_rect()
        return [round(min(1.0, max(0.0, (p.x() - r.x()) / r.width())), 5),
                round(min(1.0, max(0.0, (p.y() - r.y()) / r.height())), 5)]

    def _at(self, x: float, y: float) -> QPointF:
        r = self.image_rect()
        return QPointF(r.x() + x * r.width(), r.y() + y * r.height())

    def _hide_rect(self, hide: dict) -> QRectF:
        r = self.image_rect()
        return QRectF(r.x() + hide["x"] * r.width(), r.y() + hide["y"] * r.height(), hide["w"] * r.width(), hide["h"] * r.height())

    def _hide_at(self, p: QPointF) -> int:
        return next((i for i in reversed(range(len(self.edit.hides))) if self._hide_rect(self.edit.hides[i]).contains(p)), -1)

    def _mark_rect(self, mark: dict) -> QRectF:
        r = self.image_rect()
        x0, y0, x1, y1 = overlay.mark_bounds(mark, (max(1.0, r.width()), max(1.0, r.height())))
        return QRectF(r.x() + x0, r.y() + y0, x1 - x0, y1 - y0)

    def _on_mark(self, m: dict, p: QPointF) -> bool:
        """Whether p is on drawing m: its line (within a few pixels), or anywhere on a filled shape or a sticker."""
        if not self._mark_rect(m).adjusted(-4, -4, 4, 4).contains(p):
            return False
        if m["kind"] == "sticker" or (m["kind"] in ("box", "ellipse") and m.get("fill")):
            return True
        r = self.image_rect()
        pts = [self._at(x, y) for x, y in m["points"]]
        reach = max(6.0, m["width"] * min(r.width(), r.height()) * (4 if m["kind"] == "highlight" else 1))
        if m["kind"] in ("box", "ellipse"):
            box = QRectF(pts[0], pts[1]).normalized()
            inner = box.adjusted(reach, reach, -reach, -reach)
            return box.adjusted(-reach, -reach, reach, reach).contains(p) and not (inner.isValid() and inner.contains(p))
        return any(_dist(p, a, b) <= reach for a, b in (itertools.pairwise(pts) if len(pts) > 1 else [(pts[0], pts[0])]))

    def _mark_at(self, p: QPointF) -> int:
        return next((i for i in reversed(range(len(self.edit.marks))) if self._on_mark(self.edit.marks[i], p)), -1)

    def _choose(self, i: int) -> None:
        self.chosen = i
        self.chose.emit(i)
        self.update()

    def _hide_press(self, p: QPointF) -> None:
        hides = self.edit.hides
        if 0 <= self.chosen < len(hides):
            handle = self._handle_on(self._hide_rect(hides[self.chosen]), p)
            if handle and handle != "move":
                self.drag = ("hide-size", p, (self.chosen, handle, self._hide_rect(hides[self.chosen])))
                return
        i = self._hide_at(p)
        if i >= 0:
            self._choose(i)
            self.drag = ("hide-move", p, (i, dict(hides[i])))
        elif self.image_rect().contains(p):
            x, y = self._share(p)
            hides.append({"x": x, "y": y, "w": 0.0, "h": 0.0, **self.hide_style})
            self._choose(len(hides) - 1)
            self.drag = ("hide-new", p, (len(hides) - 1, (x, y)))
        else:
            self._choose(-1)

    def _hide_drag(self, what: str, p: QPointF, start: QPointF, then: tuple) -> None:
        r = self.image_rect()
        hide = self.edit.hides[then[0]]
        if what == "hide-new":
            (x0, y0), (x1, y1) = then[1], self._share(p)
            hide.update(x=min(x0, x1), y=min(y0, y1), w=round(abs(x1 - x0), 5), h=round(abs(y1 - y0), 5))
            return
        if what == "hide-move":
            old = then[1]
            hide["x"] = round(min(1 - old["w"], max(0.0, old["x"] + (p.x() - start.x()) / r.width())), 5)
            hide["y"] = round(min(1 - old["h"], max(0.0, old["y"] + (p.y() - start.y()) / r.height())), 5)
            return
        _, handle, box = then
        px, py = min(max(p.x(), r.left()), r.right()), min(max(p.y(), r.top()), r.bottom())
        x0, y0, x1, y1 = box.left(), box.top(), box.right(), box.bottom()
        x0 = min(px, x1 - SMALLEST) if "w" in handle else x0
        x1 = max(px, x0 + SMALLEST) if "e" in handle else x1
        y0 = min(py, y1 - SMALLEST) if "n" in handle else y0
        y1 = max(py, y0 + SMALLEST) if "s" in handle else y1
        (sx, sy), (ex, ey) = self._share(QPointF(x0, y0)), self._share(QPointF(x1, y1))
        hide.update(x=sx, y=sy, w=round(ex - sx, 5), h=round(ey - sy, 5))

    def _draw_press(self, p: QPointF) -> None:
        if self.draw_kind == "select":
            i = self._mark_at(p)
            self._choose(i)
            if i >= 0:
                self.drag = ("mark-move", p, (i, copy.deepcopy(self.edit.marks[i]["points"])))
            return
        if not self.image_rect().contains(p):
            self._choose(-1)
            return
        at, kind, style = self._share(p), self.draw_kind, self.mark_style
        mark = {"kind": kind, "points": [at] if kind in ("pen", "highlight", "sticker") else [at, list(at)],
                "colour": style["colour"], "width": style["width"]}
        if kind in ("box", "ellipse"):
            mark["fill"] = style["fill"]
        if kind == "sticker":
            mark.update(name=style["name"], size=style["size"])
            if style["name"] == "number":  # numbered in the order they're put down
                mark["n"] = 1 + sum(1 for m in self.edit.marks if m.get("name") == "number")
        self.edit.marks.append(mark)
        self._drawn = None
        self._choose(len(self.edit.marks) - 1)
        self.drag = ("mark-new", p, (self.chosen, at))
        self.changed.emit()

    def _mark_drag(self, what: str, p: QPointF, start: QPointF, then: tuple, shift: bool) -> None:
        m = self.edit.marks[then[0]]
        self._drawn = None
        if what == "mark-move":  # all of it moved, kept on the picture
            r, pts = self.image_rect(), then[1]
            dx = min(max((p.x() - start.x()) / r.width(), -min(q[0] for q in pts)), 1 - max(q[0] for q in pts))
            dy = min(max((p.y() - start.y()) / r.height(), -min(q[1] for q in pts)), 1 - max(q[1] for q in pts))
            m["points"] = [[round(x + dx, 5), round(y + dy, 5)] for x, y in pts]
        elif m["kind"] in ("pen", "highlight"):
            if (p - self._at(*m["points"][-1])).manhattanLength() >= 3 and len(m["points"]) < options.POINTS_MAX:
                m["points"].append(self._share(p))
        elif m["kind"] == "sticker":
            m["points"] = [self._share(p)]
        else:
            if shift:  # lines at 45 degrees, boxes and ellipses square
                d = p - start
                if m["kind"] in ("box", "ellipse"):
                    side = max(abs(d.x()), abs(d.y()))
                    p = start + QPointF(math.copysign(side, d.x() or 1), math.copysign(side, d.y() or 1))
                else:
                    angle = round(math.atan2(d.y(), d.x()) / (math.pi / 4)) * math.pi / 4
                    length = math.hypot(d.x(), d.y())
                    p = start + QPointF(length * math.cos(angle), length * math.sin(angle))
            m["points"][1] = self._share(p)

    def _too_small(self, what: str) -> bool:
        """Whether the box or line just made is too small to be one (it was a click)."""
        if what == "hide-new":
            hide = self.edit.hides[-1]
            r = self.image_rect()
            return hide["w"] * r.width() < SMALLEST or hide["h"] * r.height() < SMALLEST
        m = self.edit.marks[-1]
        if m["kind"] in ("arrow", "line", "box", "ellipse"):
            a, b = (self._at(*q) for q in m["points"])
            return (a - b).manhattanLength() < SMALLEST
        return False

    def remove_chosen(self) -> None:
        items = self.edit.hides if self.mode == "hide" else self.edit.marks
        if 0 <= self.chosen < len(items):
            del items[self.chosen]
            self._drawn = None
            if self.mode == "hide":
                self.retouched.emit()
            self._choose(-1)
            self.changed.emit()
            self.committed.emit()

    def update_chosen(self, **changes) -> None:
        """A change in the bar: to the hidden part or drawing chosen (as update_text does for texts)."""
        items = self.edit.hides if self.mode == "hide" else self.edit.marks
        if not 0 <= self.chosen < len(items):
            return
        item = items[self.chosen]
        if self.mode == "draw":
            changes = {k: v for k, v in changes.items() if k in ("colour", "width") or (k == "fill" and "fill" in item)
                       or (k in ("name", "size") and item["kind"] == "sticker")}
            if changes.get("name") == "number" and "n" not in item:
                changes["n"] = 1 + sum(1 for m in self.edit.marks if m.get("name") == "number")
        item.update(changes)
        self._drawn = None
        if self.mode == "hide":
            self.retouched.emit()
        self.changed.emit()
        self.committed.emit()
        self.update()

    # -- drawing

    def _layer_size(self, r: QRectF) -> tuple[int, int]:
        """The pixels to draw a layer over r with: the screen's, or the picture's own when zoomed in past them (the
        layer is then drawn bigger, as the conversion's would look, never rendered at 8 times the picture)."""
        s = max(1.0, self.scale())
        return max(1, round(r.width() / s)), max(1, round(r.height() / s))

    def _texts_image(self, b: QRectF) -> QImage | None:
        size = self._layer_size(b)
        texts = written(self.edit.texts)
        mark = self.mark
        if not texts and not mark["text"] and not mark["image"]:
            return None
        key = (size, json.dumps(texts), json.dumps(mark, sort_keys=True))
        if self._layer is None or self._layer[0] != key:
            from PIL import Image
            layer = overlay.text_layer(size, texts) if texts else Image.new("RGBA", size)
            if mark["text"] or mark["image"]:
                try:
                    logo = overlay.watermark_picture(mark["image"]) if mark["image"] else None
                except ConversionError:  # gone or not a picture: the window says so
                    logo = None
                layer = Image.alpha_composite(layer, overlay.watermark_layer(size, mark["text"], mark["at"], False,
                                                                             mark["opacity"], logo))
            self._layer = (key, QImage(layer.tobytes(), size[0], size[1], size[0] * 4, QImage.Format_RGBA8888).copy())
        return self._layer[1]

    def _marks_image(self, r: QRectF) -> QImage | None:
        if not self.edit.marks:
            return None
        size = self._layer_size(r)
        key = (size, json.dumps(self.edit.marks))
        if self._drawn is None or self._drawn[0] != key:
            layer = overlay.marks_layer(size, self.edit.marks)
            self._drawn = (key, QImage(layer.tobytes(), size[0], size[1], size[0] * 4, QImage.Format_RGBA8888).copy())
        return self._drawn[1]

    def _checks(self, p: QPainter, r: QRectF) -> None:
        """A checkerboard under the picture, for its see-through parts: one tiled fill of what's on the screen."""
        global _CHECKS
        if _CHECKS is None:
            tile = QPixmap(2 * CHECK, 2 * CHECK)
            tile.fill(QColor("#3a3b40"))
            t = QPainter(tile)
            t.fillRect(0, 0, CHECK, CHECK, QColor("#2e2f33"))
            t.fillRect(CHECK, CHECK, CHECK, CHECK, QColor("#2e2f33"))
            t.end()
            _CHECKS = QBrush(tile)
        p.save()
        p.setClipRect(r)
        p.fillRect(r.intersected(QRectF(self.rect())), _CHECKS)
        p.restore()

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.fillRect(self.rect(), QColor(INK["stage"]))
        if self.picture is None:
            p.setPen(QColor(INK["muted"]))
            p.drawText(self.rect(), Qt.AlignCenter, "This file can't be shown here.\nIt's converted as it is.")
            return
        if self.before and self.original is not None:
            self._paint_before(p)
            return
        r, b = self.image_rect(), self.box_rect()
        self._checks(p, r)
        p.drawImage(r, self.picture)
        drawn = self._marks_image(r)
        if drawn is not None:
            self._draw_layer(p, r, drawn)
        shade = QPainterPath()
        shade.addRect(r)
        inner = QPainterPath()
        inner.addRect(b)
        p.fillPath(shade.subtracted(inner), QColor(0, 0, 0, 150 if self.mode == "crop" else 190))
        layer = self._texts_image(b)
        if layer is not None:
            self._draw_layer(p, b, layer)
        accent = QColor(INK["accent"])
        if self.drag is not None and self.drag[0] != "text":  # thirds while the box moves
            p.setPen(QPen(QColor(255, 255, 255, 110), 1))
            for k in (1, 2):
                x, y = b.left() + b.width() * k / 3, b.top() + b.height() * k / 3
                p.drawLine(QPointF(x, b.top()), QPointF(x, b.bottom()))
                p.drawLine(QPointF(b.left(), y), QPointF(b.right(), y))
        area = b if self.mode == "text" else r
        p.setPen(QPen(accent, 1.5))
        if "v" in self.guides:
            p.drawLine(QPointF(area.center().x(), area.top()), QPointF(area.center().x(), area.bottom()))
        if "h" in self.guides:
            p.drawLine(QPointF(area.left(), area.center().y()), QPointF(area.right(), area.center().y()))
        if self.mode == "crop":
            p.setPen(QPen(QColor(255, 255, 255, 230), 1.2))
            p.setBrush(Qt.NoBrush)
            p.drawRect(b)
            self._handles(p, b)
            self._size_label(p, b, accent)
        elif self.mode == "text":
            self._text_marks(p, b, accent)
        elif self.mode == "hide":
            self._hide_marks(p, accent)
        elif self.mode == "draw" and 0 <= self.chosen < len(self.edit.marks):
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(accent, 1.5, Qt.DashLine))
            p.drawRect(self._mark_rect(self.edit.marks[self.chosen]).adjusted(-3, -3, 3, 3))
        if self.straightening:  # a fine grid to line the picture up against
            p.setPen(QPen(QColor(255, 255, 255, 70), 1))
            for k in range(1, 8):
                x, y = r.left() + r.width() * k / 8, r.top() + r.height() * k / 8
                p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
                p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        self._paint_hint(p)
        p.end()

    def _draw_layer(self, p: QPainter, r: QRectF, layer: QImage) -> None:
        if self.scale() > 1:  # made at the picture's own pixels: shown bigger
            p.drawImage(r, layer)
        else:
            p.drawImage(r.topLeft(), layer)

    def _hide_marks(self, p: QPainter, accent: QColor) -> None:
        """Each hidden part's outline; the one chosen in the accent, with handles to size it."""
        p.setBrush(Qt.NoBrush)
        for i, hide in enumerate(self.edit.hides):
            box = self._hide_rect(hide)
            p.setPen(QPen(QColor(0, 0, 0, 160), 3))
            p.drawRect(box)
            p.setPen(QPen(accent if i == self.chosen else QColor(255, 255, 255, 220), 1.5,
                          Qt.SolidLine if i == self.chosen else Qt.DashLine))
            p.drawRect(box)
            if i == self.chosen:
                p.setBrush(QColor(255, 255, 255))
                for x in (box.left(), box.center().x(), box.right()):
                    for y in (box.top(), box.center().y(), box.bottom()):
                        if (x, y) != (box.center().x(), box.center().y()):
                            p.drawRect(QRectF(x - 3.5, y - 3.5, 7, 7))
                p.setBrush(Qt.NoBrush)

    def _handles(self, p: QPainter, b: QRectF) -> None:
        """Corner brackets and side bars, as photo editors draw them."""
        pen = QPen(QColor(255, 255, 255), 3)
        pen.setCapStyle(Qt.SquareCap)
        p.setPen(pen)
        arm = min(16.0, b.width() / 4, b.height() / 4)
        for sx, x in ((1, b.left()), (-1, b.right())):
            for sy, y in ((1, b.top()), (-1, b.bottom())):
                p.drawLine(QPointF(x, y), QPointF(x + sx * arm, y))
                p.drawLine(QPointF(x, y), QPointF(x, y + sy * arm))
        c = b.center()
        for a, z in (((c.x() - arm / 2, b.top()), (c.x() + arm / 2, b.top())),
                     ((c.x() - arm / 2, b.bottom()), (c.x() + arm / 2, b.bottom())),
                     ((b.left(), c.y() - arm / 2), (b.left(), c.y() + arm / 2)),
                     ((b.right(), c.y() - arm / 2), (b.right(), c.y() + arm / 2))):
            p.drawLine(QPointF(*a), QPointF(*z))

    def _text_marks(self, p: QPainter, b: QRectF, accent: QColor) -> None:
        if not 0 <= self.sel < len(self.edit.texts):
            return
        t = self.edit.texts[self.sel]
        line = t["size"] * b.height()
        at = self._bounds()[self.sel]
        if at is None:  # nothing typed yet: a box where it will go
            cx, cy = b.x() + t["x"] * b.width(), b.y() + t["y"] * b.height()
            at = (cx - line * 0.4, cy - line * 0.6, cx + line * 0.4, cy + line * 0.6)
        x0, y0, x1, y1 = at
        typing = self.editing == self.sel
        pad = 12 if typing else 5  # room for the caret inside
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(accent, 1.5, Qt.DashLine))
        p.drawRect(QRectF(x0 - pad, y0 - pad, x1 - x0 + 2 * pad, y1 - y0 + 2 * pad))
        if typing and self.caret:  # where the next letter goes: white edged in black, seen on anything
            x = x1 + 4 if t["text"].strip() else (x0 + x1) / 2
            top, bottom = QPointF(x, max(y0, y1 - line * 1.1)), QPointF(x, y1)
            p.setPen(QPen(QColor(0, 0, 0, 200), 4))
            p.drawLine(top, bottom)
            p.setPen(QPen(QColor(255, 255, 255), 2))
            p.drawLine(top, bottom)

    def _pill(self, p: QPainter, text: str, at: QPointF, fill: QColor) -> QRectF:
        f = QFont(self.font())
        f.setPointSizeF(8.5)
        f.setWeight(QFont.DemiBold)
        p.setFont(f)
        fm = p.fontMetrics()
        pill = QRectF(at.x(), at.y(), fm.horizontalAdvance(text) + 18, fm.height() + 8)
        p.setPen(Qt.NoPen)
        p.setBrush(fill)
        p.drawRoundedRect(pill, pill.height() / 2, pill.height() / 2)
        p.setPen(QColor(255, 255, 255))
        p.drawText(pill, Qt.AlignCenter, text)
        return pill

    def _size_label(self, p: QPainter, b: QRectF, accent: QColor) -> None:
        """"1080 × 1350 · 4:5" at the box's top-left, accented when it's a known shape."""
        w, h = self.pixel_size()
        text = f"{w} × {h}" + (f"  ·  {self.shape}" if self.shape else "")
        at = QPointF(b.left() + 10, b.top() + 10)
        if b.width() < 150 or b.height() < 60:
            at = QPointF(b.left(), max(4.0, b.top() - 30))
        self._pill(p, text, at, accent if self.shape else QColor(0, 0, 0, 170))

    def _paint_hint(self, p: QPainter) -> None:
        if not self.hint:
            return
        f = QFont(self.font())
        f.setPointSizeF(8.5)
        p.setFont(f)
        p.setPen(QColor(INK["muted"]))
        p.drawText(QRectF(14, self.height() - 26, self.width() - 28, 20), Qt.AlignLeft | Qt.AlignVCenter, self.hint)

    def _paint_before(self, p: QPainter) -> None:
        img = self.original
        avail = QRectF(MARGIN, MARGIN, self.width() - 2 * MARGIN, self.height() - 2 * MARGIN)
        s = min(avail.width() / img.width(), avail.height() / img.height())
        r = QRectF(0, 0, img.width() * s, img.height() * s)
        r.moveCenter(avail.center())
        self._checks(p, r)
        p.drawImage(r, img)
        self._pill(p, "Before", QPointF(r.left() + 10, r.top() + 10), QColor(0, 0, 0, 170))
        p.end()
