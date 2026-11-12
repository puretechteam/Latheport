"""PDF via PDFium (pypdfium2); XPS and SVG drawn with Qt; HTML and text laid out by bookport;
PostScript and EPS read and written by bookport.ps."""

import shutil
from pathlib import Path

from PIL import Image

from .. import cancel, options, pdfium
from ..errors import ConversionError
from ..losses import loss
from ..registry import converter, expect, note, option
from .images import QUALITY, prepare, save_image
from .images import WRITE as RASTER_OUT

RASTER_OUT = RASTER_OUT - {"pdf", "eps"}  # PDF -> EPS stays vector (bookport.ps)
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
            cancel.check()  # a page at a time: a 1,000-page PDF stops within a page of Cancel
            _save_page(doc[i], folder / f"page-{i + 1:0{width}}.{dst_fmt}", dst_fmt)
        return folder


@converter(inputs={"svg"}, outputs=RASTER_OUT)
def svg_to_image(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
    from ..qtdraw import svg_bytes
    from ..qtdraw import svg_to_image as draw
    save_image(prepare(draw(svg_bytes(src), name=src.name), dst_fmt), dst, dst_fmt)


# ---- a PDF to a PDF: pages chosen, ordered and turned; split; made smaller (bookport's rewrite)

SQUEEZE = {"smaller": (150, 70), "balanced": (200, 85)}  # dpi of the largest page, JPEG quality


def _pdf_notes(src: str, dst: str, opts: dict) -> list:
    out = []
    if opts.get("pages"):
        n = len(options.parse_pages(opts["pages"]))
        out.append(loss(f"Pages chosen, put in order and turned by hand ({n} page{'s' if n != 1 else ''})", weight=0,
                        affects=("pages",)))
    if opts.get("split"):
        n = int(opts["split"])
        out.append(loss(f"Split into PDFs of {n} page{'s' if n != 1 else ''} each, together in a folder", "pages",
                        weight=0, affects=("pages",)))
    if opts.get("compress"):
        dpi = SQUEEZE[opts["compress"]][0]
        out.append(loss(f"Pictures in it saved again at {dpi} dpi: smaller, a little less sharp up close; text stays text",
                        weight=0))
    if opts.get("searchable"):
        out.append(loss("Scanned pages' text read by a smart tool (OCR) and laid over them unseen, so it can be searched "
                        "and copied: in Latin letters, and a word now and then can come out wrong", weight=0))
    if opts.get("pages") or opts.get("split") or opts.get("compress") or opts.get("searchable"):
        out.append(loss("Page labels and accessibility tags aren't kept (bookmarks, links, form fields and the "
                        "document's details are)", weight=0))
    else:
        out.append(loss("Saved as it is: set Pages, Split into, Compress or Make searchable to change it", weight=0))
    return out


OCR_DPI = 300      # scanned pages are read drawn at this resolution
OCR_PIXELS = 40e6  # but no bigger than this (a poster)
STAMP = 16         # characters: text this short over a full-page picture is a scanner app's mark, not the page's


def scanned(page) -> bool:
    """A page to read: it has no text of its own, or only a scanner app's stamp over a picture filling it.
    A page with real text stays as it is: its text would be there twice."""
    text = pdfium.text(page).strip()
    return not text or (len(text) < STAMP and pdfium.picture_share(page) > 0.8)


def text_layers(srcs: list[Path]) -> dict[int, list]:
    """What OCR reads on each page that has no text of its own: page index (across srcs, in order) ->
    lines of (word, x0, y0, x1, y1) in points on the page as it shows, from its bottom left."""
    from ..smart import need, ocr
    need("ocr")
    layers: dict[int, list] = {}
    index = done = had = 0
    for src in srcs:
        with pdfium.opened(src) as doc:
            for k in range(len(doc)):
                cancel.check()
                page = doc[k]
                if not scanned(page):
                    had += 1
                    index += 1
                    continue
                w, h = page.get_size()  # as it shows: PDFium turns it as the page says
                scale = min(OCR_DPI / 72, (OCR_PIXELS / max(w * h, 1.0)) ** 0.5)
                img = pdfium.render(page, scale)
                sx, sy = img.width / w, img.height / h
                lines = [[(wd.text, wd.box[0] / sx, h - wd.box[3] / sy, wd.box[2] / sx, h - wd.box[1] / sy)
                          for wd in line.words] for line in ocr.read(img)]
                lines = [line for line in lines if line]
                if lines:
                    layers[index] = lines
                done += 1
                index += 1
    if done:
        words = sum(len(line) for page in layers.values() for line in page)
        note(f"{done} scanned page{'s' if done != 1 else ''} read: {words:,} words found"
             + (f"; {had} page{'s' if had != 1 else ''} had text already" if had else ""))
    else:
        note("Every page has text already: nothing to read")
    return layers


def rewrite_pdfs(srcs: list[Path], dst: Path, plan: str | None, split, squeeze: str | None,
                 searchable: bool = False) -> Path:
    """srcs (joined, in order) as a new PDF at dst, with the pages plan says, made smaller as squeeze says, with
    scanned pages' text laid over them when searchable; split: a folder of PDFs of that many pages each. Returns
    what was written."""
    from bookport.errors import BookportError
    from bookport.pdf import rewrite
    dpi, quality = SQUEEZE[squeeze] if squeeze else (None, 75)
    pages = options.parse_pages(plan) if plan else None
    text = text_layers(srcs) if searchable else None
    try:
        pieces = rewrite.edit([p.read_bytes() for p in srcs], pages=pages, split=int(split) if split else None,
                              dpi=dpi, quality=quality, text=text)
    except BookportError as exc:
        if "needs a password" in str(exc):  # bookport files it under DRM; it's a password, said as PDFium's is
            who = srcs[0].name if len(srcs) == 1 else "One of the PDFs"
            raise ConversionError(f"{who} is password-protected; Latheport can't open it") from None
        raise ConversionError(str(exc)) from None
    if not split:
        dst.write_bytes(pieces[0])
        if pages is not None:
            expect("pages", len(pages), "Pages chosen by hand")
        return dst
    from bookport.pdf.parser import Document
    folder = dst.parent / f"{dst.stem} (split)"
    folder.mkdir()
    size = int(split)
    count = size * (len(pieces) - 1) + len(Document(pieces[-1]).pages())  # the last part may be shorter
    width = len(str(count))
    for k, data in enumerate(pieces):
        first = k * size + 1
        last = min(first + size - 1, count)
        (folder / (f"{dst.stem} {first:0{width}}.pdf" if size == 1 else
                   f"{dst.stem} {first:0{width}}-{last:0{width}}.pdf")).write_bytes(data)
    if pages is not None:
        expect("pages", count, "Pages chosen by hand")
    return folder


@converter(inputs={"pdf"}, outputs={"pdf"}, same=True, options=("pages", "split", "compress", "searchable"),
           notes=_pdf_notes)
def pdf_to_pdf(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> Path:
    plan, split, squeeze, searchable = option("pages"), option("split"), option("compress"), option("searchable")
    if not (plan or split or squeeze or searchable):
        shutil.copyfile(src, dst)
        return dst
    return rewrite_pdfs([src], dst, plan, split, squeeze, bool(searchable))


@converter(inputs={"pdf"}, outputs={"txt"}, terminal=True)  # ebooks: bookport (ebooks.py)
def to_text(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
    pages = []
    with pdfium.opened(src) as doc:
        for i in range(len(doc)):
            cancel.check()
            pages.append(pdfium.text(doc[i]).replace("\r\n", "\n").strip())
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


# ---- PostScript and EPS, read and written by bookport.ps (Pillow writes raster EPS on its own, see images.py)

def _flattener(src: Path):
    """bookport's flatten(index, dpi): a page drawn by PDFium, for the pages PostScript can't say."""
    def flatten(index: int, dpi: int):
        with pdfium.opened(src) as doc:
            img = pdfium.render(doc[index], dpi / 72)
        return img.width, img.height, img.tobytes()
    return flatten


@converter(inputs={"eps", "ps"}, outputs={"pdf"})
@converter(inputs={"pdf"}, outputs={"ps", "eps"}, terminal=True)
def postscript_convert(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
    import bookport
    from bookport import ps
    notes: list[str] = []
    try:
        if dst_fmt == "pdf":  # an EPS page is its drawing's bounding box, not a sheet of paper
            ps.to_pdf(src, dst, eps=True if src_fmt == "eps" else None, notes=notes)
        else:  # EPS is one picture: the first page
            ps.from_pdf(src, dst, eps=dst_fmt == "eps", flatten=_flattener(src), notes=notes)
    except bookport.BookportError as exc:
        raise ConversionError(str(exc)) from None
    for n in notes:  # a program that stopped early drew fewer pages
        note(n, ("pages",) if "stopped early" in n else ())
