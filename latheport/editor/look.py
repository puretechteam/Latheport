"""The editor's look: one dark workspace whatever the app's theme (photos read best on dark grey), one
spacing scale, one control height, and icons drawn with QPainter so they're crisp at any size."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QAbstractButton, QHBoxLayout, QPushButton, QWidget

INK = {"stage": "#1b1c1f", "panel": "#25262a", "raised": "#2f3035", "line": "#3a3b41", "text": "#ececf1",
       "muted": "#9a9ba3", "accent": "#f0915a", "accent_hover": "#f5ad83", "accent_text": "#0b0b10",
       "accent_soft": "#46322a", "danger": "#f87171"}
CONTROL = 32   # every button, box and field in the bars
RAIL = 72      # the tool column's width
GAP = 8        # between controls; 4, 12 and 16 elsewhere, nothing in between

QSS = """
#editor, #editor QWidget {{ background: {panel}; color: {text}; font-family: "Segoe UI Variable Text", "Segoe UI", sans-serif; font-size: 9.5pt; }}
#editor QToolTip {{ background: {raised}; color: {text}; border: 1px solid {line}; border-radius: 6px; padding: 5px 8px; }}
#editor QLabel {{ background: transparent; }}
#editor QLabel#muted {{ color: {muted}; }}
#editor QLabel#name {{ font-weight: 600; }}
#editor QLabel#section {{ color: {muted}; font-size: 8pt; font-weight: 600; letter-spacing: 1px; padding-top: 4px; }}
#editor QLabel#value {{ color: {muted}; min-width: 34px; }}
#editor QPushButton {{ background: {raised}; border: 1px solid {line}; border-radius: 8px; padding: 0 12px; min-height: 30px; max-height: 30px; }}
#editor QPushButton:hover {{ border-color: {accent}; }}
#editor QPushButton:checked {{ background: {accent_soft}; border-color: {accent}; }}
#editor QPushButton:disabled {{ color: {muted}; border-color: {line}; }}
#editor QPushButton:checked:disabled {{ background: {raised}; border-color: {line}; color: {muted}; }}
#editor QPushButton#primary {{ background: {accent}; color: {accent_text}; border: 1px solid {accent}; font-weight: 600; padding: 0 20px; }}
#editor QPushButton#primary:hover {{ background: {accent_hover}; border-color: {accent_hover}; }}
#editor QPushButton#segFirst {{ border-top-right-radius: 0; border-bottom-right-radius: 0; }}
#editor QPushButton#segMid {{ border-radius: 0; border-left: none; }}
#editor QPushButton#segLast {{ border-top-left-radius: 0; border-bottom-left-radius: 0; border-left: none; }}
#editor QPushButton#segFirst:checked, #editor QPushButton#segMid:checked, #editor QPushButton#segLast:checked {{ border: 1px solid {accent}; }}
#editor QComboBox {{ background: {raised}; border: 1px solid {line}; border-radius: 8px; padding: 0 10px; min-height: 30px; max-height: 30px; }}
#editor QComboBox:hover {{ border-color: {accent}; }}
#editor QComboBox::drop-down {{ border: none; width: 20px; }}
#editor QComboBox QAbstractItemView {{ background: {raised}; border: 1px solid {line}; selection-background-color: {accent_soft}; outline: none; padding: 4px; }}
#editor QLineEdit {{ background: {stage}; border: 1px solid {line}; border-radius: 8px; padding: 0 10px; min-height: 30px; max-height: 30px; selection-background-color: {accent}; selection-color: {accent_text}; }}
#editor QLineEdit:focus {{ border-color: {accent}; }}
#editor QSlider::groove:horizontal {{ height: 4px; background: {line}; border-radius: 2px; }}
#editor QSlider::sub-page:horizontal {{ background: {accent}; border-radius: 2px; }}
#editor QSlider::handle:horizontal {{ background: {text}; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; }}
#editor QSlider::handle:horizontal:hover {{ background: {accent}; }}
#editor QSlider::sub-page:horizontal:disabled {{ background: {line}; }}
#editor QSlider::handle:horizontal:disabled {{ background: {muted}; }}
#editor #bar {{ border-bottom: 1px solid {line}; }}
#editor #rail {{ border-right: 1px solid {line}; }}
#editor #inspector {{ border-left: 1px solid {line}; }}
#editor #footer {{ border-top: 1px solid {line}; }}
#editor #videobar {{ border-top: 1px solid {line}; }}
#editor QListWidget#strip {{ background: {panel}; border: none; outline: none; }}
#editor QListWidget#strip::item {{ border: 2px solid transparent; border-radius: 6px; margin: 0 2px; }}
#editor QListWidget#strip::item:selected {{ border-color: {accent}; background: transparent; }}
#editor QScrollBar:horizontal {{ background: transparent; height: 6px; }}
#editor QScrollBar::handle:horizontal {{ background: {line}; border-radius: 3px; min-width: 24px; }}
#editor QScrollBar::add-line:horizontal, #editor QScrollBar::sub-line:horizontal {{ width: 0; }}
"""


def stylesheet() -> str:
    return QSS.format(**INK)


# ---------------------------------------------------------------- icons, on a 24 x 24 grid

def _path(name: str) -> QPainterPath:
    """The icon's outline (FILLED says which are filled; the rest are strokes)."""
    p = QPainterPath()
    if name == "crop":
        p.moveTo(7, 2); p.lineTo(7, 17); p.lineTo(22, 17)
        p.moveTo(2, 7); p.lineTo(17, 7); p.lineTo(17, 22)
    elif name == "adjust":
        for y, knob in ((6, 15), (12, 8), (18, 13)):
            p.moveTo(3, y); p.lineTo(21, y)
            p.addEllipse(QPointF(knob, y), 2.2, 2.2)
    elif name == "text":
        p.moveTo(5, 5); p.lineTo(19, 5)
        p.moveTo(12, 5); p.lineTo(12, 20)
        p.moveTo(9, 20); p.lineTo(15, 20)
    elif name == "undo":
        p.moveTo(9, 6); p.lineTo(4, 11); p.lineTo(9, 16)
        p.moveTo(4, 11); p.cubicTo(12, 11, 20, 10, 20, 17)
    elif name == "redo":
        p.moveTo(15, 6); p.lineTo(20, 11); p.lineTo(15, 16)
        p.moveTo(20, 11); p.cubicTo(12, 11, 4, 10, 4, 17)
    elif name == "compare":
        p.addRoundedRect(QRectF(3, 4, 18, 16), 2, 2)
        p.moveTo(12, 2); p.lineTo(12, 22)
    elif name in ("turn_left", "turn_right"):
        right = name == "turn_right"
        p.addRoundedRect(QRectF(9 if right else 3, 11, 12, 10), 1.5, 1.5)
        if right:
            p.moveTo(5, 13); p.cubicTo(5, 6, 10, 3, 16, 4)
            p.moveTo(13, 1.5); p.lineTo(16, 4); p.lineTo(13, 7)
        else:
            p.moveTo(19, 13); p.cubicTo(19, 6, 14, 3, 8, 4)
            p.moveTo(11, 1.5); p.lineTo(8, 4); p.lineTo(11, 7)
    elif name == "mirror":
        p.moveTo(12, 2); p.lineTo(12, 22)
        p.moveTo(9, 6); p.lineTo(3, 18); p.lineTo(9, 18); p.closeSubpath()
        p.moveTo(15, 6); p.lineTo(21, 18); p.lineTo(15, 18); p.closeSubpath()
    elif name == "fit":
        for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            cx, cy = 12 + sx * 8, 12 + sy * 7
            p.moveTo(cx - sx * 5, cy); p.lineTo(cx, cy); p.lineTo(cx, cy - sy * 5)
    elif name in ("zoom_in", "zoom_out"):
        p.addEllipse(QPointF(10, 10), 6.5, 6.5)
        p.moveTo(15, 15); p.lineTo(21, 21)
        p.moveTo(7, 10); p.lineTo(13, 10)
        if name == "zoom_in":
            p.moveTo(10, 7); p.lineTo(10, 13)
    elif name == "meme":
        p.addRoundedRect(QRectF(3, 4, 18, 16), 2, 2)
        p.moveTo(7, 8); p.lineTo(17, 8)
        p.moveTo(7, 16); p.lineTo(17, 16)
    elif name == "delete":
        p.moveTo(4, 7); p.lineTo(20, 7)
        p.moveTo(9, 7); p.lineTo(9, 4); p.lineTo(15, 4); p.lineTo(15, 7)
        p.moveTo(6, 7); p.lineTo(7, 20); p.lineTo(17, 20); p.lineTo(18, 7)
    elif name == "reset":
        p.moveTo(5, 12); p.cubicTo(5, 6, 10, 4, 13, 4); p.cubicTo(18, 4, 20, 9, 20, 12)
        p.cubicTo(20, 17, 16, 20, 12, 20); p.cubicTo(9, 20, 6, 18, 5, 15)
        p.moveTo(2, 9); p.lineTo(5, 12.5); p.lineTo(8.5, 9.5)
    elif name == "close":
        p.moveTo(6, 6)
        p.lineTo(18, 18)
        p.moveTo(18, 6)
        p.lineTo(6, 18)
    elif name == "edit":  # a pencil
        p.moveTo(4, 20); p.lineTo(5, 15); p.lineTo(16, 4); p.lineTo(20, 8); p.lineTo(9, 19); p.closeSubpath()
        p.moveTo(14, 6); p.lineTo(18, 10)
    elif name == "draw":  # a line drawn, and the pen
        p.moveTo(3, 20); p.cubicTo(6, 13, 9, 21, 12, 15)
        p.moveTo(13, 12); p.lineTo(19, 6); p.lineTo(21, 8); p.lineTo(15, 14); p.closeSubpath()
    elif name == "hide":  # an eye, struck through
        p.moveTo(2, 12); p.cubicTo(6, 5, 18, 5, 22, 12); p.cubicTo(18, 19, 6, 19, 2, 12)
        p.addEllipse(QPointF(12, 12), 3, 3)
        p.moveTo(4, 20); p.lineTo(20, 4)
    elif name == "select":  # the pointer
        p.moveTo(6, 3); p.lineTo(6, 19); p.lineTo(10, 15); p.lineTo(13, 21); p.lineTo(15.5, 20)
        p.lineTo(12.5, 14); p.lineTo(18, 14); p.closeSubpath()
    elif name == "arrow":
        p.moveTo(5, 19); p.lineTo(19, 5)
        p.moveTo(11, 5); p.lineTo(19, 5); p.lineTo(19, 13)
    elif name == "line":
        p.moveTo(5, 19); p.lineTo(19, 5)
    elif name == "box":
        p.addRect(QRectF(4, 6, 16, 12))
    elif name == "ellipse":
        p.addEllipse(QPointF(12, 12), 8.5, 6.5)
    elif name == "pen":
        p.moveTo(3, 15); p.cubicTo(6, 6, 9, 6, 11, 12); p.cubicTo(13, 18, 17, 18, 21, 9)
    elif name == "highlight":  # a marker over a thick stroke
        p.moveTo(9, 15); p.lineTo(16, 5); p.lineTo(20, 8); p.lineTo(13, 18); p.closeSubpath()
        p.moveTo(9, 15); p.lineTo(7, 18); p.lineTo(11, 19); p.lineTo(13, 18)
        p.moveTo(3, 21); p.lineTo(21, 21)
    elif name == "play":
        p.moveTo(7, 4.5); p.lineTo(19.5, 12); p.lineTo(7, 19.5); p.closeSubpath()
    elif name == "pause":
        p.addRoundedRect(QRectF(6, 5, 4, 14), 1, 1)
        p.addRoundedRect(QRectF(14, 5, 4, 14), 1, 1)
    elif name == "step_back":
        p.addRoundedRect(QRectF(5, 6, 2.5, 12), 1, 1)
        p.moveTo(19, 6); p.lineTo(10, 12); p.lineTo(19, 18); p.closeSubpath()
    elif name == "step_forward":
        p.addRoundedRect(QRectF(16.5, 6, 2.5, 12), 1, 1)
        p.moveTo(5, 6); p.lineTo(14, 12); p.lineTo(5, 18); p.closeSubpath()
    elif name == "trim":  # a strip of film, cut
        p.addRoundedRect(QRectF(3, 6, 18, 12), 2, 2)
        p.moveTo(8, 6); p.lineTo(8, 18)
        p.moveTo(16, 6); p.lineTo(16, 18)
        p.moveTo(8, 3); p.lineTo(8, 21)
        p.moveTo(16, 3); p.lineTo(16, 21)
    elif name == "frame":  # a picture: a frame with a hill
        p.addRoundedRect(QRectF(3, 5, 18, 14), 2, 2)
        p.moveTo(3, 16); p.lineTo(9, 11); p.lineTo(13, 14); p.lineTo(16, 12); p.lineTo(21, 16)
        p.addEllipse(QPointF(16, 9), 1.5, 1.5)
    elif name == "sticker":  # a star
        import math
        p.moveTo(12, 3)
        for i in range(1, 10):
            r = 9.5 if i % 2 == 0 else 4.2
            p.lineTo(12 + r * math.sin(math.pi * i / 5), 13 - r * math.cos(math.pi * i / 5))
        p.closeSubpath()
    return p


FILLED = ("play", "pause", "step_back", "step_forward")


def paint_icon(p: QPainter, name: str, r: QRectF, color: QColor, width: float = 1.8) -> None:
    """Icon name drawn into r (square) in color."""
    path, filled = _path(name), name in FILLED
    p.save()
    p.translate(r.x(), r.y())
    p.scale(r.width() / 24, r.height() / 24)
    pen = QPen(color, width * 24 / r.width())
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(color if filled else Qt.NoBrush)
    p.drawPath(path)
    p.restore()


class RailButton(QAbstractButton):
    """A tool in the left column: its icon, its name under it; lit when it's the tool in use."""

    def __init__(self, icon: str, label: str, key: str):
        super().__init__()
        self.icon, self.label = icon, label
        self.setCheckable(True)
        self.setFixedSize(RAIL - 12, 56)
        self.setToolTip(f"{label} ({key})")
        self.setAccessibleName(label)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover)

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        on, hover, off = self.isChecked(), self.underMouse() and self.isEnabled(), not self.isEnabled()
        if on or hover:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(INK["accent_soft"] if on else INK["raised"]))
            p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 8, 8)
        # a tool for another kind of file (Trim, shown a picture): faded, as a disabled control is
        colour = QColor(INK["line"] if off else INK["accent"] if on else INK["text"] if hover else INK["muted"])
        paint_icon(p, self.icon, QRectF((self.width() - 22) / 2, 8, 22, 22), colour)
        f = QFont(self.font())
        f.setPointSizeF(8)
        if on:
            f.setWeight(QFont.DemiBold)
        p.setFont(f)
        p.setPen(QColor(INK["line"] if off else INK["text"] if on or hover else INK["muted"]))
        p.drawText(QRectF(0, 34, self.width(), 18), Qt.AlignCenter, self.label)
        p.end()


class IconButton(QAbstractButton):
    """A square button with only an icon (undo, zoom...): its name in the tooltip."""

    def __init__(self, icon: str, tooltip: str, checkable: bool = False):
        super().__init__()
        self.icon = icon
        self.setCheckable(checkable)
        self.setFixedSize(CONTROL, CONTROL)
        self.setToolTip(tooltip)
        self.setAccessibleName(tooltip.split(" (")[0])
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover)

    def sizeHint(self) -> QSize:
        return QSize(CONTROL, CONTROL)

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        on, hover, enabled = self.isChecked() or self.isDown(), self.underMouse(), self.isEnabled()
        if enabled and (on or hover):
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(INK["accent_soft"] if on else INK["raised"]))
            p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 8, 8)
        colour = QColor(INK["accent"] if on else INK["text"] if enabled else INK["line"])
        paint_icon(p, self.icon, QRectF(6, 6, self.width() - 12, self.height() - 12), colour)
        p.end()


class Segmented(QWidget):
    """A row of choices that look like one control (Free | 1:1 | 4:5...); one is on at a time."""
    changed = Signal(object)

    def __init__(self, choices: list[tuple[str, object]], tooltip: str = ""):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.buttons: dict = {}
        for i, (label, value) in enumerate(choices):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setObjectName("segFirst" if i == 0 else "segLast" if i == len(choices) - 1 else "segMid")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, v=value: self.set(v, emit=True))
            lay.addWidget(b)
            self.buttons[value] = b
        if tooltip:
            self.setToolTip(tooltip)
        self.value = choices[0][1]
        self.set(self.value)

    def set(self, value, emit: bool = False) -> None:
        self.value = value
        for v, b in self.buttons.items():
            b.setChecked(v == value)
        if emit:
            self.changed.emit(value)
