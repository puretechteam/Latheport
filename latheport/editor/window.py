"""The editor window: a dark workspace with the tools in a column on the left, the tool's options in
one bar on top (Undo, Redo, Compare and zoom on its right), the stage in the middle, a panel on the
right for tools with more to set, and the files in a strip along the bottom with Cancel and Done.

Edits go to every file unless a file has its own: the switch on the top right (All files, This file,
As it is) says which, for the file shown, and the strip marks the files with their own.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRect, QRectF, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QIcon,
    QImage,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QShortcut,
    QTransform,
)
from PySide6.QtWidgets import (
    QApplication,
    QColorDialog,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSlider,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import engines, options, overlay
from ..chrome import CaptionButtons, Dialog, Frameless
from ..errors import ConversionError
from ..formats import category
from . import frames as framing
from . import jobs, playback
from .look import GAP, INK, RAIL, IconButton, RailButton, Segmented, stylesheet
from .model import Edits, History, render
from .stage import HIDE_STYLE, WATERMARK, Stage
from .timeline import Timeline, clock

TOOLS = [("crop", "crop", "Crop", "C"), ("adjust", "adjust", "Adjust", "A"), ("text", "text", "Text", "T"),
         ("draw", "draw", "Draw", "D"), ("hide", "hide", "Hide", "H"), ("trim", "trim", "Trim", "V")]
DRAW_KINDS = [("select", "Select: pick up a drawing to move it, or Delete it"), ("arrow", "Arrow"), ("line", "Line"),
              ("box", "Box"), ("ellipse", "Ellipse"), ("pen", "Pen"), ("highlight", "Highlighter"), ("sticker", "Sticker")]
SWATCHES = ["#ff3b30", "#ff9500", "#ffd60a", "#34c759", "#0a84ff", "#bf5af2", "#ffffff", "#1c1c1e"]
STICKER_NAMES = {"check": "Tick", "cross": "Cross", "star": "Star", "heart": "Heart", "number": "Numbers (1, 2, 3... in order)"}
ADJUST_NAMES = {"brightness": "Brightness", "contrast": "Contrast", "saturation": "Saturation", "warmth": "Warmth",
                "sharpness": "Sharpness"}
PICTURES = "Pictures (*.png *.jpg *.jpeg *.webp *.gif *.bmp *.tif *.tiff *.avif *.heic)"
SHAPES = [("Free", None), ("Original", "original"), ("1:1", (1, 1)), ("4:5", (4, 5)), ("16:9", (16, 9)),
          ("9:16", (9, 16))]
FONT_NAMES = {"sans": "Sans", "serif": "Serif", "bold": "Bold", "hand": "Handwriting"}
BACKGROUNDS = [("As it is", None), ("Taken out (see-through)", "remove"), ("White", "white"), ("Black", "black"),
               ("Blurred, like portrait mode", "blur")]
EDGE_NAMES = {"shadow": "Shadow", "outline": "Outline", "none": "No edge"}
HINTS = {
    "crop": "Drag the box or its corners: it snaps to the middle, the edges and the usual shapes. "
            "Scroll to zoom, Space-drag to look around.",
    "adjust": "Changes show as you make them. Hold Compare or \\ to see the picture as it was.",
    "text": "Click on the picture to add text and type it there. Click a text to change or drag it; "
            "Enter or Esc when done.",
    "draw": "Drag to draw; Shift keeps lines at 45 degrees and boxes square. Select picks a drawing up to move it; "
            "Delete removes it.",
    "trim": "Space plays and pauses, comma and period step a frame. Drag the ends of the timeline to trim, or press "
            "I and O where you are.",
    "hide": "Drag a box over what to hide. Drag one to move it, its corners to size it; Delete takes it off. "
            "Cover is the safe way to hide text such as passwords.",
}


def _sep() -> QFrame:
    line = QFrame()
    line.setFixedSize(1, 20)
    line.setStyleSheet(f"background: {INK['line']};")
    return line


def _section(text: str) -> QLabel:
    label = QLabel(text.upper())
    label.setObjectName("section")
    return label


def pick_colour(parent, now: QColor, title: str = "Text colour") -> QColor | None:
    """A colour, chosen in the editor's own kind of window (Qt's colour chooser inside it)."""
    box = Dialog(parent, title, {"text": INK["text"], "hover": INK["raised"]})
    lay = QVBoxLayout(box.body)
    lay.setContentsMargins(12, 4, 16, 16)
    lay.setSpacing(12)
    picker = QColorDialog(now)
    picker.setOptions(QColorDialog.NoButtons | QColorDialog.DontUseNativeDialog)
    picker.setWindowFlags(Qt.Widget)
    words = {"Basic colors": "Basic colours", "Custom colors": "Custom colours", "&Basic colors": "&Basic colours",
             "&Custom colors": "&Custom colours", "Pick Screen Color": "Pick a colour on the screen",
             "&Pick Screen Color": "&Pick a colour on the screen", "&Add to Custom Colors": "&Add to custom colours",
             "Add to Custom Colors": "Add to custom colours"}
    for w in picker.findChildren(QLabel) + picker.findChildren(QPushButton):  # Latheport says colour
        if w.text() in words:
            w.setText(words[w.text()])
    lay.addWidget(picker)
    row = QHBoxLayout()
    row.addStretch(1)
    cancel, use = QPushButton("Cancel"), QPushButton("Use this colour")
    use.setObjectName("primary")
    use.setDefault(True)
    cancel.clicked.connect(box.reject)
    use.clicked.connect(box.accept)
    row.addWidget(cancel)
    row.addWidget(use)
    lay.addLayout(row)
    return picker.currentColor() if box.exec() == Dialog.Accepted else None


class RenderJob(QThread):
    """The picture with its look (turned, recoloured, fried), made off the window's thread."""
    done = Signal(int, object, object)  # token, QImage, the Pillow picture

    def __init__(self, token: int, picture, look: dict):
        super().__init__()
        self.token, self.picture, self.look = token, picture, dict(look)

    def run(self) -> None:
        try:
            out = render(self.picture, self.look)
        except Exception:  # noqa: BLE001 - the preview must always hear back
            out = None
        self.done.emit(self.token, framing.qimage(out) if out is not None else None, out)


class SmartJob(QThread):
    """A smart tool's work off the window's thread: installing it, or reading a picture's text."""
    done = Signal(object, str)  # what it made (None if it failed), and why it failed

    def __init__(self, work):
        super().__init__()
        self.work = work

    def run(self) -> None:
        try:
            self.done.emit(self.work(), "")
        except Exception as exc:  # noqa: BLE001 - the window must always hear back
            self.done.emit(None, str(exc) or type(exc).__name__)


def read_text(picture) -> str:
    from ..smart import ocr
    return ocr.text(picture.convert("RGB"))


class ThumbsJob(QThread):
    """The film strip's pictures, one file after another."""
    got = Signal(int, object)  # index, QImage or None

    def __init__(self, files: list[tuple[Path, str]]):
        super().__init__()
        self.files = files

    def run(self) -> None:
        for i, (path, fmt) in enumerate(self.files):
            if self.isInterruptionRequested():
                return
            self.got.emit(i, framing.thumbnail(path, fmt))


class EditorWindow(Frameless, QWidget):
    """Edit the files. done(per_file, watermark, edits): each file's settings, the watermark (text,
    corner) and the edits (to open again where they were)."""
    done = Signal(object, object, object)

    def __init__(self, files: list[tuple[Path, str]], edits: Edits | None = None, watermark: dict | None = None):
        super().__init__()
        self.setObjectName("editor")
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setAttribute(Qt.WA_StyledBackground)
        self.setStyleSheet(stylesheet())
        self.setWindowTitle("Edit · Latheport")
        self.resize(1240, 820)
        self.setMinimumSize(960, 620)
        self.files = files
        paths = [p for p, _ in files]
        self.edits = edits if edits is not None and edits.files == paths else Edits(paths)
        self.history = History(self.edits)
        self.watermark = dict(WATERMARK, **(watermark or {}))
        self.colours = {"ink": "#ff3b30", "highlight": "#ffd60a"}  # the pen's colour, and the highlighter's
        self.render_timer = QTimer(self)  # a slider moving: the picture again once it rests a moment
        self.render_timer.setSingleShot(True)
        self.render_timer.setInterval(50)
        self.render_timer.timeout.connect(self._render)
        self.playback: playback.Playback | None = None
        self.video: dict | None = None  # the video shown: its path, length and size
        self.video_time = 0.0
        self.strips: dict[Path, list] = {}  # each video's timeline pictures, once made
        self.synced = False  # the player is at video_time (it starts at 0 until it's needed)
        self.still_token = 0
        self.still_timer = QTimer(self)  # paused, sought or changed: FFmpeg's own still once it rests
        self.still_timer.setSingleShot(True)
        self.still_timer.setInterval(120)
        self.still_timer.timeout.connect(self._still)
        self.frames: dict[Path, framing.Frame | None] = {}
        self.originals: dict = {}
        self.index = 0
        self.tool = "crop"
        self.token = 0
        self.jobs: list[QThread] = []
        self.thumbs: dict[int, QImage] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._bar())
        middle = QHBoxLayout()
        middle.setContentsMargins(0, 0, 0, 0)
        middle.setSpacing(0)
        middle.addWidget(self._rail())
        self.stage = Stage()
        centre = QVBoxLayout()
        centre.setContentsMargins(0, 0, 0, 0)
        centre.setSpacing(0)
        centre.addWidget(self.stage, 1)
        centre.addWidget(self._video_bar())
        middle.addLayout(centre, 1)
        middle.addWidget(self._inspector())
        root.addLayout(middle, 1)
        root.addWidget(self._footer())
        self.frameless(self.bar, self.caption)

        self.stage.changed.connect(self._stage_changed)
        self.stage.committed.connect(self._record)
        self.stage.picked.connect(self._picked)
        self.stage.zoomed.connect(lambda s: self.zoom_label.setText(f"{round(s * 100)}%" if self.stage.picture else ""))
        self.stage.chose.connect(self._chose)
        self.stage.retouched.connect(self._render)
        self.stage.space_tapped.connect(self.toggle_play)
        self.stage.zoomed.connect(lambda _: self._size_playback())
        self.stage.mark = dict(self.watermark)
        self._mark_note()
        for keys, fn in ((QKeySequence.Undo, self.undo), (QKeySequence.Redo, self.redo),
                         ("Ctrl+Shift+Z", self.redo), ("Ctrl+0", self.stage.fit),
                         ("Ctrl+=", lambda: self.stage.zoom_to(self.stage.zoom * 1.25)),
                         ("Ctrl++", lambda: self.stage.zoom_to(self.stage.zoom * 1.25)),
                         ("Ctrl+-", lambda: self.stage.zoom_to(self.stage.zoom / 1.25)),
                         ("Ctrl+[", lambda: self.turn(-1)), ("Ctrl+]", lambda: self.turn(1)),
                         ("Ctrl+Return", self.finish)):
            QShortcut(QKeySequence(keys), self, fn)
        self.choose_tool("crop")
        self.go(0)
        self.setMinimumWidth(max(960, self.bar.minimumSizeHint().width()))  # narrower, the bar's controls would overlap
        if len(files) > 1:
            job = ThumbsJob(files)
            job.got.connect(self._thumb)
            self._keep(job)
            job.start()

    # ---------------------------------------------------------------- building it

    def _bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("bar")
        bar.setFixedHeight(52)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(0, 10, 10, 10)
        mark = QLabel("Edit")
        mark.setObjectName("name")
        mark.setFixedWidth(RAIL)  # as wide as the tool column under it, the title in the middle of both
        mark.setAlignment(Qt.AlignCenter)
        mark.setToolTip("Latheport's editor")
        lay.addWidget(mark)
        lay.setSpacing(GAP)
        self.options_bar = QStackedWidget()
        self.options_bar.addWidget(self._crop_options())
        self.options_bar.addWidget(self._adjust_options())
        self.options_bar.addWidget(self._text_options())
        self.options_bar.addWidget(self._draw_options())
        self.options_bar.addWidget(self._hide_options())
        self.options_bar.addWidget(self._trim_options())
        lay.addWidget(self.options_bar, 1)
        self.scope = Segmented([("All files", "all"), ("This file", "this"), ("As it is", "none")],
                               "All files: edits go to every file without its own. This file: its own edits. "
                               "As it is: this file isn't edited")
        self.scope.changed.connect(self._scope)
        lay.addWidget(self.scope)
        self.scope_sep = _sep()
        lay.addWidget(self.scope_sep)
        self.undo_btn = IconButton("undo", "Undo (Ctrl+Z)")
        self.redo_btn = IconButton("redo", "Redo (Ctrl+Y)")
        self.compare_btn = IconButton("compare", "Hold to see it as it was (\\)", checkable=False)
        self.undo_btn.clicked.connect(self.undo)
        self.redo_btn.clicked.connect(self.redo)
        self.compare_btn.pressed.connect(lambda: self._compare(True))
        self.compare_btn.released.connect(lambda: self._compare(False))
        for w in (self.undo_btn, self.redo_btn, _sep(), self.compare_btn, _sep()):
            lay.addWidget(w)
        out_btn, in_btn, fit_btn = IconButton("zoom_out", "Zoom out (Ctrl+-)"), IconButton("zoom_in", "Zoom in (Ctrl++)"), \
            IconButton("fit", "Fit (Ctrl+0)")
        out_btn.clicked.connect(lambda: self.stage.zoom_to(self.stage.zoom / 1.25))
        in_btn.clicked.connect(lambda: self.stage.zoom_to(self.stage.zoom * 1.25))
        fit_btn.clicked.connect(lambda: self.stage.fit())
        self.zoom_label = QLabel("")
        self.zoom_label.setObjectName("value")
        self.zoom_label.setAlignment(Qt.AlignCenter)
        self.zoom_label.setFixedWidth(44)
        self.zoom_controls = (out_btn, self.zoom_label, in_btn, fit_btn)
        for w in self.zoom_controls:
            lay.addWidget(w)
        lay.addWidget(_sep())
        self.caption = CaptionButtons({"text": INK["text"], "hover": INK["raised"]})
        lay.addWidget(self.caption)
        self.bar = bar
        return bar

    def _row(self, *widgets) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(GAP)
        for x in widgets:
            lay.addWidget(x)
        lay.addStretch(1)
        return w

    def _crop_options(self) -> QWidget:
        left, right, mirror = IconButton("turn_left", "Turn left (Ctrl+[)"), IconButton("turn_right", "Turn right (Ctrl+])"), \
            IconButton("mirror", "Mirror")
        left.clicked.connect(lambda: self.turn(-1))
        right.clicked.connect(lambda: self.turn(1))
        mirror.clicked.connect(lambda: self.turn(0, mirror=True))
        self.shapes = Segmented(SHAPES, "The shape the box keeps")
        self.shapes.changed.connect(self._shape)
        reset = IconButton("reset", "Reset the crop")
        reset.clicked.connect(lambda: self.stage.reset_box())
        return self._row(left, right, mirror, _sep(), self.shapes, reset)

    def _adjust_options(self) -> QWidget:
        reset = QPushButton("Reset adjustments")
        reset.setToolTip("Back to how the picture looks in the file (the turn stays)")
        reset.clicked.connect(self._reset_look)
        return self._row(reset)

    def _text_options(self) -> QWidget:
        self.font_box = QComboBox()
        for k, v in FONT_NAMES.items():
            self.font_box.addItem(v, k)
        self.font_box.setToolTip("Font")
        self.size_bar = QSlider(Qt.Horizontal)
        self.size_bar.setRange(2, 30)
        self.size_bar.setMaximumWidth(110)  # gives way first when the bar is tight, so nothing overlaps
        self.size_bar.setMinimumWidth(48)
        self.size_bar.setToolTip("How tall the letters are")
        self.colour_btn = QPushButton("Colour")
        self.colour_btn.setToolTip("Text colour")
        self.edge_box = QComboBox()
        for k, v in EDGE_NAMES.items():
            self.edge_box.addItem(v, k)
        self.edge_box.setToolTip("An outline or a shadow around the letters, so they read on any picture")
        self.meme_btn = QPushButton("Meme")
        self.meme_btn.setToolTip("Big upper-case captions at the top and bottom: type to replace the top one")
        self.remove_btn = IconButton("delete", "Delete the text chosen (Delete)")
        self.font_box.activated.connect(lambda _: self._style(font=self.font_box.currentData()))
        # dragged: shown as it goes, one Undo step when let go; a key: a step each, the keys staying on the slider
        self.size_bar.valueChanged.connect(lambda v: self._style(size=v / 100, commit=not self.size_bar.isSliderDown(),
                                                                 focus=False))
        self.size_bar.sliderReleased.connect(lambda: self._style(size=self.size_bar.value() / 100,
                                                                 focus=self.stage.editing >= 0))
        self.colour_btn.clicked.connect(self._colour)
        self.edge_box.activated.connect(lambda _: self._style(edge=self.edge_box.currentData()))
        self.meme_btn.clicked.connect(self._meme)
        self.remove_btn.clicked.connect(lambda: self.stage.remove_text(self.stage.sel))
        return self._row(self.font_box, self.size_bar, self.colour_btn, self.edge_box, _sep(), self.meme_btn,
                         self.remove_btn)

    def _video_bar(self) -> QWidget:
        """Under the stage for a video: play, a frame back and on, the time, and the timeline."""
        bar = QWidget()
        bar.setObjectName("videobar")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(12, 6, 12, 8)
        lay.setSpacing(GAP)
        self.play_btn = IconButton("play", "Play (Space)")
        self.play_btn.clicked.connect(self.toggle_play)
        back, on = IconButton("step_back", "A frame back (,)"), IconButton("step_forward", "A frame on (.)")
        back.clicked.connect(lambda: self.step(-1))
        on.clicked.connect(lambda: self.step(1))
        self.time_label = QLabel("")
        self.time_label.setObjectName("value")
        self.time_label.setAlignment(Qt.AlignCenter)
        self.time_label.setFixedWidth(124)
        self.timeline = Timeline()
        self.timeline.seek.connect(self._scrub)
        self.timeline.trimmed.connect(lambda a, b: self._trim(start=a, end=b))
        for w in (self.play_btn, back, on, self.time_label):
            lay.addWidget(w)
        lay.addWidget(self.timeline, 1)
        bar.setVisible(False)
        self.video_bar = bar
        return bar

    def _trim_options(self) -> QWidget:
        start, end = QPushButton("Start here"), QPushButton("End here")
        start.setToolTip("The video starts where it is now (I)")
        end.setToolTip("The video ends where it is now (O)")
        start.clicked.connect(lambda: self.trim_here("start"))
        end.clicked.connect(lambda: self.trim_here("end"))
        whole = IconButton("reset", "Keep all of it")
        whole.clicked.connect(lambda: self._trim(start=None, end=None))
        self.frame_btn = QPushButton("Save this frame")
        self.frame_btn.setIcon(QIcon(self._icon_pixmap("frame")))
        self.frame_btn.setToolTip("This frame as a picture next to the video, with every edit on it")
        self.frame_btn.clicked.connect(self.save_frame)
        return self._row(start, end, whole, _sep(), self.frame_btn)

    @staticmethod
    def _icon_pixmap(name: str) -> QPixmap:
        from .look import paint_icon
        pix = QPixmap(36, 36)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        paint_icon(p, name, QRectF(4, 4, 28, 28), QColor(INK["text"]))
        p.end()
        return pix

    def _trim_panel(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(GAP)
        lay.addWidget(_section("Trim"))
        self.trim_note = QLabel("")
        self.trim_note.setWordWrap(True)
        lay.addWidget(self.trim_note)
        lay.addSpacing(GAP)
        lay.addWidget(_section("Speed"))
        st = options.SETTINGS["speed"]
        self.speed_box = QComboBox()
        self.speed_box.addItem("Normal", None)
        for value in st.choices:
            self.speed_box.addItem(st.shown(value), value)
        self.speed_box.setToolTip(st.help)
        self.speed_box.activated.connect(lambda _: self._pace(speed=self.speed_box.currentData()))
        lay.addWidget(self.speed_box)
        self.pitch = Segmented([("Pitch kept", None), ("Pitch changes", "change")], options.SETTINGS["pitch"].help)
        self.pitch.changed.connect(lambda v: self._pace(pitch=v))
        lay.addWidget(self.pitch)
        lay.addSpacing(GAP)
        lay.addWidget(_section("Backwards"))
        self.backwards = Segmented([("No", None), ("Yes", "yes")], options.SETTINGS["reverse"].help)
        self.backwards.changed.connect(lambda v: self._pace(reverse=v))
        lay.addWidget(self.backwards)
        lay.addSpacing(GAP)
        lay.addWidget(_section("Frame"))
        self.frame_note = QLabel("Save this frame puts a picture of it next to the video, with every edit on it.")
        self.frame_note.setObjectName("muted")
        self.frame_note.setWordWrap(True)
        lay.addWidget(self.frame_note)
        lay.addSpacing(GAP)
        note = QLabel("Trims and speed are this video's own. While it plays you see it turned, cropped, drawn on and in "
                      "black and white; pause and every effect shows exactly as it will be.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        lay.addWidget(note)
        lay.addStretch(1)
        return panel

    def _draw_options(self) -> QWidget:
        self.draw_buttons: dict[str, IconButton] = {}
        for key, tip in DRAW_KINDS:
            b = IconButton(key, tip, checkable=True)
            b.clicked.connect(lambda _=False, k=key: self.draw_kind(k))
            self.draw_buttons[key] = b
        self.mark_remove = IconButton("delete", "Delete the drawing chosen (Delete)")
        self.mark_remove.clicked.connect(lambda: self.stage.remove_chosen())
        return self._row(*self.draw_buttons.values(), _sep(), self.mark_remove)

    def _hide_options(self) -> QWidget:
        self.hide_how = Segmented([("Pixelate", "pixelate"), ("Blur", "blur"), ("Cover", "cover")],
                                  "How it's hidden: in blocks, blurred, or covered in black (the safe way for text)")
        self.hide_how.changed.connect(self._hide_how)
        label = QLabel("Strength")
        label.setObjectName("muted")
        self.hide_strength = QSlider(Qt.Horizontal)
        self.hide_strength.setRange(1, 100)
        self.hide_strength.setValue(HIDE_STYLE["strength"])
        self.hide_strength.setFixedWidth(110)
        self.hide_strength.setToolTip("Stronger: bigger blocks, more blur")
        self.hide_strength_value = QLabel(str(self.hide_strength.value()))
        self.hide_strength_value.setObjectName("value")
        self.hide_strength.valueChanged.connect(lambda v: self.hide_strength_value.setText(str(v)))
        self.hide_strength.sliderReleased.connect(self._hide_strength)
        self.hide_strength.valueChanged.connect(lambda _: None if self.hide_strength.isSliderDown() else self._hide_strength())
        self.hide_remove = IconButton("delete", "Take off the part chosen (Delete)")
        self.hide_remove.clicked.connect(lambda: self.stage.remove_chosen())
        return self._row(self.hide_how, _sep(), label, self.hide_strength, self.hide_strength_value, _sep(), self.hide_remove)

    def _rail(self) -> QWidget:
        rail = QWidget()
        rail.setObjectName("rail")
        rail.setFixedWidth(RAIL)
        lay = QVBoxLayout(rail)
        lay.setContentsMargins(6, 10, 6, 10)
        lay.setSpacing(4)
        self.rail_buttons: dict[str, RailButton] = {}
        for key, icon, label, shortcut in TOOLS:
            b = RailButton(icon, label, shortcut)
            if key == "trim":
                b.setToolTip(f"{label} ({shortcut}): for videos")
            b.clicked.connect(lambda _=False, k=key: self.choose_tool(k))
            lay.addWidget(b, 0, Qt.AlignHCenter)
            self.rail_buttons[key] = b
        lay.addStretch(1)
        return rail

    def _inspector(self) -> QWidget:
        self.inspector = QStackedWidget()
        self.inspector.setObjectName("inspector")
        self.inspector.setFixedWidth(280)
        adjust = QWidget()
        lay = QVBoxLayout(adjust)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(GAP)
        lay.addWidget(_section("Light and colour"))
        self.adjust_sliders: dict[str, tuple[QSlider, QLabel]] = {}
        for name, label in ADJUST_NAMES.items():
            slider, value = self._slider_row(lay, label, -100, 100)
            slider.valueChanged.connect(lambda v, n=name: self._adjust_moved(n, v))
            slider.sliderReleased.connect(self._record)
            self.adjust_sliders[name] = (slider, value)
        lay.addSpacing(GAP)
        lay.addWidget(_section("Straighten"))
        self.straighten, self.straighten_value = self._slider_row(lay, "Angle", -150, 150)
        self.straighten.setToolTip("Turn it level a little at a time (a grid shows while you drag); the edges are cut in so no corner is empty")
        self.straighten.valueChanged.connect(self._straighten_moved)
        self.straighten.sliderPressed.connect(lambda: self._straightening(True))
        self.straighten.sliderReleased.connect(lambda: self._straightening(False))
        lay.addSpacing(GAP)
        lay.addWidget(_section("Touch up"))
        self.touch = Segmented([("Off", None), ("Auto fix", "auto"), ("Clean page", "page")],
                               "Auto fix: colour and contrast. Clean page: a photo of a page made into a clean scan")
        self.touch.changed.connect(lambda v: self._look(enhance=v))
        lay.addWidget(self.touch)
        lay.addSpacing(GAP)
        lay.addWidget(_section("Colour"))
        self.colour = Segmented([("Original", None), ("B&&W", "bw"), ("Sepia", "sepia")], "Black and white, or sepia")
        self.colour.changed.connect(lambda v: self._look(colour=v))
        lay.addWidget(self.colour)
        self.smart_box = QWidget()  # pictures only: videos have no smart tools in the editor
        smart = QVBoxLayout(self.smart_box)
        smart.setContentsMargins(0, GAP, 0, 0)
        smart.setSpacing(GAP)
        smart.addWidget(_section("Background"))
        self.background = QComboBox()
        for name, value in BACKGROUNDS:
            self.background.addItem(name, value)
        self.background.setToolTip("A smart tool finds the subject: the background is taken out (see-through), made "
                                   "white or black, or blurred like portrait mode")
        self.background.currentIndexChanged.connect(lambda _: self._background(self.background.currentData()))
        smart.addWidget(self.background)
        self.copy_text = QPushButton("Copy the text in it")
        self.copy_text.setToolTip("A smart tool reads the text in the picture (OCR) and copies it, to paste anywhere")
        self.copy_text.clicked.connect(self._copy_text)
        smart.addWidget(self.copy_text)
        self.smart_note = QLabel("")
        self.smart_note.setObjectName("muted")
        self.smart_note.setWordWrap(True)
        self.smart_note.hide()
        smart.addWidget(self.smart_note)
        lay.addWidget(self.smart_box)
        lay.addSpacing(GAP)
        lay.addWidget(_section("Deep fry"))
        fry_row = QHBoxLayout()
        fry_row.setSpacing(GAP)
        self.fry = QSlider(Qt.Horizontal)
        self.fry.setRange(0, 100)
        self.fry.setToolTip("The meme look: higher is crispier")
        self.fry_value = QLabel("Off")
        self.fry_value.setObjectName("value")
        self.fry_value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.fry.valueChanged.connect(lambda v: self.fry_value.setText(f"{v}%" if v else "Off"))
        self.fry.sliderReleased.connect(lambda: self._look(deepfry=self.fry.value() or None))
        self.fry.valueChanged.connect(lambda v: None if self.fry.isSliderDown() else self._look(deepfry=v or None))
        fry_row.addWidget(self.fry, 1)
        fry_row.addWidget(self.fry_value)
        lay.addLayout(fry_row)
        lay.addStretch(1)
        self.inspector.addWidget(adjust)

        text = QWidget()
        lay = QVBoxLayout(text)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(GAP)
        lay.addWidget(_section("Watermark"))
        self.mark_field = QLineEdit(self.watermark["text"])
        self.mark_field.setPlaceholderText("None: type your own")
        self.mark_field.setMaxLength(options.TEXT_MAX)
        self.mark_field.setToolTip("Your own text small in a corner of every file, like your name or site. "
                                   "Kept for next time")
        self.mark_corner = QComboBox()
        st = options.SETTINGS["watermark_at"]
        for value in st.choices:
            self.mark_corner.addItem(st.shown(value), value)
        self.mark_corner.setCurrentIndex(max(0, self.mark_corner.findData(self.watermark["at"])))
        self.mark_pick = QPushButton("Add a picture…")
        self.mark_pick.setToolTip("Your logo or signature, best as a PNG with a see-through background")
        self.mark_pick.clicked.connect(self._mark_picture)
        self.mark_drop = IconButton("close", "Take the picture off")
        self.mark_drop.clicked.connect(lambda: self._mark(image=""))
        pick_row = QHBoxLayout()
        pick_row.setSpacing(GAP)
        pick_row.addWidget(self.mark_pick, 1)
        pick_row.addWidget(self.mark_drop)
        self.mark_opacity, self.mark_opacity_value = self._slider_row(None, "Opacity", 5, 100)
        self.mark_opacity.setValue(int(self.watermark["opacity"]))
        self.mark_opacity_value.setText(f"{self.mark_opacity.value()}%")
        self.mark_opacity.setToolTip("How solid the watermark is: 100% is fully solid")
        self.mark_opacity.valueChanged.connect(lambda v: (self.mark_opacity_value.setText(f"{v}%"), self._mark()))
        self.mark_note = QLabel("")
        self.mark_note.setObjectName("muted")
        self.mark_note.setWordWrap(True)
        self.mark_field.textEdited.connect(lambda _: self._mark())
        self.mark_corner.activated.connect(lambda _: self._mark())
        for w in (self.mark_field, self.mark_corner):
            lay.addWidget(w)
        lay.addLayout(pick_row)
        lay.addLayout(self.mark_opacity.parent_row)
        lay.addWidget(self.mark_note)
        lay.addStretch(1)
        self.inspector.addWidget(text)
        self.inspector.addWidget(self._draw_panel())
        self.inspector.addWidget(self._trim_panel())
        return self.inspector

    def _slider_row(self, lay, label: str, low: int, high: int) -> tuple[QSlider, QLabel]:
        """A row: the name, a slider from low to high, its value; added to lay (or kept as slider.parent_row)."""
        row = QHBoxLayout()
        row.setSpacing(GAP)
        name = QLabel(label)
        name.setFixedWidth(72)
        slider = QSlider(Qt.Horizontal)
        slider.setRange(low, high)
        slider.setAccessibleName(label)
        value = QLabel("0")
        value.setObjectName("value")
        value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(name)
        row.addWidget(slider, 1)
        row.addWidget(value)
        if lay is not None:
            lay.addLayout(row)
        slider.parent_row = row
        return slider, value

    def _draw_panel(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(GAP)
        lay.addWidget(_section("Colour"))
        grid = QGridLayout()
        grid.setSpacing(6)
        self.swatches: dict[str, QPushButton] = {}
        for i, colour in enumerate(SWATCHES):
            b = QPushButton()
            b.setCheckable(True)
            b.setFixedSize(28, 28)
            b.setToolTip(colour)
            b.setStyleSheet(f"QPushButton {{ background: {colour}; border: 2px solid {INK['line']}; border-radius: 14px; padding: 0; "
                            f"min-height: 0; max-height: 28px; }} QPushButton:checked {{ border: 2px solid {INK['text']}; }}")
            b.clicked.connect(lambda _=False, c=colour: self._draw_style(colour=c))
            grid.addWidget(b, 0, i)
            self.swatches[colour] = b
        lay.addLayout(grid)
        other = QPushButton("Another colour…")
        other.clicked.connect(self._other_colour)
        lay.addWidget(other)
        lay.addSpacing(GAP)
        lay.addWidget(_section("Line"))
        self.draw_width, self.draw_width_value = self._slider_row(lay, "Width", 1, 40)
        self.draw_width.setToolTip("How thick lines are (the highlighter is four times as thick)")
        self.draw_width.valueChanged.connect(lambda v: self.draw_width_value.setText(str(v)))
        self.draw_width.sliderReleased.connect(lambda: self._draw_style(width=self.draw_width.value() / 1000))
        self.draw_width.valueChanged.connect(
            lambda v: None if self.draw_width.isSliderDown() else self._draw_style(width=v / 1000))
        self.draw_fill = Segmented([("Outline", False), ("Filled", True)], "Boxes and ellipses: an outline, or filled in")
        self.draw_fill.changed.connect(lambda v: self._draw_style(fill=v))
        lay.addWidget(self.draw_fill)
        lay.addSpacing(GAP)
        lay.addWidget(_section("Stickers"))
        stickers = QHBoxLayout()
        stickers.setSpacing(6)
        self.sticker_buttons: dict[str, QPushButton] = {}
        for name, tip in STICKER_NAMES.items():
            b = QPushButton()
            b.setCheckable(True)
            b.setFixedSize(36, 36)
            b.setIconSize(QSize(26, 26))
            b.setToolTip(tip)
            b.setStyleSheet("QPushButton { padding: 0; min-height: 0; max-height: 36px; }")
            b.clicked.connect(lambda _=False, n=name: self._sticker(n))
            stickers.addWidget(b)
            self.sticker_buttons[name] = b
        stickers.addStretch(1)
        lay.addLayout(stickers)
        self.sticker_size, self.sticker_size_value = self._slider_row(lay, "Size", 3, 30)
        self.sticker_size.valueChanged.connect(lambda v: self.sticker_size_value.setText(str(v)))
        self.sticker_size.sliderReleased.connect(lambda: self._draw_style(size=self.sticker_size.value() / 100))
        self.sticker_size.valueChanged.connect(
            lambda v: None if self.sticker_size.isSliderDown() else self._draw_style(size=v / 100))
        note = QLabel("Shift keeps lines at 45 degrees and boxes square. Colour and width go to the drawing "
                      "chosen too; with Select, a sticker picked swaps the one chosen.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        lay.addSpacing(GAP)
        lay.addWidget(note)
        lay.addStretch(1)
        self._paint_stickers()
        return panel

    def _paint_stickers(self) -> None:
        """Each sticker button shows the sticker, in the colour chosen."""
        colour = self.stage.mark_style["colour"]
        for name, b in self.sticker_buttons.items():
            mark = {"kind": "sticker", "name": name, "points": [[0.5, 0.5]], "colour": colour, "width": 0.006,
                    "size": 0.9, "n": 1}
            layer = overlay.marks_layer((52, 52), [mark])
            b.setIcon(QIcon(QPixmap.fromImage(QImage(layer.tobytes(), 52, 52, 208, QImage.Format_RGBA8888).copy())))

    def _footer(self) -> QWidget:
        footer = QWidget()
        footer.setObjectName("footer")
        lay = QHBoxLayout(footer)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(12)
        self.strip = QListWidget()
        self.strip.setObjectName("strip")
        self.strip.setFlow(QListWidget.LeftToRight)
        self.strip.setViewMode(QListWidget.IconMode)
        self.strip.setIconSize(QSize(72, 54))
        self.strip.setGridSize(QSize(82, 62))
        self.strip.setFixedHeight(70)
        self.strip.setMovement(QListWidget.Static)
        self.strip.setWrapping(False)
        self.strip.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.strip.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        for path, _ in self.files:
            item = QListWidgetItem()
            item.setToolTip(path.name)
            item.setSizeHint(QSize(78, 58))
            self.strip.addItem(item)
        self.strip.currentRowChanged.connect(lambda i: i >= 0 and i != self.index and self.go(i))
        self.strip.setVisible(len(self.files) > 1)
        lay.addWidget(self.strip, 1)
        names = QVBoxLayout()
        names.setSpacing(2)
        self.name = QLabel()
        self.name.setObjectName("name")
        self.detail = QLabel()
        self.detail.setObjectName("muted")
        names.addStretch(1)
        names.addWidget(self.name)
        names.addWidget(self.detail)
        names.addStretch(1)
        lay.addLayout(names, 0 if len(self.files) > 1 else 1)
        self.cancel_btn, self.done_btn = QPushButton("Cancel"), QPushButton("Done")
        self.done_btn.setObjectName("primary")
        self.done_btn.setToolTip("Keep these edits for the conversion (Ctrl+Enter)")
        self.cancel_btn.clicked.connect(self.close)
        self.done_btn.clicked.connect(self.finish)
        lay.addWidget(self.cancel_btn)
        lay.addWidget(self.done_btn)
        footer.setFixedHeight(86 if len(self.files) > 1 else 56)
        return footer

    # ---------------------------------------------------------------- files

    def current(self) -> Path:
        return self.files[self.index][0]

    def edit(self):
        return self.edits.of(self.current())

    def go(self, i: int) -> None:
        if not 0 <= i < len(self.files):
            return
        self.stage.commit()
        self.index = i
        path, fmt = self.files[i]
        if path not in self.frames:
            self.setCursor(Qt.WaitCursor)
            self.frames[path] = framing.load_frame(path, fmt)
            self.unsetCursor()
            frame = self.frames[path]
            self.originals[path] = framing.qimage(frame.picture) if frame else None
        frame = self.frames[path]
        edit = self.edit()
        real = edit.turned((frame.width, frame.height)) if frame else (0, 0)
        picture = self.originals[path]
        quick = {k: v for k, v in edit.picture_settings().items() if k != "background"}  # a smart tool takes a while
        if frame is not None and quick:  # straight away: no picture of the wrong shape first
            picture = framing.qimage(render(frame.picture, quick))
        self.token += 1
        self.stage.open_file(picture, self.originals[path], real, edit)
        self._open_video(path, fmt, frame)
        self.strip.blockSignals(True)
        self.strip.setCurrentRow(i)
        self.strip.blockSignals(False)
        self.name.setText(path.name)
        shown = frame is not None  # one that can't be shown: nothing here can act on it
        for w in (self.options_bar, self.inspector, self.compare_btn, *self.zoom_controls):
            w.setEnabled(shown)
        self.smart_box.setVisible(self.video is None)
        self._sync()
        if frame is not None and "background" in edit.picture_settings():
            self._render()  # the background found off the window's thread

    def _render(self) -> None:
        """The stage's picture again, with the look as it is now (off the window's thread)."""
        frame = self.frames.get(self.current())
        if frame is None:
            return
        if self.video is not None:  # a video: FFmpeg's own still of where it is
            self.still_timer.start()
            return
        self.token += 1
        edit = self.edit()
        if not edit.picture_settings():
            self.stage.set_picture(self.originals[self.current()], (frame.width, frame.height))
            return
        job = RenderJob(self.token, frame.picture, edit.picture_settings())
        job.done.connect(self._rendered)
        self._keep(job)
        job.start()

    def _rendered(self, token: int, image, picture) -> None:
        frame = self.frames.get(self.current())
        if token != self.token or image is None or frame is None:
            return
        self.stage.set_picture(image, self.edit().turned((frame.width, frame.height)))
        if self.stage.edit.lock:
            self.stage._fit_lock()
        if self.smart_note.text().startswith("Finding"):
            self._smart_say("")

    def _keep(self, job: QThread) -> None:
        jobs.keep(self.jobs, job)

    def _thumb(self, i: int, image) -> None:
        self.thumbs[i] = image  # None: it can't be shown
        self._mark_strip(i)

    def _mark_strip(self, i: int) -> None:
        """The strip's picture for file i, with a dot when it has its own edits and a slash when left as it is."""
        image = self.thumbs.get(i)
        pix = QPixmap(72, 54)
        pix.fill(QColor(INK["stage"]))
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        if image is not None:
            scaled = image.scaled(72, 54, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            p.drawImage((72 - scaled.width()) // 2, (54 - scaled.height()) // 2, scaled)
        elif i in self.thumbs:  # tried: it can't be shown
            p.setPen(QColor(INK["muted"]))
            f = p.font()
            f.setPointSizeF(7.5)
            p.setFont(f)
            p.drawText(pix.rect(), Qt.AlignCenter, "Can't show")
        scope = self.edits.scope(self.files[i][0])
        if scope == "this":
            p.setPen(QPen(QColor(INK["stage"]), 2))
            p.setBrush(QColor(INK["accent"]))
            p.drawEllipse(58, 4, 10, 10)
        elif scope == "none":
            p.fillRect(pix.rect(), QColor(0, 0, 0, 120))
            p.setPen(QPen(QColor(INK["text"]), 2))
            p.drawLine(26, 40, 46, 14)
        p.end()
        self.strip.item(i).setIcon(QIcon(pix))

    # ---------------------------------------------------------------- tools and controls

    def choose_tool(self, key: str) -> None:
        if key == "trim" and self.video is None:
            return
        self.stage.commit()
        self.tool = key
        self.stage.mode = key
        self.stage.sel = -1
        self.stage.hint = HINTS[key]
        for k, b in self.rail_buttons.items():
            b.setChecked(k == key)
        self.options_bar.setCurrentIndex([t[0] for t in TOOLS].index(key))
        self.inspector.setVisible(key in ("adjust", "text", "draw", "trim"))
        self.inspector.setCurrentIndex({"adjust": 0, "text": 1, "draw": 2, "trim": 3}.get(key, 0))
        self.stage.chosen = -1
        self._picked(-1)
        self._chose(-1)
        if key == "draw":
            self.draw_kind(self.stage.draw_kind)
        self.stage.update()

    # -- a video: playing, the timeline, trims and pace, saving a frame

    def _open_video(self, path: Path, fmt: str, frame) -> None:
        """The player and the timeline for the file shown, if it's a video that can play."""
        if self.video is not None and self.video["path"] == path:  # the same one again (undo, a scope change)
            self._sync_trim()
            self.still_timer.start()
            return
        self._close_video()
        info = None
        if frame is not None and category(fmt) == "Video" and playback.available():
            from ..converters import media
            try:
                info = media.probe(path)
            except Exception:  # noqa: BLE001 - no length known: Trim stays off
                info = None
        self.rail_buttons["trim"].setEnabled(info is not None)
        self.stage.video = info is not None
        self.video_bar.setVisible(info is not None)
        if info is None:
            if self.tool == "trim":
                self.choose_tool("crop")
            return
        duration = float(info.get("duration") or 0)
        self.video = {"path": path, "duration": duration, "size": (frame.width, frame.height)}
        self.video_time = min(1.0, duration / 2)  # where the stage's first picture is from
        self.timeline.set_video(duration)
        self.timeline.set_position(self.video_time)
        if path in self.strips:
            self.timeline.set_pictures(self.strips[path])
        else:
            job = playback.StripJob(path, duration, (frame.width, frame.height))
            job.got.connect(self._strip_got)
            self._keep(job)
            job.start()
        try:
            self.playback = playback.Playback(path)
        except Exception:  # noqa: BLE001 - no playback: the still frame stays
            self.playback = None
        if self.playback is not None:
            self.playback.frame.connect(self._live_frame)
            self.playback.state.connect(self._play_state)
            self._size_playback()
        self.play_btn.setEnabled(self.playback is not None)
        self._sync_trim()
        self._show_time()

    def _close_video(self) -> None:
        if self.playback is not None:
            self.playback.close()
        self.playback, self.video, self.synced = None, None, False
        self.still_token += 1
        self._play_icon(False)

    def _strip_got(self, path: Path, pictures: list) -> None:
        self.strips[path] = pictures
        if self.video is not None and self.video["path"] == path:
            self.timeline.set_pictures(pictures)

    def _size_playback(self) -> None:
        """Frames as big as the stage shows them (no bigger than the video's own)."""
        if self.playback is None or self.video is None:
            return
        r = self.stage.image_rect()
        scale = self.devicePixelRatioF()
        w, h = r.width() * scale, r.height() * scale
        if options.sideways(self.edit().look.get("rotate")):
            w, h = h, w
        vw, vh = self.video["size"]
        self.playback.set_size(int(min(w, vw)), int(min(h, vh)))

    def _sync_player(self) -> None:
        """The player starts at 0: take it to where the stage is before it's first used."""
        if not self.synced and self.playback is not None:
            self.playback.seek(self.video_time)
            self.synced = True

    def toggle_play(self) -> None:
        pb = self.playback
        if pb is None:
            return
        if pb.playing:
            pb.pause()
            self._play_icon(False)
            self.still_timer.start()
            return
        start, end = self.timeline.kept()
        if pb.ended or self.video_time >= end - 0.05 or self.video_time < start:  # from the start of the part kept
            self.video_time = start
            pb.seek(start)
            self.synced = True
        self._sync_player()
        pb.set_speed(float(self.edits.times.get(self.current(), {}).get("speed") or 1))
        pb.play(until=self.timeline.end)
        self._play_icon(True)

    def step(self, frames: int) -> None:
        if self.playback is None:
            return
        self._sync_player()
        self.playback.step(frames)
        self._play_icon(False)

    def _scrub(self, seconds: float) -> None:
        self.video_time = seconds
        self._show_time()
        if self.playback is not None:
            if self.playback.playing:
                self.playback.pause()
                self._play_icon(False)
            self.playback.seek(seconds)
            self.synced = True
        self.still_timer.start()

    def _play_icon(self, playing: bool) -> None:
        if hasattr(self, "play_btn"):
            self.play_btn.icon = "pause" if playing else "play"
            self.play_btn.setToolTip("Pause (Space)" if playing else "Play (Space)")
            self.play_btn.update()

    def _play_state(self, state: str) -> None:
        self._play_icon(state == "playing")
        if state == "ended" and self.video is not None:
            self.video_time = self.video["duration"]
            self.still_timer.start()

    def _live_frame(self, image, seconds: float) -> None:
        """A frame from the player: on the stage at once, with what's quick to show; FFmpeg's exact still
        follows once it's paused."""
        if self.video is None:
            return
        self.video_time = seconds
        self.timeline.set_position(seconds)
        self._show_time()
        self.stage.set_live(self._live_look(image))
        pb = self.playback
        if pb is not None and pb.playing:
            end = self.timeline.end
            if end is not None and seconds >= end - 1e-3:  # the end of the part kept: stop there
                pb.pause()
                self._play_icon(False)
                self.still_timer.start()
        else:
            self.still_timer.start()

    def _live_look(self, image):
        """A playing frame as quick to change as it has to be: turned or mirrored, the hidden parts roughly
        hidden, black and white (the exact look shows once it's paused)."""
        look = self.edit().look
        mirrored, quarters = options.orientation(look.get("rotate"))
        if mirrored or quarters:
            how = QTransform()
            if mirrored:
                how.scale(-1, 1)
            image = image.transformed(how.rotate(90 * quarters) if quarters else how)
        if self.edit().hides:
            image = image.convertToFormat(QImage.Format_RGB32)
            p = QPainter(image)
            w, h = image.width(), image.height()
            for hide in self.edit().hides:
                box = QRect(int(hide["x"] * w), int(hide["y"] * h), max(1, int(hide["w"] * w)), max(1, int(hide["h"] * h)))
                if hide["how"] == "cover":
                    p.fillRect(box, QColor(0, 0, 0))
                    continue
                part = image.copy(box)
                fine = 4 if hide["how"] == "pixelate" else 8
                small = part.scaled(max(1, box.width() // (fine * 3)), max(1, box.height() // (fine * 3)),
                                    Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
                smooth = Qt.FastTransformation if hide["how"] == "pixelate" else Qt.SmoothTransformation
                p.drawImage(box, small.scaled(box.size(), Qt.IgnoreAspectRatio, smooth))
            p.end()
        if look.get("colour") == "bw":
            image = image.convertToFormat(QImage.Format_Grayscale8)
        return image

    def _still(self) -> None:
        """FFmpeg's own still of where the video is, with the look exactly as the conversion makes it."""
        if self.video is None or (self.playback is not None and self.playback.playing):
            return
        self.still_token += 1
        job = playback.StillJob(self.still_token, self.video["path"], self.video_time, self.edit().picture_settings(),
                                self.video["size"])
        job.done.connect(self._still_done)
        self._keep(job)
        job.start()

    def _still_done(self, token: int, seconds: float, picture) -> None:
        if token != self.still_token or picture is None or self.video is None:
            return
        self.stage.set_picture(framing.qimage(picture), self.edit().turned(self.video["size"]))
        if self.stage.edit.lock:
            self.stage._fit_lock()

    def _show_time(self) -> None:
        if self.video is not None:
            self.time_label.setText(f"{clock(self.video_time)} / {clock(self.video['duration'])}")

    def trim_here(self, which: str) -> None:
        """Start or end the video where it is now (I and O)."""
        if self.video is None:
            return
        start, end = self.timeline.kept()
        t = self.video_time
        if which == "start":
            self._trim(start=t if t > 0 else None, end=self.timeline.end if t < end - 0.1 else None)
        else:
            self._trim(start=self.timeline.start if t > start + 0.1 else None,
                       end=t if t < self.video["duration"] else None)

    def _trim(self, **change) -> None:
        if self.video is None:
            return
        self.edits.set_time(self.video["path"], **change)
        self._record()
        self._sync_trim()

    def _pace(self, **change) -> None:
        if self.video is None:
            return
        self.edits.set_time(self.video["path"], **change)
        self._record()
        self._sync_trim()

    def _sync_trim(self) -> None:
        """The timeline's ends and the Trim panel as this video's trim and pace say."""
        if self.video is None:
            return
        times = self.edits.times.get(self.video["path"], {})
        start = options.parse_time(times["start"]) if times.get("start") else None
        end = options.parse_time(times["end"]) if times.get("end") else None
        self.timeline.set_trim(start, end)
        a, b = self.timeline.kept()
        self.trim_note.setText("All of it, " + clock(b) if start is None and end is None else
                               f"From {clock(a)} to {clock(b)}: {clock(b - a)} kept")
        self.speed_box.setCurrentIndex(max(0, self.speed_box.findData(times.get("speed"))))
        self.pitch.set(times.get("pitch"))
        self.backwards.set(times.get("reverse"))
        if self.playback is not None:
            self.playback.set_speed(float(times.get("speed") or 1))

    def save_frame(self) -> None:
        if self.video is None:
            return
        path = self.video["path"]
        mark = self.watermark
        settings = {**self.edits.of(path).settings(self.video["size"]),
                    **{k: v for k, v in (("watermark", mark["text"]), ("watermark_at", mark["at"]),
                                         ("watermark_image", mark["image"]), ("watermark_opacity", mark["opacity"])) if v}}
        self.frame_btn.setEnabled(False)
        self.frame_note.setText("Saving the frame…")
        job = playback.FrameJob(path, self.video_time, settings)
        job.done.connect(self._frame_saved)
        self._keep(job)
        job.start()

    def _frame_saved(self, out, why: str) -> None:
        self.frame_btn.setEnabled(True)
        self.frame_note.setText(f"Saved as {out.name}, next to the video." if out is not None else why)

    # -- Draw and Hide

    def draw_kind(self, kind: str) -> None:
        """What Draw makes next (or Select, to pick one up); the highlighter keeps a colour of its own."""
        self.stage.draw_kind = kind
        for k, b in self.draw_buttons.items():
            b.setChecked(k == kind)
        if kind != "select":
            self.stage.mark_style["colour"] = self.colours["highlight" if kind == "highlight" else "ink"]
            if self.stage.chosen >= 0:
                self.stage._choose(-1)
        self._chose(self.stage.chosen)

    def _draw_style(self, **change) -> None:
        """A change in the Draw panel: the look of the next drawing, and of the one chosen."""
        if "colour" in change:
            self.colours["highlight" if self.stage.draw_kind == "highlight" else "ink"] = change["colour"]
        self.stage.mark_style.update(change)
        if self.stage.mode == "draw":
            self.stage.update_chosen(**change)
        self._chose(self.stage.chosen)

    def _other_colour(self) -> None:
        chosen = pick_colour(self, QColor(self.stage.mark_style["colour"]), "Drawing colour")
        if chosen is not None:
            self._draw_style(colour=chosen.name())

    def _sticker(self, name: str) -> None:
        """The next sticker; with Select in use, the sticker picked up becomes this one."""
        self.stage.mark_style["name"] = name
        marks, i = self.stage.edit.marks, self.stage.chosen
        if self.stage.draw_kind == "select" and 0 <= i < len(marks) and marks[i]["kind"] == "sticker":
            self.stage.update_chosen(name=name)
        else:
            self.draw_kind("sticker")
        self._chose(self.stage.chosen)

    def _hide_how(self, how: str) -> None:
        self.stage.hide_style["how"] = how
        self.hide_strength.setEnabled(how != "cover")
        self.stage.update_chosen(how=how)

    def _hide_strength(self) -> None:
        self.stage.hide_style["strength"] = self.hide_strength.value()
        self.stage.update_chosen(strength=self.hide_strength.value())

    def _chose(self, i: int) -> None:
        """The bars and the panel show the hidden part or drawing chosen (or what the next one will be)."""
        stage = self.stage
        hides, marks = stage.edit.hides, stage.edit.marks
        self.hide_remove.setEnabled(stage.mode == "hide" and 0 <= i < len(hides))
        self.mark_remove.setEnabled(stage.mode == "draw" and 0 <= i < len(marks))
        hide = hides[i] if stage.mode == "hide" and 0 <= i < len(hides) else stage.hide_style
        self.hide_how.set(hide["how"])
        self.hide_strength.blockSignals(True)
        self.hide_strength.setValue(hide["strength"])
        self.hide_strength.blockSignals(False)
        self.hide_strength_value.setText(str(hide["strength"]))
        self.hide_strength.setEnabled(hide["how"] != "cover")
        mark = dict(stage.mark_style, **(marks[i] if stage.mode == "draw" and 0 <= i < len(marks) else {}))
        for colour, b in self.swatches.items():
            b.setChecked(colour == mark["colour"])
        for w, v in ((self.draw_width, round(mark["width"] * 1000)), (self.sticker_size, round(mark.get("size", 0.08) * 100))):
            w.blockSignals(True)
            w.setValue(v)
            w.blockSignals(False)
        self.draw_width_value.setText(str(self.draw_width.value()))
        self.sticker_size_value.setText(str(self.sticker_size.value()))
        self.draw_fill.set(bool(mark.get("fill", stage.mark_style["fill"])))
        for name, b in self.sticker_buttons.items():
            b.setChecked(name == mark.get("name", stage.mark_style["name"]))
        self._paint_stickers()

    # -- Adjust and Straighten

    def _adjust_moved(self, name: str, value: int) -> None:
        self.adjust_sliders[name][1].setText(f"{value:+d}" if value else "0")
        look = self.edit().look
        got = options.adjustments(look.get("adjust"))
        got[name] = value
        text = options.check_adjust(got)
        if text:
            look["adjust"] = text
        else:
            look.pop("adjust", None)
        self.render_timer.start()
        if not self.adjust_sliders[name][0].isSliderDown():  # moved by a key: a step for Undo now
            self._record()

    def _straighten_moved(self, tenths: int) -> None:
        self.straighten_value.setText(f"{tenths / 10:g}°")
        look = self.edit().look
        angle = options.check_angle(tenths / 10)
        if angle:
            look["straighten"] = angle
        else:
            look.pop("straighten", None)
        self.render_timer.start()
        if not self.straighten.isSliderDown():
            self._record()

    def _straightening(self, on: bool) -> None:
        self.stage.straightening = on
        self.stage.update()
        if not on:
            self._record()

    def turn(self, quarters: int, mirror: bool = False) -> None:
        self.stage.commit()
        self.edit().turn(quarters, mirror)
        frame = self.frames.get(self.current())
        if frame is not None and self.stage.picture is not None:  # turned on screen at once; exact when rendered
            how = QTransform().scale(-1, 1) if mirror else QTransform().rotate(90 * quarters)
            self.stage.set_picture(self.stage.picture.transformed(how), self.edit().turned((frame.width, frame.height)))
        self._render()
        self._record()

    def _shape(self, value) -> None:
        if value == "original":
            frame = self.frames.get(self.current())
            value = self.edit().turned((frame.width, frame.height)) if frame else None
        self.stage.set_lock(tuple(value) if value else None)

    def _look(self, **change) -> None:
        look = self.edit().look
        for k, v in change.items():
            if v:
                look[k] = v
            else:
                look.pop(k, None)
        self._render()
        self._record()

    # -- smart tools

    def _have(self, key: str, then) -> None:
        """then() once the smart tool key is installed: at once if it is, else after asking and downloading it."""
        if engines.available(key):
            then()
            return
        eng = engines.ENGINES[key]
        if not self._ask(f"One-time setup: {eng.name}",
                         f"This needs the {eng.name.lower()} ({engines.size_text(key)}), downloaded once and run on "
                         "this computer: nothing is sent anywhere. Download it now?", "Download"):
            self._sync()
            return
        self._smart_busy(f"Downloading the {eng.name.lower()} ({engines.size_text(key)})…")

        def installed(ok, err: str) -> None:
            self._smart_busy("")
            if ok:
                then()
            else:
                self._smart_say(f"Couldn't download it: {err}" if err else "Couldn't download it")
                self._sync()
        job = SmartJob(lambda: engines.install(key) or None)
        job.done.connect(installed)
        self._keep(job)
        job.start()

    def _ask(self, title: str, text: str, yes: str) -> bool:
        box = Dialog(self, title, {"text": INK["text"], "hover": INK["raised"]})
        box.setMinimumWidth(420)
        lay = QVBoxLayout(box.body)
        lay.setContentsMargins(18, 4, 18, 16)
        lay.setSpacing(14)
        words = QLabel(text)
        words.setWordWrap(True)
        lay.addWidget(words)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel, go = QPushButton("Cancel"), QPushButton(yes)
        go.setObjectName("primary")
        go.setDefault(True)
        cancel.clicked.connect(box.reject)
        go.clicked.connect(box.accept)
        row.addWidget(cancel)
        row.addWidget(go)
        lay.addLayout(row)
        return box.exec() == Dialog.Accepted

    def _smart_busy(self, text: str) -> None:
        for w in (self.background, self.copy_text):
            w.setEnabled(not text)
        self._smart_say(text)

    def _smart_say(self, text: str) -> None:
        self.smart_note.setText(text)
        self.smart_note.setVisible(bool(text))

    def _background(self, how) -> None:
        if not how:
            self._smart_say("")
            self._look(background=None)
            return

        def apply() -> None:
            self._smart_say("Finding the subject: a few seconds the first time")
            self._look(background=how)
        self._have("background", apply)

    def _copy_text(self) -> None:
        frame = self.frames.get(self.current())
        if frame is None:
            return

        def read() -> None:
            self._smart_busy("Reading the text…")

        def got(text, err: str) -> None:
            self._smart_busy("")
            if err:
                self._smart_say(f"Couldn't read it: {err}")
            elif not (text or "").strip():
                self._smart_say("No text found in this picture")
            else:
                QApplication.clipboard().setText(text)
                n = len([line for line in text.splitlines() if line.strip()])
                self._smart_say(f"Copied {n} line{'s' if n != 1 else ''} of text: paste it anywhere")

        def start() -> None:
            read()
            job = SmartJob(lambda: read_text(frame.picture))
            job.done.connect(got)
            self._keep(job)
            job.start()
        self._have("ocr", start)

    def _reset_look(self) -> None:
        turn = self.edit().look.get("rotate")
        self.edit().look.clear()
        if turn:
            self.edit().look["rotate"] = turn
        self._render()
        self._record()

    def _scope(self, scope: str) -> None:
        self.edits.set_scope(self.current(), scope)
        self._record()
        self.go(self.index)
        self._mark_all()

    def _style(self, commit: bool = True, focus: bool = True, **change) -> None:
        """A change in the bar: the look of the next text, and of the one chosen. focus: back to the picture, to
        type on (not for the Size slider, whose arrow keys would then switch file)."""
        self.stage.style.update(change)
        self.stage.update_text(commit=commit, **change)
        if focus:
            self.stage.setFocus()
        self._picked(self.stage.sel)

    def _colour(self) -> None:
        sel = self.stage.sel
        texts = self.stage.edit.texts
        now = QColor(texts[sel]["colour"] if 0 <= sel < len(texts) else self.stage.style["colour"])
        chosen = pick_colour(self, now)
        if chosen is not None:
            self._style(colour=chosen.name())

    def _meme(self) -> None:
        self.stage.commit()
        top = len(self.stage.edit.texts)
        self.stage.add_text(options.text_box("Bottom text", "bottom", meme=True))
        self.stage.add_text(options.text_box("Top text", "top", meme=True), typing=True)
        self.stage.sel = self.stage.editing = top + 1
        self._picked(self.stage.sel)

    def _picked(self, i: int) -> None:
        """The text bar shows the chosen text's look, or the next one's."""
        texts = self.stage.edit.texts
        have = 0 <= i < len(texts)
        self.remove_btn.setEnabled(have)
        t = texts[i] if have else self.stage.style
        for w in (self.font_box, self.size_bar, self.edge_box):
            w.blockSignals(True)
        self.font_box.setCurrentIndex(self.font_box.findData(t["font"]))
        self.size_bar.setValue(round(t["size"] * 100))
        self.edge_box.setCurrentIndex(self.edge_box.findData(t["edge"]))
        for w in (self.font_box, self.size_bar, self.edge_box):
            w.blockSignals(False)
        swatch = QPixmap(20, 14)  # the colour, and 6 px of room before the word beside it
        swatch.fill(Qt.transparent)
        edge = QPainter(swatch)
        edge.fillRect(0, 0, 14, 14, QColor(t["colour"]))
        edge.setPen(QColor(128, 128, 128))
        edge.drawRect(0, 0, 13, 13)
        edge.end()
        self.colour_btn.setIcon(QIcon(swatch))

    def _mark(self, **change) -> None:
        text = options.check_text(self.mark_field.text()) if self.mark_field.text().strip() else ""
        self.watermark = dict(self.watermark, text=text, at=self.mark_corner.currentData(),
                              opacity=self.mark_opacity.value(), **change)
        self.stage.mark = dict(self.watermark)
        self.stage._layer = None
        self.stage.update()
        self._mark_note()

    def _mark_picture(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "A picture for the watermark", self.watermark["image"] or "", PICTURES)
        if path:
            self._mark(image=path)

    def _mark_note(self) -> None:
        """The watermark picture's name under the controls, or why it can't be used."""
        image = self.watermark["image"]
        if not hasattr(self, "mark_note"):
            return
        self.mark_drop.setVisible(bool(image))
        self.mark_pick.setText(Path(image).name if image else "Add a picture…")
        note, bad = "On every file, small and see-through as set. Kept for next time.", False
        if image:
            try:
                overlay.watermark_picture(image)
            except ConversionError as exc:
                note, bad = str(exc), True
        self.mark_note.setText(note)
        self.mark_note.setStyleSheet(f"color: {INK['danger']};" if bad else "")

    def _compare(self, on: bool) -> None:
        self.stage.before = on
        self.stage.update()

    def _stage_changed(self) -> None:
        self._detail()

    def _record(self) -> None:
        self.history.record()
        self._sync()

    def undo(self) -> None:
        self.stage.commit()
        self.history.undo()
        self.go(self.index)
        self._mark_all()

    def redo(self) -> None:
        self.stage.commit()
        self.history.redo()
        self.go(self.index)
        self._mark_all()

    def _sync(self) -> None:
        """Every control as the edit shown says."""
        edit = self.edit()
        self.undo_btn.setEnabled(self.history.can_undo())
        self.redo_btn.setEnabled(self.history.can_redo())
        many = len(self.files) > 1
        self.scope.setVisible(many)
        self.scope_sep.setVisible(many)
        self.scope.set(self.edits.scope(self.current()))
        frame = self.frames.get(self.current())
        named = [s for _, s in SHAPES]
        if edit.lock in named:
            self.shapes.set(edit.lock)
        elif edit.lock and frame is not None and edit.lock == edit.turned((frame.width, frame.height)):
            self.shapes.set("original")
        else:
            self.shapes.set(None)
        self.touch.set(edit.look.get("enhance"))
        self.colour.set(edit.look.get("colour"))
        self.background.blockSignals(True)
        self.background.setCurrentIndex(max(0, self.background.findData(edit.look.get("background"))))
        self.background.blockSignals(False)
        self.fry.blockSignals(True)
        self.fry.setValue(int(edit.look.get("deepfry") or 0))
        self.fry.blockSignals(False)
        self.fry_value.setText(f"{self.fry.value()}%" if self.fry.value() else "Off")
        got = options.adjustments(edit.look.get("adjust"))
        for name, (slider, value) in self.adjust_sliders.items():
            slider.blockSignals(True)
            slider.setValue(got[name])
            slider.blockSignals(False)
            value.setText(f"{got[name]:+d}" if got[name] else "0")
        angle = float(edit.look.get("straighten") or 0)
        self.straighten.blockSignals(True)
        self.straighten.setValue(round(angle * 10))
        self.straighten.blockSignals(False)
        self.straighten_value.setText(f"{angle:g}°")
        self._chose(self.stage.chosen)
        if len(self.files) > 1:
            self._mark_strip(self.index)
        self._detail()

    def _mark_all(self) -> None:
        for i in range(len(self.files) if len(self.files) > 1 else 0):
            self._mark_strip(i)

    def _detail(self) -> None:
        frame = self.frames.get(self.current())
        if frame is None:
            self.detail.setText("Can't be shown: converted without edits")
            return
        w, h = self.stage.pixel_size()
        count = f"{self.index + 1} of {len(self.files)} · " if len(self.files) > 1 else ""
        self.detail.setText(f"{count}{w} × {h} px")

    # ---------------------------------------------------------------- keys and the end

    def keyPressEvent(self, e) -> None:
        keys = {Qt.Key_C: "crop", Qt.Key_A: "adjust", Qt.Key_T: "text", Qt.Key_D: "draw", Qt.Key_H: "hide",
                Qt.Key_V: "trim"}
        video = {Qt.Key_Comma: lambda: self.step(-1), Qt.Key_Period: lambda: self.step(1),
                 Qt.Key_I: lambda: self.trim_here("start"), Qt.Key_O: lambda: self.trim_here("end")}
        if e.key() in keys and not e.modifiers():
            self.choose_tool(keys[e.key()])
        elif self.video is not None and e.key() in video and not e.modifiers():
            video[e.key()]()
        elif e.key() == Qt.Key_Backslash and not e.isAutoRepeat():
            self._compare(True)
        elif e.key() in (Qt.Key_Left, Qt.Key_PageUp) and len(self.files) > 1:
            self.go(self.index - 1)
        elif e.key() in (Qt.Key_Right, Qt.Key_PageDown) and len(self.files) > 1:
            self.go(self.index + 1)
        else:
            super().keyPressEvent(e)

    def keyReleaseEvent(self, e) -> None:
        if e.key() == Qt.Key_Backslash and not e.isAutoRepeat():
            self._compare(False)
        else:
            super().keyReleaseEvent(e)

    def result(self) -> dict[Path, dict]:
        """Each file's settings from the edits (a box kept to a shape is fitted to each file's own size)."""
        sizes = {}
        for path, fmt in self.files:
            edit = self.edits.of(path)
            if edit.lock:
                frame = self.frames.get(path)
                sizes[path] = (frame.width, frame.height) if frame else framing.size_of(path, fmt)
        return self.edits.settings(sizes)

    def finish(self) -> None:
        self.stage.commit()
        self.done.emit(self.result(), self.watermark, self.edits)
        self.close()

    def closeEvent(self, e) -> None:
        self._close_video()
        jobs.let_go(self.jobs)  # a smart tool downloading finishes on its own: the window doesn't wait for it
        super().closeEvent(e)
