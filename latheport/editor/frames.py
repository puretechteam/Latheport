"""What the editor shows: a picture, or a video's frame, upright and at most PREVIEW pixels, with the
real picture's size; and the small pictures in the film strip."""

from __future__ import annotations

import io
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtGui import QImage

from ..formats import category

PREVIEW = 2400   # pixels: the longest side the stage gets (zoomed in 4x on a big screen it's still sharp)
THUMB = 96       # the film strip's pictures


@dataclass
class Frame:
    picture: object  # a Pillow image, RGBA, upright (as the file shows it), at most PREVIEW pixels
    width: int       # the real picture's size, upright
    height: int


def qimage(img) -> QImage:
    """A Pillow image as a QImage (RGBA, copied)."""
    img = img.convert("RGBA")
    return QImage(img.tobytes(), img.width, img.height, img.width * 4, QImage.Format_RGBA8888).copy()


def load_frame(path: Path, fmt: str, longest: int = PREVIEW) -> Frame | None:
    """The picture (or a video's frame a second in), upright; None if it can't be shown."""
    from PIL import Image
    try:
        img, size = _video_frame(path) if category(fmt) == "Video" else _picture(path, fmt, longest)
    except Exception:  # noqa: BLE001 - no picture is fine: the strip shows a blank
        return None
    img.thumbnail((longest, longest), Image.Resampling.LANCZOS)
    return Frame(img.convert("RGBA"), *size)


def size_of(path: Path, fmt: str) -> tuple[int, int] | None:
    """The picture's or video's size as shown (upright), read from its header where it can be."""
    try:
        if category(fmt) == "Video":
            from ..converters import media
            info = media.probe(path)
            return info["width"], info["height"]
        from PIL import Image
        if fmt in ("heic", "avif"):
            from ..converters.images import enable_heif
            enable_heif()
        with Image.open(path) as img:
            turned = img.getexif().get(0x0112, 1) in (5, 6, 7, 8)
            return (img.height, img.width) if turned else img.size
    except Exception:  # noqa: BLE001 - Pillow can't: decode it the long way
        frame = load_frame(path, fmt, THUMB)
        return (frame.width, frame.height) if frame else None


def thumbnail(path: Path, fmt: str) -> QImage | None:
    frame = load_frame(path, fmt, THUMB)
    return qimage(frame.picture) if frame else None


def _picture(path: Path, fmt: str, longest: int):
    from PIL import Image, ImageOps
    if fmt in ("heic", "avif"):
        from ..converters.images import enable_heif
        enable_heif()
    try:
        with Image.open(path) as img:
            img.draft("RGB", (longest, longest))  # a JPEG decodes smaller, much quicker
            up = (ImageOps.exif_transpose(img) or img).copy()
    except Exception:  # noqa: BLE001 - Pillow can't: pixport reads it
        import pixport
        up = pixport.read(path, fmt).to_pil()
        return up, up.size
    with Image.open(path) as img:  # the real size (draft decodes JPEGs smaller), upright
        full = img.size
        turned = img.getexif().get(0x0112, 1) in (5, 6, 7, 8)
    return up, (full[1], full[0]) if turned else full


def _video_frame(path: Path):
    from PIL import Image

    from .. import engines
    from ..converters import media
    info = media.probe(path)
    at = min(1.0, (info.get("duration") or 0) / 2)
    png = subprocess.run([engines.require("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin",
                          "-protocol_whitelist", "file", "-ss", f"{at:.3f}", "-i", str(path), "-frames:v", "1",
                          "-f", "image2pipe", "-c:v", "png", "-"], capture_output=True, check=True, timeout=60,
                         creationflags=engines.NO_WINDOW, stdin=subprocess.DEVNULL).stdout
    img = Image.open(io.BytesIO(png))
    img.load()
    return img, (info["width"], info["height"])
