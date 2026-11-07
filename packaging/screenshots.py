"""The README's pictures, taken from the real window (never mocked up), with sample files made
here (never anyone's own):

    .venv\\Scripts\\python packaging\\screenshots.py [BIG_ARCHIVE | --promo]

- screenshots/convertall.png: the window, dark, with a video to cut and fit in 10 MB, beside the
  release: the engines it no longer needs (GONE), measured times against the last one (TIMES) and
  what's new (NEW). It heads the README and is the picture for the release's post.
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

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSettings, Qt
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

NEW = ["Cut, fit in a size, even out loudness, join", "Watch folders, and a queue for what you drop"]
GONE = "No ImageMagick, Pandoc or DjVuLibre: 330 MB less to install"
# seconds against 1.3.0 with those engines, same files, same PC, the two taking turns on each file, best of 3: what, before, now
TIMES = [("20 DjVu books to PDF", 388.8, 28.9), ("Markup files to HTML", 8.18, 1.43), ("12 RAW photos to PNG", 91.7, 59.0)]


def pump(app, until, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while not until() and time.monotonic() < deadline:
        app.processEvents()
        # processEvents leaves these for the event loop proper: rows replaced would stay on screen
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
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


def long_video(work: Path) -> Path:
    """Six minutes of FFmpeg's test picture and tone at 720p, big enough (about 130 MB) that fitting it in 10 MB makes sense."""
    from convertall import engines
    path = work / "talk.mp4"
    engines.run([engines.find("ffmpeg"), "-hide_banner", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24:duration=360",
                 "-f", "lavfi", "-i", "sine=frequency=330:duration=360", "-vf", "noise=alls=12:allf=t", "-c:v", "libx264",
                 "-preset", "ultrafast", "-b:v", "2800k", "-threads", "4", "-c:a", "aac", "-shortest", str(path)])
    return path


def cutting(app, theme: str, video: Path, work: Path) -> QImage:
    """The window with a video to cut and fit in 10 MB, its Options open."""
    w = window(app, theme, work)
    try:
        w.set_files([video])
        pump(app, lambda: False, 0.5)
        w.picker.set_current("mp4")
        pump(app, lambda: False, 0.2)
        panel = w.options_panel
        panel.head.click()
        pump(app, lambda: False, 0.3)
        for key, value in (("start", "1:30"), ("end", "5:00"), ("target_size", 10)):
            panel.widgets[key].setValue(value)
        pump(app, lambda: False, 0.5)
        return w.grab().toImage()
    finally:
        settle(app)
        w.close()
        settle(app)


def promo(shot: QImage) -> QImage:
    """1600 x 900 (an X post's picture, and the top of the README): the real window on the left; on the
    right the release, the engines it no longer needs, its times beside the last one's, and what's new."""
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
    x, y = (780 - scaled.width()) // 2, (900 - scaled.height()) // 2
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
    # the engines gone, then the times, before and now
    text(GONE, 24, dark["accent"], QPoint(left, 300), QFont.DemiBold)
    text("SECONDS, 1.3.0 AND ITS ENGINES AGAINST " + __version__, 17, dark["muted"], QPoint(left, 356), QFont.DemiBold)

    def seconds(v: float) -> str:
        return f"{v:.1f} s" if v >= 1 else f"{v:.2f} s"

    for k, (what, before, now) in enumerate(TIMES):
        top = 390 + 92 * k
        text(what, 21, dark["text"], QPoint(left, top), QFont.DemiBold)
        for j, (v, colour) in enumerate(((before, QColor(dark["border"])), (now, accent))):
            bar = QPainterPath()
            width = max(6.0, 500 * v / before)
            bar.addRoundedRect(QRectF(left, top + 32 + 24 * j, width, 18), 5, 5)
            p.fillPath(bar, colour)
            text(seconds(v), 17, dark["muted"] if j == 0 else dark["text"], QPoint(left + int(width) + 12, top + 29 + 24 * j),
                 QFont.Normal if j == 0 else QFont.Bold, 160)
        text(f"{round(before / now, 1):g}x", 32, dark["accent"],
             QPoint(left + 590, top + 26), QFont.Bold, 130, Qt.AlignRight)
    p.setPen(QPen(QColor(dark["border"]), 2))
    p.drawLine(left, 672, 1520, 672)
    for k, line in enumerate(NEW):
        p.setBrush(accent)
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPoint(left + 6, 711 + 42 * k), 5, 5)
        text(line, 24, dark["text"], QPoint(left + 28, 695 + 42 * k))
    text("github.com/puretechteam/ConvertAll", 24, dark["accent"], QPoint(left, 790))
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
        extracting(app, theme, src, work).save(str(OUT / f"extracting_{theme}.png"))
    elif kind == "cutting":
        promo(cutting(app, theme, src, work)).save(str(OUT / "convertall.png"))
    else:
        after_converting(app, theme, src, work).save(str(OUT / f"after_converting_{theme}.png"))


def main() -> int:
    import subprocess

    from samples import Samples
    if len(sys.argv) > 1 and sys.argv[1] == "--one":
        one(sys.argv[2], sys.argv[3], Path(sys.argv[4]), Path(sys.argv[5]))
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == "--promo":  # only the release's picture
        with tempfile.TemporaryDirectory(prefix="convertall-shots-") as tmp:
            subprocess.run([sys.executable, __file__, "--one", "cutting", "dark", str(long_video(Path(tmp))), tmp], check=True)
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
        subprocess.run([sys.executable, __file__, "--one", "cutting", "dark", str(long_video(work)), str(work)], check=True)
    for p in sorted(OUT.glob("*.png")):
        img = QImage(str(p))
        print(f"{p.name}: {img.width()} x {img.height()}, {p.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
