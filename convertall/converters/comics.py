"""Comics (CBZ/CBR/CB7/CBT, i.e. archives of page images) <-> PDF, and folders of images -> PDF."""

import io
import re
from pathlib import Path

from PIL import Image, ImageOps

from ..detect import COMICS
from ..errors import ConversionError
from ..losses import loss
from ..registry import converter, note, option
from .archives import compress, extract_to
from .images import READ as PIL_READ
from .images import enable_heif, prepare
from .pdf import PDF_DPI

PAGE_EXT = {f".{e}" for e in PIL_READ} | {".jpeg", ".jpe", ".jfif", ".tif", ".heif"}
WRITABLE = COMICS  # CBR too: stored RAR via packport (or WinRAR when installed)


def _natural(p: Path) -> list:
    """'page2' before 'page10'."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", p.as_posix().lower())]


def _pages(root: Path) -> list[Path]:
    hidden = lambda p: any(part.startswith((".", "__MACOSX")) for part in p.relative_to(root).parts)
    pages = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in PAGE_EXT and not hidden(p)]
    return sorted(pages, key=lambda p: _natural(p.relative_to(root)))


def _page_bytes(path: Path) -> tuple[bytes, tuple[int, int]]:
    """JPEG and PNG pages go into the PDF untouched; anything else is re-encoded once."""
    if path.suffix.lower() in (".heic", ".heif"):
        enable_heif()
    with Image.open(path) as img:
        upright = img.getexif().get(0x0112, 1) in (0, 1)
        if img.format in ("JPEG", "PNG") and upright and img.mode in ("RGB", "L", "RGBA", "LA", "P", "1"):
            return path.read_bytes(), img.size
        img = ImageOps.exif_transpose(img) or img
        # lossy sources (WebP, AVIF, HEIC) stay JPEG; lossless ones (GIF, BMP, TIFF) become PNG
        fmt = "jpg" if img.format in ("WEBP", "AVIF", "HEIF", "JPEG", "MPO") else "png"
        out = io.BytesIO()
        prepare(img, fmt).save(out, "JPEG" if fmt == "jpg" else "PNG", quality=95)
        return out.getvalue(), img.size


def images_to_pdf(pages: list[Path], dst: Path) -> None:
    from .. import pdfium
    data = []
    for path in pages:
        try:
            data.append(_page_bytes(path))
        except Exception as exc:
            raise ConversionError(f"Couldn't read page {path.name}: {exc}") from exc
    pdfium.images_to_pdf(data, dst, PDF_DPI)


@converter(inputs=COMICS, outputs={"pdf"}, terminal=True)
def comic_to_pdf(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
    work = dst.parent / "_pages"
    found: list[str] = []
    extract_to(src, work, src_fmt, found)
    for line in found:
        note(line)
    pages = _pages(work)
    if not pages:
        raise ConversionError("No page images found in this comic")
    images_to_pdf(pages, dst)


@converter(inputs={"folder"}, outputs={"pdf"}, terminal=True)
def folder_to_pdf(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
    """Every image in the folder (and subfolders), in name order, one per page."""
    pages = _pages(src)
    if not pages:
        raise ConversionError("No images found in this folder")
    images_to_pdf(pages, dst)


@converter(inputs={"pdf"}, outputs=WRITABLE, terminal=True, options=("dpi",),
           notes=lambda s, d, o: [loss(f"Pages are drawn as pictures at {o.get('dpi') or PDF_DPI} dpi (a page that "
                                       "is one picture is kept as it is)", weight=0)])
def pdf_to_comic(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
    """A page that is nothing but one JPEG or PNG (a scanned comic's are) goes in as stored, with
    nothing re-compressed; other pages are drawn."""
    from .. import pdfium
    work = dst.parent / "_pages"
    work.mkdir()
    zoom = int(option("dpi", PDF_DPI)) / 72
    with pdfium.opened(src) as doc:
        width = max(3, len(str(len(doc))))
        for i in range(len(doc)):
            page = doc[i]
            stored = pdfium.stored_picture(page)
            if stored:
                data, ext = stored
                (work / f"{i + 1:0{width}}.{ext}").write_bytes(data)
            else:
                pdfium.render(page, zoom).save(work / f"{i + 1:0{width}}.jpg", "JPEG", quality=92)
    compress(work, dst, dst_fmt)


@converter(inputs={"folder"}, outputs=WRITABLE, terminal=True)
def folder_to_comic(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
    if not _pages(src):
        raise ConversionError("No images found in this folder")
    compress(src, dst, dst_fmt)
