"""PDF via PDFium (pypdfium2); XPS and SVG drawn with Qt; HTML and text laid out by bookport;
PostScript and EPS via Ghostscript."""

import shutil
from pathlib import Path

from PIL import Image

from .. import pdfium
from ..errors import ConversionError
from ..losses import loss
from ..registry import converter, option
from .images import QUALITY, prepare, save_image
from .images import WRITE as RASTER_OUT

RASTER_OUT = RASTER_OUT - {"pdf", "eps"}  # PDF -> EPS stays vector (Ghostscript)
PDF_DPI = 150


@converter(inputs={"xps", "oxps"}, outputs={"pdf"})
def xps_to_pdf(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
    from ..qtdraw import xps_to_pdf as draw
    draw(src, dst)


@converter(inputs={"svg"}, outputs={"pdf"}, terminal=True)  # picture-only PDFs: don't chain on
def svg_to_pdf(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
    from ..qtdraw import svg_bytes
    from ..qtdraw import svg_to_pdf as draw
    draw(svg_bytes(src), dst, src.name)


@converter(inputs={"ai"}, outputs={"pdf"})
def illustrator_to_pdf(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
    """Illustrator saves a full PDF inside .ai files unless "Create PDF compatible file" was off."""
    if not src.read_bytes()[:1024].lstrip().startswith(b"%PDF"):
        raise ConversionError("This .ai file was saved without PDF compatibility, so only Illustrator can open it")
    with pdfium.opened(src) as doc:
        doc.save(str(dst))  # written afresh: a clean PDF, not Illustrator's file renamed


def _render(page, fmt: str, zoom: float, alpha: bool) -> Image.Image:
    return prepare(pdfium.render(page, zoom, alpha), fmt)


def _save_page(page, path: Path, fmt: str) -> None:
    if fmt == "svg":  # vector stays vector; text becomes outlines so it looks the same everywhere
        path.write_text(pdfium.page_svg(page), encoding="utf-8")
    else:
        save_image(_render(page, fmt, int(option("dpi", PDF_DPI)) / 72, False), path, fmt)


def _page_notes(src: str, dst: str, opts: dict) -> list:
    if dst == "svg":
        return [loss("Each page becomes its own SVG, together in a folder", "pages", weight=0, affects=("pages",)),
                loss("Text becomes outlines: it can't be selected or searched", "text")]
    return [loss(f"Pages are drawn as pictures at {opts.get('dpi') or PDF_DPI} dpi", weight=0),
            loss("Each page becomes its own image, together in a folder", "pages", weight=0, affects=("pages",))]


@converter(inputs={"pdf"}, outputs=RASTER_OUT | {"svg"}, terminal=True, notes=_page_notes,
           options=lambda s, d: () if d == "svg" else ("dpi", "quality") if d in QUALITY else ("dpi",))
def pages_to_images(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> Path:
    """One page -> one image file. Several pages -> a folder of page images."""
    with pdfium.opened(src) as doc:
        if len(doc) == 1:
            _save_page(doc[0], dst, dst_fmt)
            return dst
        folder = dst.parent / f"{dst.stem} ({dst_fmt} pages)"
        folder.mkdir()
        width = len(str(len(doc)))
        for i in range(len(doc)):
            _save_page(doc[i], folder / f"page-{i + 1:0{width}}.{dst_fmt}", dst_fmt)
        return folder


@converter(inputs={"svg"}, outputs=RASTER_OUT)
def svg_to_image(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
    from ..qtdraw import svg_bytes
    from ..qtdraw import svg_to_image as draw
    save_image(prepare(draw(svg_bytes(src), name=src.name), dst_fmt), dst, dst_fmt)


@converter(inputs={"pdf"}, outputs={"txt"}, terminal=True)  # ebooks: bookport (ebooks.py)
def to_text(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
    with pdfium.opened(src) as doc:
        pages = [pdfium.text(doc[i]).replace("\r\n", "\n").strip() for i in range(len(doc))]
    dst.write_text("\n\n".join(pages), encoding="utf-8")


@converter(inputs={"html", "txt"}, outputs={"pdf"}, cost=2, options=("page_size", "text_size"),
           notes=lambda s, d, o: [loss("Laid out as book pages: scripts don't run and some page styles "
                                       "are simplified", weight=0)] if s == "html" else [])
def html_to_pdf(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
    """A web page or text file as a PDF, laid out by bookport (as its ebooks are)."""
    import bookport
    try:
        book = bookport.read(src, src_fmt)
        book.write(dst, "pdf", page_size=option("page_size"), text_size=option("text_size"))
    except bookport.BookportError as exc:
        raise ConversionError(str(exc)) from None


# ---- PostScript and EPS via Ghostscript (Pillow writes raster EPS on its own, see images.py)

GS_DEVICE = {"pdf": "pdfwrite", "ps": "ps2write", "eps": "eps2write"}


@converter(inputs={"eps", "ps"}, outputs={"pdf"}, engine="ghostscript")
@converter(inputs={"pdf"}, outputs={"ps", "eps"}, engine="ghostscript", terminal=True)
def ghostscript_convert(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
    from .. import engines
    # Ghostscript's argument parsing and non-ASCII paths don't mix: work on plain names
    local, out = dst.parent / f"input.{src_fmt}", dst.parent / f"output.{dst_fmt}"
    shutil.copyfile(src, local)
    args = [engines.require("ghostscript"), "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE",
            f"-sDEVICE={GS_DEVICE[dst_fmt]}"]
    if src_fmt == "eps":
        args.append("-dEPSCrop")  # the page is the drawing's bounding box, not a sheet of paper
    if dst_fmt == "eps":
        args += ["-dFirstPage=1", "-dLastPage=1"]  # EPS is one picture
    engines.run([*args, f"-sOutputFile={out.name}", local.name], timeout=600, cwd=str(dst.parent))
    if not out.exists() or not out.stat().st_size:
        raise ConversionError("Ghostscript produced nothing; the file may be damaged")
    out.replace(dst)
