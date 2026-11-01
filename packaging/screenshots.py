"""The README's pictures, taken from the real window (never mocked up), with sample files made
here (never anyone's own):

    .venv\\Scripts\\python packaging\\screenshots.py [BIG_ARCHIVE]

- screenshots/convertall.png: the window, dark, extracting a big archive, beside the release: its
  download against the last one's (DOWNLOAD, measured from the zips) and what's new (NEW). It heads
  the README and is the picture for the release's post.
- screenshots/after_converting_{light,dark}.png: a two-part GLB made into an STL, with Details open
- screenshots/extracting_{light,dark}.png: a big archive unpacking, megabytes done and Cancel

BIG_ARCHIVE: a 7z that takes several seconds to unpack (PPMd is slow on purpose); without one, a
100 MB one is made in a temporary folder first (about a minute).
"""

from __future__ import annotations

import random
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
OUT = ROOT / "screenshots"

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSettings, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import QApplication

from convertall import __version__, gui, registry

NEW = ["No 7-Zip needed: every archive is read built in", "Cancel any time, and a safe close",
       "Disk images too: VHD, VMDK, DMG and more"]
# the release zip, measured (MB): the one before, and this one
DOWNLOAD = [("1.2.0", 179.1), (__version__, 81.7)]


def pump(app, until, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while not until() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    return until()


def big_archive(work: Path) -> Path:
    """100 MB of made-up text in a PPMd 7z: slow enough to unpack to photograph it part-way."""
    import py7zr
    rng = random.Random(13)
    words = [w for w in ["the", "quick", "brown", "fox", "jumps", "over", "a", "lazy", "dog", "while", "seven", "archives", "unpack", "in", "time"]]
    text = work / "book.txt"
    with text.open("w", encoding="ascii") as f:
        while f.tell() < 100 << 20:
            f.write(" ".join(rng.choice(words) for _ in range(5000)) + "\n")
    arc = work / "big archive.7z"
    with py7zr.SevenZipFile(arc, "w", filters=[{"id": py7zr.FILTER_PPMD, "order": 6, "mem": 24}]) as z:
        z.write(text, "book.txt")
    text.unlink()
    return arc


def settle(app) -> None:
    """Let a 3D preview that's loading on its thread finish (a window closed under it crashes)."""
    viewer = sys.modules.get("convertall.viewer")
    for job in list(viewer._RUNNING) if viewer else []:
        job.wait()
    app.processEvents()


def window(app, theme: str, settings_dir: Path):
    gui.is_dark = lambda t=theme: t == "dark"
    gui.apply_theme(app)
    settings = QSettings(str(settings_dir / f"settings-{theme}.ini"), QSettings.IniFormat)
    gui.QSettings = lambda *a: settings
    w = gui.Window()
    w.show()
    pump(app, lambda: False, 0.4)
    return w


def extracting(app, theme: str, archive: Path, work: Path) -> QImage:
    """The window part-way through unpacking archive: megabytes done, and Cancel."""
    w = window(app, theme, work)
    try:
        w.set_files([archive])
        w.start_extract()
        # packport unpacks it in a few seconds: photographed as soon as the megabytes show
        if not pump(app, lambda: " MB" in w.status.text() and " of " in w.status.text(), 60):
            raise RuntimeError(f"no megabytes in the status line: {w.status.text()!r}")
        pump(app, lambda: False, 0.3)
        shot = w.grab().toImage()
        job = w.job
        if job is None:
            raise RuntimeError("the archive finished unpacking before its picture: use a bigger one")
        w.confirm_stop = lambda: True
        w.convert_clicked()  # Cancel
        pump(app, lambda: w.job is None, 20)
        job.wait()
        return shot
    finally:
        w.close()
        settle(app)
        for p in archive.parent.iterdir():
            if p != archive and p.stem.startswith(archive.stem):
                shutil.rmtree(p, ignore_errors=True)


def after_converting(app, theme: str, model: Path, work: Path) -> QImage:
    """A two-part GLB made into an STL, the window and its Details side by side."""
    w = window(app, theme, work)
    try:
        w.set_files([model])
        pump(app, lambda: False, 1.5)  # the 3D preview loads
        settle(app)
        w.picker.set_current("stl")
        w.convert_clicked()
        pump(app, lambda: w.job is None, 60)
        pump(app, lambda: False, 1.0)
        dialog = w.show_details()
        pump(app, lambda: False, 0.6)
        a, b = w.grab().toImage(), dialog.grab().toImage()
        dialog.close()
        pad = 24
        theme_bg = QColor(gui.DARK["bg"] if theme == "dark" else gui.LIGHT["bg"])
        img = QImage(a.width() + b.width() + 3 * pad, max(a.height(), b.height()) + 2 * pad, QImage.Format_RGB32)
        img.fill(theme_bg)
        p = QPainter(img)
        p.drawImage(pad, pad, a)
        p.drawImage(2 * pad + a.width(), pad, b)
        p.end()
        return img
    finally:
        settle(app)
        w.close()
        settle(app)
        for p in model.parent.glob(f"{model.stem}*.stl"):
            p.unlink()


def promo(shot: QImage) -> QImage:
    """1600 x 900 (an X post's picture, and the top of the README): the window part-way through an
    archive on the left; on the right the release, its download beside the last one's, and what's new."""
    dark = gui.DARK
    img = QImage(1600, 900, QImage.Format_RGB32)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    bg = QLinearGradient(0, 0, 1600, 900)
    bg.setColorAt(0, QColor("#14151d"))
    bg.setColorAt(1, QColor("#20223a"))
    p.fillRect(img.rect(), bg)
    # a soft glow of the accent colour behind the window
    glow = QRadialGradient(QPointF(400, 450), 520)
    accent = QColor(dark["accent"])
    glow.setColorAt(0, QColor(accent.red(), accent.green(), accent.blue(), 70))
    glow.setColorAt(1, QColor(accent.red(), accent.green(), accent.blue(), 0))
    p.fillRect(img.rect(), glow)
    # the window, scaled to fit, on a card
    scaled = shot.scaled(640, 760, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    x, y = 80, (900 - scaled.height()) // 2
    card = QPainterPath()
    card.addRoundedRect(QRectF(x - 1, y - 1, scaled.width() + 2, scaled.height() + 2), 18, 18)
    p.fillPath(card, QColor(dark["border"]))
    clip = QPainterPath()
    clip.addRoundedRect(QRectF(x, y, scaled.width(), scaled.height()), 17, 17)
    p.save()
    p.setClipPath(clip)
    p.drawImage(x, y, scaled)
    p.restore()

    def text(s, size, color, at, weight=QFont.Normal, width=760, align=Qt.AlignLeft):
        f = QFont("Segoe UI", 1)
        f.setPixelSize(size)
        f.setWeight(weight)
        p.setFont(f)
        p.setPen(QColor(color))
        p.drawText(QRect(at.x(), at.y(), width, size * 2), align | Qt.AlignTop, s)

    left = 800
    icon = QImage(str(ROOT / "convertall" / "assets" / "icon.png")).scaled(64, 64, Qt.KeepAspectRatio,
                                                                          Qt.SmoothTransformation)
    p.drawImage(left, 92, icon)
    text("ConvertAll", 56, dark["text"], QPoint(left + 84, 84), QFont.DemiBold)
    # the version as a pill beside the name
    f = QFont("Segoe UI", 1)
    f.setPixelSize(56)
    f.setWeight(QFont.DemiBold)
    name_w = QFontMetrics(f).horizontalAdvance("ConvertAll")
    pill = QRectF(left + 84 + name_w + 22, 104, 110, 42)
    path = QPainterPath()
    path.addRoundedRect(pill, 21, 21)
    p.fillPath(path, QColor(accent.red(), accent.green(), accent.blue(), 60))
    text(__version__, 24, dark["accent"], QPoint(int(pill.x()), int(pill.y()) + 5), QFont.Bold, int(pill.width()),
         Qt.AlignHCenter)
    text("Drop a file. Pick a format. Done.", 34, dark["text"], QPoint(left, 190), QFont.DemiBold)
    ins = {s for s in registry._edges if s != "*"}
    outs = {e.dst for es in registry._edges.values() for e in es}
    text(f"{len(ins)} formats in, {len(outs)} out. Free and open source, for Windows.", 22, dark["muted"],
         QPoint(left, 244))
    # the download, then and now
    text("THE DOWNLOAD", 17, dark["muted"], QPoint(left, 330), QFont.DemiBold)
    biggest = max(mb for _v, mb in DOWNLOAD)
    for k, (version, mb) in enumerate(DOWNLOAD):
        top = 372 + 70 * k
        now = version == __version__
        text(version, 24, dark["text"] if now else dark["muted"], QPoint(left, top + 4), QFont.DemiBold, 90)
        bar = QPainterPath()
        bar.addRoundedRect(QRectF(left + 96, top + 6, 500 * mb / biggest, 34), 8, 8)
        p.fillPath(bar, accent if now else QColor(dark["border"]))
        text(f"{mb:.0f} MB", 28, dark["text"] if now else dark["muted"],
             QPoint(left + 96 + int(500 * mb / biggest) + 16, top + 2), QFont.Bold if now else QFont.DemiBold, 160)
    text(f"{100 * (1 - DOWNLOAD[-1][1] / DOWNLOAD[0][1]):.0f}% smaller", 22, dark["accent"], QPoint(left + 96, 512),
         QFont.DemiBold)
    p.setPen(QPen(QColor(dark["border"]), 2))
    p.drawLine(left, 572, 1520, 572)
    text(f"ALSO NEW IN {__version__}", 17, dark["muted"], QPoint(left, 598), QFont.DemiBold)
    for k, line in enumerate(NEW):
        p.setBrush(accent)
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPoint(left + 6, 655 + 48 * k), 5, 5)
        text(line, 26, dark["text"], QPoint(left + 28, 637 + 48 * k))
    text("github.com/puretechteam/ConvertAll", 24, dark["accent"], QPoint(left, 806))
    p.end()
    return img


APP: list = []  # the Qt application lives as long as the process: windows must go before it does


def one(kind: str, theme: str, src: Path, work: Path) -> None:
    """One picture, in a process of its own (as the app runs: one window per process)."""
    import gc
    APP.append(QApplication.instance() or QApplication(sys.argv[:1]))
    app = APP[0]
    try:
        _one(app, kind, theme, src, work)
    finally:
        gc.collect()  # the closed window and its jobs, while Qt is still there
        app.processEvents()


def _one(app, kind: str, theme: str, src: Path, work: Path) -> None:
    if kind == "extracting":
        shot = extracting(app, theme, src, work)
        shot.save(str(OUT / f"extracting_{theme}.png"))
        if theme == "dark":
            promo(shot).save(str(OUT / "convertall.png"))
    else:
        after_converting(app, theme, src, work).save(str(OUT / f"after_converting_{theme}.png"))


def main() -> int:
    import subprocess

    from samples import Samples
    if len(sys.argv) > 1 and sys.argv[1] == "--one":
        one(sys.argv[2], sys.argv[3], Path(sys.argv[4]), Path(sys.argv[5]))
        return 0
    with tempfile.TemporaryDirectory(prefix="convertall-shots-") as tmp:
        work = Path(tmp)
        archive = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None
        if archive is None:
            print("Making a 100 MB archive to unpack...")
            archive = big_archive(work)
        else:
            copy = work / "big archive.7z"  # its unpacked files land next to it: in the temporary folder
            shutil.copyfile(archive, copy)
            archive = copy
        model = Samples(work / "samples")("glb")
        bracket = model.with_name("bracket.glb")
        shutil.copyfile(model, bracket)
        for theme in ("light", "dark"):
            for kind, src in (("extracting", archive), ("after_converting", bracket)):
                subprocess.run([sys.executable, __file__, "--one", kind, theme, str(src), str(work)], check=True)
    for p in sorted(OUT.glob("*.png")):
        img = QImage(str(p))
        print(f"{p.name}: {img.width()} x {img.height()}, {p.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
