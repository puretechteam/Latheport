"""Playing a video in the editor: puretech-player decodes it (FFmpeg) and plays its sound (WASAPI), and hands
each frame to the stage here; FFmpeg makes the still shown when it's paused, with the edits exactly as the
conversion will make them; and the timeline's strip of pictures.

Frames arrive on the player's own thread. Only the newest one is kept: the window takes it when it gets to it,
so a slow screen skips frames instead of falling behind.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtGui import QImage

from .. import engines

SOUND = True        # tests turn the speakers off
STILL_LONGEST = 2400  # pixels: as big as the stage's own pictures (frames.PREVIEW)
THUMB_TALL = 48      # the timeline's pictures
THUMBS = 24


def available() -> bool:
    """Whether videos can play here: FFmpeg is installed and the player imports."""
    try:
        import puretech_player  # noqa: F401 - only whether it imports
    except ImportError:
        return False
    return bool(engines.find("ffmpeg"))


def _presenter_class():
    from puretech_player import Presenter

    class QtPresenter(Presenter):
        """Frames from the player's thread to the window's: the newest one only."""
        pixel_format = "bgra"

        def __init__(self, bridge: Playback):
            self.bridge = bridge
            self._size = (960, 540)
            self._lock = threading.Lock()
            self._latest = None
            self._waiting = False

        def set_size(self, w: int, h: int) -> None:
            with self._lock:
                self._size = (max(2, w), max(2, h))

        def size(self):
            with self._lock:
                return self._size

        def present(self, frame) -> None:
            n = frame.w * frame.h * 4
            image = QImage(frame.w, frame.h, QImage.Format_RGB32)  # its rows are w * 4 bytes, as the frame's are
            memoryview(image.bits())[:n] = memoryview(frame.buf)[:n]  # one copy (it was two: to bytes, then .copy())
            wall = frame.wall
            frame.release()
            with self._lock:
                self._latest = (image, wall)
                if self._waiting:
                    return
                self._waiting = True
            self.bridge._arrived.emit()

        def take(self):
            with self._lock:
                got, self._latest, self._waiting = self._latest, None, False
            return got

    return QtPresenter


class Playback(QObject):
    """One video, playing: frame(QImage, seconds) for each shown, state("playing", "paused", "ended"...).
    Every call returns at once."""
    frame = Signal(object, float)
    state = Signal(str)
    _arrived = Signal()
    _state = Signal(str)

    def __init__(self, path: Path, ffprobe: str | None = None):
        super().__init__()
        from puretech_player import Chunk, Player, Source
        ffmpeg = engines.find("ffmpeg")
        self.presenter = _presenter_class()(self)
        self.player = Player(Source([Chunk(str(path), 0.0, ffprobe=ffprobe or _ffprobe())]), ffmpeg=ffmpeg,
                             presenter=self.presenter, audio=SOUND)
        self.playing = False
        self.ended = False
        self._arrived.connect(self._show)
        self._state.connect(self._changed)
        self.player.on_state = lambda s, _info: self._state.emit(s)  # the player's thread: queued to the window's

    def _show(self) -> None:
        got = self.presenter.take()
        if got is not None:
            self.frame.emit(*got)

    def _changed(self, state: str) -> None:
        self.playing = state == "playing"
        self.ended = state == "ended"
        self.state.emit(state)

    def set_size(self, w: int, h: int) -> None:
        self.presenter.set_size(w, h)
        self.player.resize()

    def play(self) -> None:
        self.playing = True
        self.player.play()

    def pause(self) -> None:
        self.playing = False
        self.player.pause()

    def seek(self, seconds: float) -> None:
        self.player.seek(max(0.0, seconds))

    def step(self, frames: int) -> None:
        self.playing = False
        self.player.step(frames)

    def set_speed(self, speed: float) -> None:
        self.player.set_speed(speed)

    def close(self) -> None:
        self.player.on_state = None
        self.player.close()


def _ffprobe() -> str | None:
    from ..assurance import _ffprobe as find
    return find() or None


def still(path: Path, seconds: float, look: dict, size: tuple[int, int]):
    """The frame at seconds with the look (turned, straightened, hidden, adjusted, recoloured) made by FFmpeg
    with the conversion's own filters, no bigger than STILL_LONGEST: a Pillow picture. size: the frame's own
    width and height, as stored."""
    from PIL import Image

    from ..converters import media
    filters, w, h = media.look_filters(look, *size)
    scale = min(1.0, STILL_LONGEST / max(w, h))
    sw, sh = max(2, round(w * scale) // 2 * 2), max(2, round(h * scale) // 2 * 2)
    graph = media._graph(filters, "", f"scale={sw}:{sh}" if (sw, sh) != (w, h) else "")
    raw = subprocess.run([engines.require("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin",
                          "-protocol_whitelist", "file", "-ss", f"{max(0.0, seconds):.3f}", "-i", str(path),
                          "-frames:v", "1", "-vf", graph, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True, timeout=60, creationflags=engines.NO_WINDOW,
                         stdin=subprocess.DEVNULL).stdout
    if len(raw) < sw * sh * 3:
        raise OSError("FFmpeg made no frame there")
    return Image.frombytes("RGB", (sw, sh), raw[:sw * sh * 3])


def timeline_pictures(path: Path, duration: float, size: tuple[int, int], count: int = THUMBS) -> list:
    """count pictures spread along the video, THUMB_TALL pixels tall (Pillow pictures). A long video is read
    by its keyframes only, which is much quicker."""
    from PIL import Image
    w, h = size
    tw = max(2, round(THUMB_TALL * w / max(1, h)) // 2 * 2)
    rate = count / max(duration, 0.1)
    fast = ["-skip_frame", "nokey"] if duration > 60 else []
    raw = subprocess.run([engines.require("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin",
                          "-protocol_whitelist", "file", *fast, "-i", str(path), "-an",
                          "-vf", f"fps={rate:.6f},scale={tw}:{THUMB_TALL}", "-frames:v", str(count),
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True, timeout=300, creationflags=engines.NO_WINDOW,
                         stdin=subprocess.DEVNULL).stdout
    one = tw * THUMB_TALL * 3
    return [Image.frombytes("RGB", (tw, THUMB_TALL), raw[i:i + one]) for i in range(0, len(raw) - one + 1, one)]


def frame_name(path: Path, seconds: float) -> Path:
    """Where a saved frame goes: next to the video, named for when it is ("talk at 0-12.4.png"), never over
    a file already there."""
    from ..pipeline import unique_path
    from .timeline import clock
    return unique_path(path.with_name(f"{path.stem} at {clock(seconds).replace(':', '-')}.png"))


def save_frame(path: Path, seconds: float, settings: dict) -> Path:
    """The frame at seconds as a PNG next to the video, with every edit the conversion would make (turned,
    cropped, hidden, adjusted, drawn on, texts, the watermark), at the video's own size."""
    import tempfile

    from .. import options, registry
    from ..converters import media
    out = frame_name(path, seconds)
    with tempfile.TemporaryDirectory(prefix="latheport-frame-") as work:
        tokens = registry.running(options.clean(settings), [], [])
        try:
            filters, png, _ = media.picture_effects(media.probe(path), Path(work), sized_later=True)
        finally:
            registry.reset(tokens)
        graph = ["-vf", media._graph(filters, png, "")] if filters or png else []
        subprocess.run([engines.require("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin",
                        "-protocol_whitelist", "file", "-ss", f"{max(0.0, seconds):.3f}", "-i", str(path),
                        "-frames:v", "1", *graph, "-update", "1", str(out)],
                       capture_output=True, check=True, timeout=120, creationflags=engines.NO_WINDOW,
                       stdin=subprocess.DEVNULL)
    if not out.exists():
        raise OSError("FFmpeg made no frame there")
    return out


class FrameJob(QThread):
    """save_frame() off the window's thread: done(the file written, or None, and why not)."""
    done = Signal(object, str)

    def __init__(self, path: Path, seconds: float, settings: dict):
        super().__init__()
        self.path, self.seconds, self.settings = path, seconds, dict(settings)

    def run(self) -> None:
        from ..errors import ConversionError
        try:
            self.done.emit(save_frame(self.path, self.seconds, self.settings), "")
        except ConversionError as exc:
            self.done.emit(None, str(exc))
        except (OSError, subprocess.SubprocessError, ValueError):
            self.done.emit(None, "That frame couldn't be saved")


class StillJob(QThread):
    """still() off the window's thread: done(token, seconds, Pillow picture or None)."""
    done = Signal(int, float, object)

    def __init__(self, token: int, path: Path, seconds: float, look: dict, size: tuple[int, int]):
        super().__init__()
        self.token, self.path, self.seconds, self.look, self.size = token, path, seconds, dict(look), size

    def run(self) -> None:
        from ..errors import ConversionError
        try:
            got = still(self.path, self.seconds, self.look, self.size)
        except (ConversionError, OSError, subprocess.SubprocessError, ValueError):  # FFmpeg missing, or the look
            got = None
        self.done.emit(self.token, self.seconds, got)


class StripJob(QThread):
    """timeline_pictures() off the window's thread: got(path, [QImage])."""
    got = Signal(object, object)

    def __init__(self, path: Path, duration: float, size: tuple[int, int]):
        super().__init__()
        self.path, self.duration, self.size = path, duration, size

    def run(self) -> None:
        from ..errors import ConversionError
        from .frames import qimage
        try:
            pictures = [qimage(p) for p in timeline_pictures(self.path, self.duration, self.size)]
        except (ConversionError, OSError, subprocess.SubprocessError, ValueError):  # FFmpeg missing: no strip
            pictures = []
        self.got.emit(self.path, pictures)
