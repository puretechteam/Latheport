"""The timeline under a video: a strip of its pictures, the playhead, and handles at each end of the part
kept (Start at, End at). Click or drag on it to go there; drag a handle to trim, the picture following it."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from .look import INK

HANDLE = 10          # pixels: each trim handle's width, and how near the pointer picks it up
SHORTEST = 0.1       # seconds: the least a trim keeps
TOP, STRIP = 8, 44   # the room above the strip (the playhead's knob), the strip's height


class Timeline(QWidget):
    seek = Signal(float)              # go here (a click, a drag, a handle being moved)
    trimmed = Signal(object, object)  # the part kept, once a handle is let go: start, end (None: the very start/end)

    def __init__(self):
        super().__init__()
        self.setObjectName("timeline")
        self.setMinimumHeight(TOP + STRIP + 6)
        self.setMouseTracking(True)
        self.duration = 0.0
        self.position = 0.0
        self.start: float | None = None
        self.end: float | None = None
        self.pictures: list = []
        self.drag: str | None = None   # "start", "end" or "seek"
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Click or drag to go there; drag either end to trim (I and O set them where you are)")

    # -- what it shows

    def set_video(self, duration: float) -> None:
        self.duration, self.position, self.start, self.end, self.pictures = max(0.0, duration), 0.0, None, None, []
        self.update()

    def set_pictures(self, pictures: list) -> None:
        self.pictures = list(pictures)
        self.update()

    def set_position(self, seconds: float) -> None:
        self.position = min(max(0.0, seconds), self.duration)
        self.update()

    def set_trim(self, start: float | None, end: float | None) -> None:
        self.start, self.end = start, end
        self.update()

    def kept(self) -> tuple[float, float]:
        return (self.start or 0.0), (self.end if self.end is not None else self.duration)

    # -- where things are

    def _strip(self) -> QRectF:
        return QRectF(HANDLE, TOP, max(1.0, self.width() - 2 * HANDLE), STRIP)

    def x_of(self, seconds: float) -> float:
        r = self._strip()
        return r.left() + (seconds / self.duration if self.duration else 0.0) * r.width()

    def time_at(self, x: float) -> float:
        r = self._strip()
        return min(max(0.0, (x - r.left()) / r.width()), 1.0) * self.duration

    def _handle_at(self, x: float) -> str | None:
        a, b = self.kept()
        xa, xb = self.x_of(a), self.x_of(b)
        if abs(x - xb) <= HANDLE and (x >= xb or abs(x - xb) < abs(x - xa)):
            return "end"
        if abs(x - xa) <= HANDLE:
            return "start"
        return None

    # -- the mouse

    def mousePressEvent(self, e) -> None:
        if e.button() != Qt.LeftButton or not self.duration:
            return
        x = e.position().x()
        self.drag = self._handle_at(x) or "seek"
        self._move(x)

    def mouseMoveEvent(self, e) -> None:
        x = e.position().x()
        if self.drag is None:
            self.setCursor(Qt.SizeHorCursor if self._handle_at(x) else Qt.PointingHandCursor)
            return
        self._move(x)

    def _move(self, x: float) -> None:
        t = self.time_at(x)
        a, b = self.kept()
        if self.drag == "start":
            t = min(t, b - SHORTEST)
            self.start = None if t <= 0.0 else round(t, 3)
        elif self.drag == "end":
            t = max(t, a + SHORTEST)
            self.end = None if t >= self.duration else round(t, 3)
        self.position = max(0.0, t)
        self.seek.emit(self.position)
        self.update()

    def mouseReleaseEvent(self, e) -> None:
        if self.drag in ("start", "end"):
            self.trimmed.emit(self.start, self.end)
        self.drag = None

    # -- drawing

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self._strip()
        clip = QPainterPath()
        clip.addRoundedRect(r, 6, 6)
        p.save()
        p.setClipPath(clip)
        p.fillRect(r, QColor(INK["raised"]))
        if self.pictures:
            n = len(self.pictures)
            slot = r.width() / n
            for i, img in enumerate(self.pictures):  # each picture fills its slot, cut to it from the middle
                target = QRectF(r.left() + i * slot, r.top(), slot + 1, r.height())
                scale = max(target.width() / img.width(), target.height() / img.height())
                sw, sh = target.width() / scale, target.height() / scale
                p.drawImage(target, img, QRectF((img.width() - sw) / 2, (img.height() - sh) / 2, sw, sh))
        a, b = self.kept()
        xa, xb = self.x_of(a), self.x_of(b)
        shade = QColor(0, 0, 0, 165)
        p.fillRect(QRectF(r.left(), r.top(), xa - r.left(), r.height()), shade)  # what's cut off, dimmed
        p.fillRect(QRectF(xb, r.top(), r.right() - xb, r.height()), shade)
        p.restore()
        if not self.duration:
            p.end()
            return
        trimmed = self.start is not None or self.end is not None
        accent = QColor(INK["accent"] if trimmed else INK["text"])
        p.setPen(QPen(accent, 2))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(xa, r.top(), xb - xa, r.height()), 5, 5)
        for x, side in ((xa, -1), (xb, 1)):  # the handles: a bar each end, with a grip
            bar = QRectF(x - HANDLE if side < 0 else x, r.top(), HANDLE, r.height())
            p.setPen(Qt.NoPen)
            p.setBrush(accent)
            p.drawRoundedRect(bar, 4, 4)
            p.setPen(QPen(QColor(INK["stage"]), 1.5))
            mid = bar.center()
            p.drawLine(QPointF(mid.x(), mid.y() - 7), QPointF(mid.x(), mid.y() + 7))
        x = self.x_of(self.position)  # the playhead, a knob on top
        p.setPen(QPen(QColor(0, 0, 0, 140), 4))
        p.drawLine(QPointF(x, TOP - 2), QPointF(x, r.bottom() + 3))
        p.setPen(QPen(QColor(255, 255, 255), 2))
        p.drawLine(QPointF(x, TOP - 2), QPointF(x, r.bottom() + 3))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255))
        p.drawEllipse(QPointF(x, TOP - 3), 5, 5)
        p.end()


def clock(seconds: float) -> str:
    """0:07.4, 1:02:03.0: a time as the timeline shows it, to a tenth."""
    seconds = max(0.0, seconds)
    tenths = round(seconds * 10)
    h, rest = divmod(tenths, 36000)
    m, rest = divmod(rest, 600)
    s, t = divmod(rest, 10)
    return f"{h}:{m:02d}:{s:02d}.{t}" if h else f"{m}:{s:02d}.{t}"
