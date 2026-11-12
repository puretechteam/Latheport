"""Windows without the system's title bar: the window's own header is where it's dragged from and
double-clicked to maximise, with minimise, maximise and close drawn in the app's style.

On Windows the frame stays the system's underneath, so nothing is lost: the window still snaps to
the screen's edges, shows Windows 11's snap layouts over the maximise button, resizes from every
edge and casts its shadow. That comes from answering two messages: WM_NCCALCSIZE (draw no title bar
or border: the window is all ours) and WM_NCHITTEST (which part is under the pointer: an edge to
resize from, the header to drag, the maximise button, or the window's own controls).
Elsewhere (and offscreen, in tests) the header moves the window through Qt.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from PySide6.QtCore import QObject, QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractSpinBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QSlider,
    QVBoxLayout,
    QWidget,
)

INTERACTIVE = (QAbstractButton, QComboBox, QSlider, QLineEdit, QAbstractSpinBox, QListWidget)

WM_NCCALCSIZE, WM_NCHITTEST, WM_STYLECHANGING = 0x0083, 0x0084, 0x007C
WS_CAPTION, WS_THICKFRAME, WS_MINIMIZEBOX, WS_MAXIMIZEBOX = 0x00C00000, 0x00040000, 0x00020000, 0x00010000
WM_NCMOUSEMOVE, WM_NCLBUTTONDOWN, WM_NCLBUTTONUP, WM_NCMOUSELEAVE = 0x00A0, 0x00A1, 0x00A2, 0x02A2
HTCLIENT, HTCAPTION, HTMAXBUTTON = 1, 2, 9
HT_EDGES = {(-1, 0): 10, (1, 0): 11, (0, -1): 12, (-1, -1): 13, (1, -1): 14, (0, 1): 15, (-1, 1): 16, (1, 1): 17}
EDGE = 6        # logical pixels along each side that resize the window
CLOSE_RED = "#c42b1c"


class CaptionButton(QAbstractButton):
    """Minimise, maximise (restore when maximised) or close, drawn like the app's other icon buttons."""

    def __init__(self, kind: str, colours: dict, tooltip: str):
        super().__init__()
        self.kind, self.colours = kind, colours
        self.hovered = False  # set by the window too: Windows owns the pointer over the maximise button
        self.setFixedSize(34, 30)
        self.setToolTip(tooltip)
        self.setAccessibleName(tooltip)
        self.setFocusPolicy(Qt.NoFocus)
        self.setAttribute(Qt.WA_Hover)

    def set_hover(self, on: bool) -> None:
        if on != self.hovered:
            self.hovered = on
            self.update()

    def enterEvent(self, e) -> None:
        self.set_hover(True)
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:
        self.set_hover(False)
        super().leaveEvent(e)

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        hot = self.hovered or self.isDown()
        if hot:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(CLOSE_RED if self.kind == "close" else self.colours.get("hover", self.colours.get("accent_soft"))))
            p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 7, 7)
        ink = QColor("#ffffff" if hot and self.kind == "close" else self.colours["text"])
        pen = QPen(ink, 1.2)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        cx, cy, s = self.width() / 2, self.height() / 2, 5.0
        if self.kind == "min":
            p.drawLine(QPointF(cx - s, cy), QPointF(cx + s, cy))
        elif self.kind == "max":
            if self.window().isMaximized():  # restore: a square with another behind it
                p.drawRect(QRectF(cx - s, cy - s + 2, 2 * s - 2, 2 * s - 2))
                p.drawPolyline([QPointF(cx - s + 2, cy - s + 2), QPointF(cx - s + 2, cy - s),
                                QPointF(cx + s, cy - s), QPointF(cx + s, cy + s - 2), QPointF(cx + s - 2, cy + s - 2)])
            else:
                p.drawRect(QRectF(cx - s, cy - s, 2 * s, 2 * s))
        else:
            p.drawLine(QPointF(cx - s, cy - s), QPointF(cx + s, cy + s))
            p.drawLine(QPointF(cx + s, cy - s), QPointF(cx - s, cy + s))
        p.end()


class CaptionButtons(QWidget):
    """Minimise, maximise and close (or the ones asked for: a dialog has only close), in a row."""
    closing = Signal()

    def __init__(self, colours: dict, kinds: tuple[str, ...] = ("min", "max", "close")):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        tips = {"min": "Minimise", "max": "Maximise", "close": "Close"}
        made = {k: CaptionButton(k, colours, tips[k]) for k in kinds}
        self.min, self.max, self.close = made.get("min"), made.get("max"), made.get("close")
        if self.min is not None:
            self.min.clicked.connect(lambda: self.window().showMinimized())
        if self.max is not None:
            self.max.clicked.connect(self.toggle)
        self.close.clicked.connect(lambda: self.window().close())
        for b in made.values():
            lay.addWidget(b)

    def toggle(self) -> None:
        win = self.window()
        win.showNormal() if win.isMaximized() else win.showMaximized()


class Frameless:
    """For a top-level QWidget: no system title bar. Call frameless(header, buttons) in __init__."""

    def frameless(self, header: QWidget, buttons: CaptionButtons, resizable: bool = True) -> None:
        self._header, self._caption, self._resizable = header, buttons, resizable
        self._native = False
        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self._mover = _Mover(self)  # held here: an event filter with no owner would be collected
        header.installEventFilter(self._mover)

    # -- Windows: the system's frame underneath, none of it drawn

    def showEvent(self, e) -> None:
        super().showEvent(e)
        if sys.platform == "win32" and not self._native and self.windowHandle() is not None \
                and self.windowHandle().screen() is not None and _real_platform():
            self._hwnd = int(self.winId())
            self._native = _keep_native_frame(self._hwnd, self._resizable, self._caption.max is not None)

    def changeEvent(self, e) -> None:
        super().changeEvent(e)
        if e.type() == e.Type.WindowStateChange and getattr(self, "_caption", None) is not None \
                and self._caption.max is not None:
            self._caption.max.setToolTip("Restore" if self.isMaximized() else "Maximise")
            self._caption.max.update()

    def nativeEvent(self, kind, message):
        if not getattr(self, "_native", False) or bytes(kind) != b"windows_generic_MSG":
            return False, 0
        msg = wintypes.MSG.from_address(int(message))
        if msg.hWnd and msg.hWnd != getattr(self, "_hwnd", msg.hWnd):
            # Qt made the window again (a 3D view starting does): the new one gets the frame's styles too
            self._hwnd = msg.hWnd
            _keep_native_frame(msg.hWnd, self._resizable, self._caption.max is not None)
        if msg.message == WM_STYLECHANGING and ctypes.c_int(msg.wParam & 0xFFFFFFFF).value == -16:
            # Qt sets the styles again now and then (a 3D view starting does): keep the frame's
            change = _STYLESTRUCT.from_address(msg.lParam)
            change.new = _frame_style(change.new, self._resizable, self._caption.max is not None)
            return False, 0
        if msg.message == WM_NCCALCSIZE and msg.wParam:
            if self.isMaximized():  # Windows lays a maximised window past the screen by its frame: take it back
                params = _NCCALCSIZE_PARAMS.from_address(msg.lParam)
                t = _frame_thickness(msg.hWnd)
                r = params.rgrc[0]
                r.left, r.top, r.right, r.bottom = r.left + t, r.top + t, r.right - t, r.bottom - t
            return True, 0
        if msg.message == WM_NCHITTEST:
            x = ctypes.c_short(msg.lParam & 0xFFFF).value
            y = ctypes.c_short((msg.lParam >> 16) & 0xFFFF).value
            rect = wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(msg.hWnd, ctypes.byref(rect))
            scale = self.devicePixelRatioF()
            return True, self.hit((x - rect.left) / scale, (y - rect.top) / scale)
        max_btn = self._caption.max
        if max_btn is None:
            return False, 0
        if msg.message == WM_NCMOUSEMOVE:
            max_btn.set_hover(msg.wParam == HTMAXBUTTON)
        elif msg.message == WM_NCMOUSELEAVE:
            max_btn.set_hover(False)
        elif msg.message == WM_NCLBUTTONDOWN and msg.wParam == HTMAXBUTTON:
            return True, 0
        elif msg.message == WM_NCLBUTTONUP and msg.wParam == HTMAXBUTTON:
            max_btn.set_hover(False)
            self._caption.toggle()
            return True, 0
        return False, 0

    def hit(self, x: float, y: float) -> int:
        """What's under the pointer at x, y (logical pixels from the window's top-left): an edge to
        resize from, the maximise button, the header to drag by, or the window itself."""
        if self._resizable and not self.isMaximized():
            sx = -1 if x < EDGE else 1 if x >= self.width() - EDGE else 0
            sy = -1 if y < EDGE else 1 if y >= self.height() - EDGE else 0
            if sx or sy:
                return HT_EDGES[(sx, sy)]
        point = QPoint(int(x), int(y))
        max_btn = self._caption.max
        if max_btn is not None and max_btn.rect().contains(max_btn.mapFrom(self, point)):
            return HTMAXBUTTON
        header_bottom = self._header.mapTo(self, QPoint(0, self._header.height())).y()
        if y < header_bottom:  # the header, and the window's margin above and beside it
            child = self.childAt(point)
            if child is None or not isinstance(child, INTERACTIVE):  # its blank parts and labels: drag by them
                return HTCAPTION
        return HTCLIENT


class Dialog(Frameless, QDialog):
    """A dialog with the app's own title bar (its title, and close) in place of the system's; what it
    holds goes in self.body. colours: the theme's (text, accent_soft) for the close button."""

    def __init__(self, parent, title: str, colours: dict, resizable: bool = False):
        super().__init__(parent)
        self.setWindowTitle(title)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.titlebar = QWidget()
        self.titlebar.setObjectName("dialogTitle")
        bar = QHBoxLayout(self.titlebar)
        bar.setContentsMargins(18, 10, 8, 0)
        self.title = QLabel(title)
        self.title.setObjectName("dialogName")
        self.caption = CaptionButtons(colours, ("close",))
        bar.addWidget(self.title, 1)
        bar.addWidget(self.caption, 0, Qt.AlignTop)
        outer.addWidget(self.titlebar)
        self.body = QWidget()
        self.body.setObjectName("dialogBody")
        outer.addWidget(self.body, 1)
        self.frameless(self.titlebar, self.caption, resizable)

    def setWindowTitle(self, title: str) -> None:
        super().setWindowTitle(title)
        if getattr(self, "title", None) is not None:
            self.title.setText(title)


class _Mover(QObject):
    """Off Windows (and offscreen): the header moves the window and a double-click maximises it."""

    def __init__(self, win):
        super().__init__()
        self.win = win

    def eventFilter(self, obj, e) -> bool:
        # Qt can still send events while Python takes the window apart at exit: nothing to move then
        if getattr(self, "win", None) is None or getattr(self.win, "_native", False):
            return False
        if e.type() == e.Type.MouseButtonPress and e.button() == Qt.LeftButton and self.win.windowHandle():
            self.win.windowHandle().startSystemMove()
            return True
        if e.type() == e.Type.MouseButtonDblClick and e.button() == Qt.LeftButton \
                and self.win._caption.max is not None:
            self.win._caption.toggle()
            return True
        return False


class _NCCALCSIZE_PARAMS(ctypes.Structure):
    _fields_ = [("rgrc", wintypes.RECT * 3), ("lppos", ctypes.c_void_p)]


class _STYLESTRUCT(ctypes.Structure):
    _fields_ = [("old", wintypes.DWORD), ("new", wintypes.DWORD)]


def _frame_style(style: int, resizable: bool, maximizable: bool) -> int:
    """style with the system frame's parts this window keeps: the caption (snapping, the shadow), the
    resizing frame if it resizes, minimise and maximise if it has them."""
    style |= WS_CAPTION
    style = style | WS_THICKFRAME if resizable else style & ~WS_THICKFRAME
    boxes = WS_MINIMIZEBOX | WS_MAXIMIZEBOX
    return (style | boxes if maximizable else style & ~boxes) & 0xFFFFFFFF


class _MARGINS(ctypes.Structure):
    _fields_ = [("left", ctypes.c_int), ("right", ctypes.c_int), ("top", ctypes.c_int), ("bottom", ctypes.c_int)]


def _real_platform() -> bool:
    from PySide6.QtGui import QGuiApplication
    return QGuiApplication.platformName() == "windows"


def _frame_thickness(hwnd: int) -> int:
    user32 = ctypes.windll.user32
    dpi = user32.GetDpiForWindow(hwnd) or 96
    return user32.GetSystemMetricsForDpi(32, dpi) + user32.GetSystemMetricsForDpi(92, dpi)  # SIZEFRAME + PADDEDBORDER


def _keep_native_frame(hwnd: int, resizable: bool = True, maximizable: bool = True) -> bool:
    """Give the window back the system frame's styles (snapping, resizing, the shadow), have the frame
    redrawn under our own WM_NCCALCSIZE, and the shadow kept by a 1-pixel glass edge under the header."""
    try:
        user32 = ctypes.windll.user32
        GWL_STYLE = -16
        get = user32.GetWindowLongPtrW
        get.restype = ctypes.c_ssize_t
        set_ = user32.SetWindowLongPtrW
        set_.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        set_(hwnd, GWL_STYLE, _frame_style(get(hwnd, GWL_STYLE), resizable, maximizable))
        ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(_MARGINS(0, 0, 1, 0)))
        SWP = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020
        user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, SWP)
        return True
    except (OSError, AttributeError):  # an old Windows without these: Qt's own frameless window, still usable
        return False
