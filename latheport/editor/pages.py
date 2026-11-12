"""The Pages window, for PDFs: every page as a picture in a grid, to put in another order (drag them), turn
or take out, with Undo; the pages picked saved as a PDF of their own. PDFs joined show as one grid, so they're
merged and ordered at once; otherwise each PDF has its own, chosen in the strip at the bottom.

What it gives back is a page list for each PDF ("3,1-2,5r"), or one for the PDFs joined, counted across them.
"""

from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt, QThread, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QImage,
    QKeySequence,
    QPainter,
    QPen,
    QShortcut,
    QTransform,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSlider,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from .. import options, pdfium
from ..chrome import CaptionButtons, Frameless
from . import jobs
from .look import GAP, INK, RAIL, IconButton, stylesheet

THUMB = 260   # pixels: how tall each page is drawn (shown smaller)
CHUNK = 8     # pages drawn while a PDF is open
ROLE = Qt.UserRole


def page_counts(files: list[Path]) -> list[int]:
    out = []
    for path in files:
        with pdfium.opened(path) as doc:
            out.append(len(doc))
    return out


class ThumbsJob(QThread):
    """Each page drawn small, one after another: got(file index, page index, QImage)."""
    got = Signal(int, int, object)

    def __init__(self, files: list[Path], wanted: list[tuple[int, int]]):
        super().__init__()
        self.files, self.wanted = files, wanted

    def run(self) -> None:
        from .frames import qimage
        by_file: dict[int, list[int]] = {}
        for f, p in self.wanted:
            by_file.setdefault(f, []).append(p)
        for f, pages in by_file.items():
            for start in range(0, len(pages), CHUNK):  # PDFium is let go between chunks: conversions get a turn
                if self.isInterruptionRequested():
                    return
                drawn = []
                try:
                    with pdfium.opened(self.files[f]) as doc:
                        for p in pages[start:start + CHUNK]:
                            try:
                                page = doc[p]
                                drawn.append((p, pdfium.render(page, THUMB / max(1.0, page.get_height()))))
                            except Exception:  # noqa: BLE001 - one bad page leaves a blank, not the rest
                                drawn.append((p, None))
                except Exception:  # noqa: BLE001 - a damaged PDF shows blank pages
                    drawn = [(p, None) for p in pages[start:start + CHUNK]]
                for p, img in drawn:
                    self.got.emit(f, p, qimage(img) if img is not None else None)


class SaveJob(QThread):
    """The pages picked as a PDF next to the first one's file: done(path or None, why not)."""
    done = Signal(object, str)

    def __init__(self, files: list[Path], plan: list[tuple[int, int]], out: Path):
        super().__init__()
        self.files, self.plan, self.out = files, plan, out

    def run(self) -> None:
        from bookport.errors import BookportError
        from bookport.pdf import rewrite
        try:
            data = rewrite.edit([p.read_bytes() for p in self.files], pages=self.plan)[0]
            self.out.write_bytes(data)
            self.done.emit(self.out, "")
        except (BookportError, OSError) as exc:
            self.done.emit(None, f"Those pages couldn't be saved: {exc}")


class PageCard(QStyledItemDelegate):
    """A page in the grid: its picture (turned as it will be), its number under it, the accent when picked."""

    def __init__(self, window: PagesWindow):
        super().__init__(window)
        self.win = window

    def sizeHint(self, option, index):
        side = self.win.card
        return QSize(side, round(side * 1.32) + 26)

    def paint(self, p: QPainter, option, index) -> None:
        f, page, quarters = index.data(ROLE)
        r = QRectF(option.rect).adjusted(6, 6, -6, -6)
        picked = self.win.grid.selectionModel().isSelected(index)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        if picked:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(INK["accent_soft"]))
            p.drawRoundedRect(r, 10, 10)
        area = r.adjusted(10, 10, -10, -30)
        img = self.win.picture(f, page, quarters)
        if img is None:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(INK["raised"]))
            p.drawRoundedRect(area.adjusted(area.width() * 0.12, 0, -area.width() * 0.12, 0), 4, 4)
        else:
            scale = min(area.width() / img.width(), area.height() / img.height())
            w, h = img.width() * scale, img.height() * scale
            box = QRectF(area.center().x() - w / 2, area.center().y() - h / 2, w, h)
            p.setPen(QPen(QColor(0, 0, 0, 90), 4))
            p.drawRect(box.adjusted(1, 1, 1, 1))
            p.drawImage(box, img)
            if picked:
                p.setPen(QPen(QColor(INK["accent"]), 2.5))
                p.setBrush(Qt.NoBrush)
                p.drawRect(box.adjusted(-2, -2, 2, 2))
        p.setPen(QColor(INK["text"] if picked else INK["muted"]))
        font = QFont(option.font)
        font.setPointSizeF(8.5)
        if picked:
            font.setWeight(QFont.DemiBold)
        p.setFont(font)
        label = f"{page + 1}" if not self.win.joined or len(self.win.files) == 1 else f"{self.win.short(f)} · {page + 1}"
        p.drawText(QRectF(r.left(), r.bottom() - 24, r.width(), 20), Qt.AlignCenter, label)
        p.restore()


class PagesWindow(Frameless, QWidget):
    """done(each PDF's page list (only the ones changed), the joined PDFs' page list or None, state to open
    again where it was)."""
    done = Signal(object, object, object)

    def __init__(self, files: list[Path], joined: bool, state: dict | None = None):
        super().__init__()
        self.setObjectName("editor")
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setAttribute(Qt.WA_StyledBackground)
        self.setStyleSheet(stylesheet())
        self.setWindowTitle("Pages · Latheport")
        self.resize(1180, 800)
        self.setMinimumSize(860, 560)
        self.files, self.joined = list(files), joined
        self.counts = page_counts(self.files)
        self.offsets = [sum(self.counts[:i]) for i in range(len(self.files))]
        keys = ["all"] if joined else list(range(len(self.files)))
        fresh = {k: [(f, p, 0) for f in (range(len(self.files)) if k == "all" else [k]) for p in range(self.counts[f])]
                 for k in keys}
        same = state is not None and state.get("files") == self.files and state.get("joined") == joined
        self.layouts: dict = {k: list(v) for k, v in (state["layouts"] if same else fresh).items()}
        self.history = {k: [list(v)] for k, v in self.layouts.items()}
        self.at = dict.fromkeys(self.layouts, 0)
        self.key = keys[0]
        self.card = 150
        self.pictures: dict[tuple[int, int], QImage] = {}
        self.turned: dict[tuple[int, int, int], QImage] = {}
        self.jobs: list[QThread] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._bar())
        self.grid = QListWidget()
        self.grid.setObjectName("pages")
        self.grid.setViewMode(QListView.ListMode)
        self.grid.setFlow(QListView.LeftToRight)
        self.grid.setWrapping(True)
        self.grid.setResizeMode(QListView.Adjust)
        self.grid.setUniformItemSizes(True)
        self.grid.setSpacing(6)
        self.grid.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.grid.setDragDropMode(QAbstractItemView.InternalMove)
        self.grid.setDefaultDropAction(Qt.MoveAction)
        self.grid.setItemDelegate(PageCard(self))
        self.grid.setStyleSheet(f"QListWidget#pages {{ background: {INK['stage']}; border: none; padding: 14px; }}")
        self.grid.model().rowsMoved.connect(lambda *_: self._moved())
        self.grid.itemSelectionChanged.connect(self._picked)
        root.addWidget(self.grid, 1)
        root.addWidget(self._footer())
        self.frameless(self.bar, self.caption)
        for keys_, fn in ((QKeySequence.Undo, self.undo), (QKeySequence.Redo, self.redo), ("Ctrl+Shift+Z", self.redo),
                          ("Ctrl+[", lambda: self.turn(-1)), ("Ctrl+]", lambda: self.turn(1)),
                          (QKeySequence.Delete, self.remove), ("Backspace", self.remove), ("Ctrl+Return", self.finish)):
            QShortcut(QKeySequence(keys_), self, fn)
        self.show_layout(self.key)
        wanted = [(f, p) for f in range(len(self.files)) for p in range(self.counts[f])]
        job = ThumbsJob(self.files, wanted)
        job.got.connect(self._thumb)
        self._keep(job)
        job.start()

    def _keep(self, job: QThread) -> None:
        jobs.keep(self.jobs, job)

    # ---------------------------------------------------------------- building it

    def _bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("bar")
        bar.setFixedHeight(52)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(12, 10, 10, 10)
        lay.setSpacing(GAP)
        name = QLabel("Pages")
        name.setObjectName("name")
        name.setFixedWidth(RAIL - 12)
        lay.addWidget(name)
        left, right = IconButton("turn_left", "Turn left (Ctrl+[)"), IconButton("turn_right", "Turn right (Ctrl+])")
        left.clicked.connect(lambda: self.turn(-1))
        right.clicked.connect(lambda: self.turn(1))
        self.remove_btn = IconButton("delete", "Take the pages picked out (Delete)")
        self.remove_btn.clicked.connect(self.remove)
        self.save_btn = QPushButton("Save the picked pages as a PDF")
        self.save_btn.setToolTip("The pages picked, in this order, as a PDF of their own next to the file")
        self.save_btn.clicked.connect(self.save_picked)
        everything = QPushButton("Pick all")
        everything.clicked.connect(lambda: self.grid.selectAll())
        for w in (left, right, self.remove_btn, self._sep(), everything, self.save_btn):
            lay.addWidget(w)
        lay.addStretch(1)
        self.undo_btn, self.redo_btn = IconButton("undo", "Undo (Ctrl+Z)"), IconButton("redo", "Redo (Ctrl+Y)")
        self.undo_btn.clicked.connect(self.undo)
        self.redo_btn.clicked.connect(self.redo)
        self.size = QSlider(Qt.Horizontal)
        self.size.setRange(90, 300)
        self.size.setValue(self.card)
        self.size.setFixedWidth(110)
        self.size.setToolTip("How big the pages show")
        self.size.valueChanged.connect(self._size)
        self.caption = CaptionButtons({"text": INK["text"], "hover": INK["raised"]})
        for w in (self.undo_btn, self.redo_btn, self._sep(), self.size, self._sep(), self.caption):
            lay.addWidget(w)
        self.bar = bar
        return bar

    @staticmethod
    def _sep():
        from .window import _sep
        return _sep()

    def _footer(self) -> QWidget:
        footer = QWidget()
        footer.setObjectName("footer")
        lay = QHBoxLayout(footer)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(12)
        self.strip = QListWidget()
        self.strip.setObjectName("strip")
        self.strip.setFlow(QListWidget.LeftToRight)
        self.strip.setFixedHeight(40)
        self.strip.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.strip.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        for path in self.files:
            self.strip.addItem(QListWidgetItem(path.name))
        self.strip.currentRowChanged.connect(lambda i: i >= 0 and self.show_layout(i))
        self.strip.setVisible(not self.joined and len(self.files) > 1)
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
        lay.addLayout(names, 0 if self.strip.isVisibleTo(self) or (not self.joined and len(self.files) > 1) else 1)
        self.cancel_btn, self.done_btn = QPushButton("Cancel"), QPushButton("Done")
        self.done_btn.setObjectName("primary")
        self.done_btn.setToolTip("Keep these pages for the conversion (Ctrl+Enter)")
        self.cancel_btn.clicked.connect(self.close)
        self.done_btn.clicked.connect(self.finish)
        lay.addWidget(self.cancel_btn)
        lay.addWidget(self.done_btn)
        footer.setFixedHeight(56)
        return footer

    # ---------------------------------------------------------------- what it shows

    def short(self, f: int) -> str:
        """A PDF's name, short enough for under a page."""
        stem = self.files[f].stem
        return stem if len(stem) <= 14 else stem[:13] + "…"

    def picture(self, f: int, page: int, quarters: int) -> QImage | None:
        img = self.pictures.get((f, page))
        if img is None or not quarters:
            return img
        key = (f, page, quarters)
        if key not in self.turned:
            self.turned[key] = img.transformed(QTransform().rotate(90 * quarters))
        return self.turned[key]

    def _thumb(self, f: int, page: int, img) -> None:
        if img is not None:
            self.pictures[(f, page)] = img
            self.grid.viewport().update()

    def show_layout(self, key, take: bool = True) -> None:
        if take:  # the order shown kept first (it may have been dragged); not when Undo puts one back
            self._take()
        self.key = key
        self.grid.blockSignals(True)
        self.grid.clear()
        for entry in self.layouts[key]:
            item = QListWidgetItem()
            item.setData(ROLE, entry)
            item.setFlags(item.flags() | Qt.ItemIsDragEnabled)
            self.grid.addItem(item)
        self.grid.blockSignals(False)
        if not self.joined and len(self.files) > 1:
            self.strip.blockSignals(True)
            self.strip.setCurrentRow(key)
            self.strip.blockSignals(False)
        self._sync()

    def _size(self, value: int) -> None:
        self.card = value
        self.grid.doItemsLayout()
        self.grid.viewport().update()

    def entries(self) -> list[tuple[int, int, int]]:
        return [self.grid.item(i).data(ROLE) for i in range(self.grid.count())]

    def picked_rows(self) -> list[int]:
        return sorted(self.grid.row(it) for it in self.grid.selectedItems())

    def _take(self) -> None:
        """The grid's order into the layout shown (it may have been dragged)."""
        if self.key in self.layouts and self.grid.count():
            self.layouts[self.key] = self.entries()

    # ---------------------------------------------------------------- changes

    def _record(self) -> None:
        self._take()
        h, k = self.history[self.key], self.key
        if h[self.at[k]] == self.layouts[k]:
            return
        del h[self.at[k] + 1:]
        h.append(list(self.layouts[k]))
        self.at[k] = len(h) - 1
        self._sync()

    def _moved(self) -> None:
        self._record()

    def turn(self, quarters: int) -> None:
        rows = self.picked_rows()
        if not rows:
            return
        for r in rows:
            item = self.grid.item(r)
            f, p, q = item.data(ROLE)
            item.setData(ROLE, (f, p, (q + quarters) % 4))
        self._record()
        self.grid.viewport().update()

    def remove(self) -> None:
        rows = self.picked_rows()
        if not rows:
            return
        if len(rows) >= self.grid.count():
            self.detail.setText("A PDF needs one page at least: pick fewer to take out")
            return
        for r in reversed(rows):
            self.grid.takeItem(r)
        self._record()

    def undo(self) -> None:
        k = self.key
        if self.at[k] > 0:
            self.at[k] -= 1
            self.layouts[k] = list(self.history[k][self.at[k]])
            self.show_layout(k, take=False)

    def redo(self) -> None:
        k = self.key
        if self.at[k] < len(self.history[k]) - 1:
            self.at[k] += 1
            self.layouts[k] = list(self.history[k][self.at[k]])
            self.show_layout(k, take=False)

    def _picked(self) -> None:
        self._sync()

    def _sync(self) -> None:
        k = self.key
        self.undo_btn.setEnabled(self.at[k] > 0)
        self.redo_btn.setEnabled(self.at[k] < len(self.history[k]) - 1)
        n, picked = self.grid.count(), len(self.grid.selectedItems())
        self.remove_btn.setEnabled(0 < picked < n)
        self.save_btn.setEnabled(picked > 0)
        if self.joined and len(self.files) > 1:
            self.name.setText(f"{len(self.files)} PDFs, joined")
        else:
            self.name.setText(self.files[k if k != "all" else 0].name)
        total = sum(self.counts) if k == "all" else self.counts[k]
        words = f"{n} of {total} pages" if n != total else f"{n} page{'s' if n != 1 else ''}"
        self.detail.setText(words + (f" · {picked} picked" if picked else ""))

    # ---------------------------------------------------------------- the pages picked, and the end

    def plan(self, entries) -> list[tuple[int, int]]:
        """(page index, quarter turns) as the conversion counts them: across the PDFs when joined."""
        if self.joined:
            return [(self.offsets[f] + p, q) for f, p, q in entries]
        return [(p, q) for _f, p, q in entries]

    def save_picked(self) -> None:
        self._take()
        rows = self.picked_rows()
        if not rows:
            return
        entries = [self.grid.item(r).data(ROLE) for r in rows]
        files = self.files if self.joined else [self.files[self.key]]
        plan = self.plan(entries)
        first = files[0]
        words = options.pages_text(plan) if not self.joined or len(self.files) == 1 else f"{len(plan)} pages"
        safe = re.sub(r"[^0-9A-Za-z ,-]", "", words)[:40]
        from ..pipeline import unique_path
        out = unique_path(first.with_name(f"{first.stem} pages {safe}.pdf"))
        self.save_btn.setEnabled(False)
        self.detail.setText("Saving the pages picked…")
        job = SaveJob(files, plan, out)
        job.done.connect(self._saved)
        self._keep(job)
        job.start()

    def _saved(self, out, why: str) -> None:
        self._sync()
        self.detail.setText(f"Saved as {out.name}, next to the PDF" if out is not None else why)

    def result(self) -> tuple[dict, str | None]:
        self._take()
        if self.joined:
            plan = self.plan(self.layouts["all"])
            whole = [(i, 0) for i in range(sum(self.counts))]
            return {}, (options.pages_text(plan) if plan != whole else None)
        got = {}
        for f, entries in self.layouts.items():
            plan = self.plan(entries)
            if plan != [(i, 0) for i in range(self.counts[f])]:
                got[self.files[f]] = options.pages_text(plan)
        return got, None

    def state(self) -> dict:
        self._take()
        return {"files": self.files, "joined": self.joined, "layouts": {k: list(v) for k, v in self.layouts.items()}}

    def finish(self) -> None:
        per_file, joined = self.result()
        self.done.emit(per_file, joined, self.state())
        self.close()

    def closeEvent(self, e) -> None:
        jobs.let_go(self.jobs)  # a big save still going runs on to its end, kept in jobs.RUNNING
        super().closeEvent(e)
