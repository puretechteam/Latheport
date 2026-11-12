"""Where text really lands, measured in pixels from a window's own grab (no mock-ups): the ink of each
label, button, box and field, against its own widget and against the others in its row.

The ink is whatever differs clearly from the widget's background. Letters with and without descenders
sit differently in a box, so the vertical position compared is the middle of the capital letters: the
baseline is read off the ink, and the capital height comes from the font.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise

import numpy as np
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QFontMetrics, QImage
from PySide6.QtWidgets import (
    QAbstractButton,
    QBoxLayout,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QRadioButton,
    QWidget,
)

INSET = 4        # pixels left out around each widget: its border and rounded corners are not text
THRESHOLD = 40   # how far (per colour channel) a pixel must be from the background to be ink
TOLERANCE = 2.0  # pixels
PADDED = {"section"}  # labels that have padding-top on purpose: their words are not meant to sit in the middle


@dataclass
class Ink:
    widget: QWidget
    text: str
    box: QRect            # the widget, in the grabbed window's pixels
    ink: tuple            # left, top, right, bottom (right and bottom exclusive)
    cap_cy: float         # middle of the capital letters
    ink_cy: float         # middle of the ink as it is
    centred_x: bool       # text meant to sit in the middle of its widget, left to right
    edge: bool = False    # the ink reaches the edge of what was measured (clipped, or a border leaked in)
    dpr: float = 1.0      # all the numbers above are device pixels
    baseline: float = 0.0 # where the letters stand, read off the ink
    size: int = 0         # the font's line height

    @property
    def dy_own(self) -> float:
        return self.cap_cy - (self.box.top() + self.box.height() / 2)

    @property
    def dx_own(self) -> float:
        return (self.ink[0] + self.ink[2]) / 2 - (self.box.left() + self.box.width() / 2)

    @property
    def name(self) -> str:
        return f"{type(self.widget).__name__}({self.text[:24]!r})"


def to_array(img: QImage) -> np.ndarray:
    img = img.convertToFormat(QImage.Format_RGBA8888)
    w, h = img.width(), img.height()
    buf = np.frombuffer(img.constBits(), dtype=np.uint8, count=img.bytesPerLine() * h)
    return buf.reshape(h, img.bytesPerLine() // 4, 4)[:, :w, :3].astype(np.int16)


def _text(w: QWidget) -> str:
    if isinstance(w, QLabel):
        if w.pixmap() is not None and not w.pixmap().isNull() or w.wordWrap() or w.textFormat() == Qt.RichText or ("<" in w.text() and ">" in w.text()):
            return ""
        return w.text()
    if isinstance(w, QAbstractButton):
        return w.text() if QAbstractButton.icon(w).isNull() else ""
    if isinstance(w, QComboBox):
        return w.currentText()
    if isinstance(w, QLineEdit):
        return w.text()
    return ""


def _ink_box(arr: np.ndarray, r: QRect):
    crop = arr[r.top():r.bottom() + 1, r.left():r.right() + 1]
    if crop.size == 0:
        return None
    flat = (crop[..., 0].astype(np.int32) << 16) | (crop[..., 1].astype(np.int32) << 8) | crop[..., 2]
    values, counts = np.unique(flat, return_counts=True)
    bg = int(values[counts.argmax()])
    bgc = np.array([(bg >> 16) & 255, (bg >> 8) & 255, bg & 255], dtype=np.int16)
    mask = np.abs(crop - bgc).max(axis=2) > THRESHOLD
    if not mask.any():
        return None
    ys, xs = np.where(mask)
    return (r.left() + int(xs.min()), r.top() + int(ys.min()), r.left() + int(xs.max()) + 1, r.top() + int(ys.max()) + 1), r


def measure(top: QWidget, img: QImage | None = None) -> dict[QWidget, Ink]:
    """Every visible single-line text widget under top, measured from top's grab."""
    img = img if img is not None else top.grab().toImage()
    dpr = img.devicePixelRatio()
    arr = to_array(img)
    found: dict[QWidget, Ink] = {}
    for w in top.findChildren(QWidget):
        if not w.isVisible() or w.width() < 8:
            continue
        if w.visibleRegion().boundingRect().size() != w.size():
            continue  # scrolled partly out of sight: what is shown is not all of it
        text = _text(w)
        if not text.strip():
            continue
        if isinstance(w, QAbstractButton) and w.metaObject().className() == "CaptionButton":
            continue
        fm = QFontMetrics(w.font())
        if w.height() > fm.lineSpacing() * 2.2:
            continue  # more than a line tall and not a plain box: its text may be anywhere in it
        p = w.mapTo(top, w.rect().topLeft())
        box = QRect(round(p.x() * dpr), round(p.y() * dpr), round(w.width() * dpr), round(w.height() * dpr))
        if isinstance(w, QLabel):  # the margins are space around its words, not part of where they sit
            m = w.contentsMargins()
            box = box.adjusted(round(m.left() * dpr), round(m.top() * dpr), -round(m.right() * dpr), -round(m.bottom() * dpr))
        inset = round((1 if isinstance(w, QLabel) else INSET) * dpr)
        meas = box.adjusted(inset, inset, -inset, -inset)
        if isinstance(w, QComboBox):
            meas.setRight(box.right() - round(24 * dpr))
        got = _ink_box(arr, meas)
        if got is None:
            continue
        (l, t, r, b), meas = got
        tight = fm.tightBoundingRect(text)
        cap = fm.tightBoundingRect("H")
        base_top = t - tight.y() * dpr                           # the baseline, from the top of the ink
        base_bottom = b - (tight.y() + tight.height()) * dpr     # and from its bottom
        base = (base_top + base_bottom) / 2
        cap_cy = base - cap.height() * dpr / 2
        centred = (isinstance(w, QAbstractButton) and not isinstance(w, (QCheckBox, QRadioButton))) or (isinstance(w, QLabel) and bool(w.alignment() & Qt.AlignHCenter))
        edge = fm.horizontalAdvance(text) > w.width() + 1  # more words than room
        found[w] = Ink(w, text, box, (l, t, r, b), cap_cy, (t + b) / 2, centred, edge, dpr, base, fm.height())
    return found


def rows(top: QWidget, found: dict[QWidget, Ink]) -> list[list[Ink]]:
    """The measured widgets that sit side by side in one horizontal layout, two or more at a time."""
    out = []
    layouts = [lay for lay in top.findChildren(QHBoxLayout)] + ([top.layout()] if isinstance(top.layout(), QHBoxLayout) else [])
    for lay in layouts:
        if not isinstance(lay, QBoxLayout):
            continue
        row = []
        for i in range(lay.count()):
            w = lay.itemAt(i).widget()
            if w is not None and w in found:
                row.append(found[w])
        if len(row) >= 2:
            out.append(row)
    return out


def problems(top: QWidget, img: QImage | None = None) -> list[str]:
    """Plain sentences, one per thing more than TOLERANCE pixels out; empty when all is centred."""
    found = measure(top, img)
    tol = TOLERANCE
    bad = []
    for ink in found.values():
        dpr = ink.dpr
        if abs(ink.dy_own) / dpr > tol and ink.widget.objectName() not in PADDED:
            bad.append(f"{ink.name} sits {ink.dy_own / dpr:+.1f}px from the middle of its own box")
        if ink.centred_x and abs(ink.dx_own) / dpr > tol:
            bad.append(f"{ink.name} is {ink.dx_own / dpr:+.1f}px off the middle of its box, left to right")
        if ink.edge:
            bad.append(f"{ink.name}: ink reaches the edge of its box (clipped?)")
    for row in rows(top, found):
        ys = [i.cap_cy / i.dpr for i in row]
        lo, hi = row[ys.index(min(ys))], row[ys.index(max(ys))]
        if max(ys) - min(ys) > tol:  # side by side, whatever their size: the middles of their capitals line up
            bad.append(f"row: {lo.name} and {hi.name} differ by {max(ys) - min(ys):.1f}px")
    return bad + overlaps(top)


def overlaps(top: QWidget) -> list[str]:
    """Widgets side by side in one horizontal layout that lie over each other (a row squeezed below its minimum)."""
    out = []
    layouts = list(top.findChildren(QHBoxLayout)) + ([top.layout()] if isinstance(top.layout(), QHBoxLayout) else [])
    for lay in layouts:
        ws = [lay.itemAt(i).widget() for i in range(lay.count())]
        ws = [w for w in ws if w is not None and w.isVisible()]
        for a, b in pairwise(ws):
            over = a.geometry().right() + 1 - b.geometry().left()
            if over > 0:
                label = lambda w: getattr(w, "text", lambda: "")() if hasattr(w, "text") else ""
                out.append(f"overlap: {type(a).__name__}({label(a)!r}) and {type(b).__name__}({label(b)!r}) by {over}px")
    return out


def span(top: QWidget, ink: Ink, img: QImage | None = None, columns: int = 7) -> tuple[int, int]:
    """The top and bottom of the first letter of ink's text (a capital or a digit), read from the pixels only. The
    background is taken from the whole text's box: a capital's stem can fill most of a few columns."""
    arr = to_array(img if img is not None else top.grab().toImage())
    l, r = ink.ink[0], ink.ink[2]
    t, b = ink.box.top(), ink.box.bottom() + 1  # the whole box top to bottom: the ink's own box leaves out its edge rows
    whole = arr[t:b, l:r]
    flat = (whole[..., 0].astype(np.int32) << 16) | (whole[..., 1].astype(np.int32) << 8) | whole[..., 2]
    values, counts = np.unique(flat, return_counts=True)
    bg = int(values[counts.argmax()])
    bgc = np.array([(bg >> 16) & 255, (bg >> 8) & 255, bg & 255], dtype=np.int16)
    rows = np.where((np.abs(arr[t:b, l:l + columns] - bgc).max(axis=2) > THRESHOLD).any(axis=1))[0]
    return t + int(rows.min()), t + int(rows.max()) + 1
