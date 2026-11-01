"""PDFs through PDFium (Chrome's PDF engine; BSD and Apache licensed) and pypdfium2: opening,
drawing pages, their text, PDFs made from page pictures, and a page's shapes, text and pictures
as vectors (for SVG and DXF).

PDFium may only be used by one thread at a time, so everything here takes LOCK; the window's
thumbnails and a conversion can both want it."""

from __future__ import annotations

import base64
import contextlib
import ctypes
import io
import math
import threading
from collections.abc import Iterator
from pathlib import Path

from PIL import Image

from .errors import ConversionError

LOCK = threading.RLock()
# the most pixels a page is drawn with: an A4 or Letter page at the highest setting, 1200 dpi. A PDF
# page can be 200 inches square, which at 1200 dpi would be 23 GB to draw from a tiny file.
PAGE_PIXELS = 150_000_000

TEXT, PATH, IMAGE, SHADING, FORM = 1, 2, 3, 4, 5  # FPDF_PAGEOBJ_*
LINETO, BEZIERTO, MOVETO = 0, 1, 2  # FPDF_SEGMENT_*


@contextlib.contextmanager
def opened(src: Path | bytes, name: str | None = None) -> Iterator:
    """The pypdfium2 document for src (a path or the PDF's bytes), held under LOCK."""
    import pypdfium2 as pdfium
    label = name or (src.name if isinstance(src, Path) else "this PDF")
    with LOCK:
        try:
            doc = pdfium.PdfDocument(str(src) if isinstance(src, Path) else bytes(src))
        except pdfium.PdfiumError as exc:
            why = str(exc)
            if "password" in why.lower():
                raise ConversionError(f"{label} is password-protected; ConvertAll can't open it") from None
            raise ConversionError(f"Couldn't open {label}: it looks damaged ({why})") from None
        try:
            yield doc
        finally:
            doc.close()


def render(page, scale: float, alpha: bool = False) -> Image.Image:
    """The page drawn at scale (1 = 72 dpi), or smaller if that would be more than PAGE_PIXELS: RGB
    on white, or RGBA on transparent."""
    w, h = page.get_size()
    if w > 0 and h > 0:  # PDFium rounds the pixel size up: 160.0000001 would make 161
        cap = (PAGE_PIXELS / (w * h)) ** 0.5
        size = (int, cap) if scale > cap else (round, scale)  # rounded down when capped
        want = max(1, size[0](w * size[1])), max(1, size[0](h * size[1]))
        scale = min(want[0] / w, want[1] / h) * (1 - 1e-9)
    bitmap = page.render(scale=scale, fill_color=(255, 255, 255, 0) if alpha else (255, 255, 255, 255),
                         may_draw_forms=True)
    img = bitmap.to_pil()
    return img.convert("RGBA" if alpha else "RGB")


def text(page) -> str:
    textpage = page.get_textpage()
    try:
        return textpage.get_text_range()
    finally:
        textpage.close()


# ---- making a PDF of pictures (comics, folders of scans)

def images_to_pdf(pages: list[tuple[bytes, tuple[int, int]]], dst: Path, dpi: int) -> None:
    """One page per picture, sized for dpi. JPEGs go in as they are (no re-compression); other
    pictures are stored losslessly."""
    import pypdfium2 as pdfium
    scale = 72 / dpi
    with LOCK:
        doc = pdfium.PdfDocument.new()
        try:
            for data, (w, h) in pages:
                width, height = w * scale, h * scale
                page = doc.new_page(width, height)
                image = pdfium.PdfImage.new(doc)
                if data[:3] == b"\xff\xd8\xff":
                    image.load_jpeg(io.BytesIO(data), inline=True)
                else:
                    with Image.open(io.BytesIO(data)) as img:
                        bitmap = pdfium.PdfBitmap.from_pil(img.convert("RGBA" if "A" in img.getbands() else "RGB"))
                    image.set_bitmap(bitmap)
                    bitmap.close()
                image.set_matrix(pdfium.PdfMatrix().scale(width, height))
                page.insert_obj(image)
                page.gen_content()
                page.close()
            doc.save(str(dst))
        finally:
            doc.close()


def stored_picture(page) -> tuple[bytes, str] | None:
    """The page's picture as stored, when the page is nothing but one picture filling it (a
    scanned comic page): a JPEG's own bytes, or another picture losslessly as PNG. None otherwise."""
    from pypdfium2 import raw
    if page.get_rotation() or text(page).strip():
        return None
    objs = list(page.get_objects(max_depth=2))
    images = [o for o in objs if o.type == IMAGE]
    if len(images) != 1 or any(o.type in (PATH, TEXT, SHADING) for o in objs):
        return None
    img = images[0]
    x0, y0, x1, y1 = img.get_bounds()
    pw, ph = page.get_size()
    if (x1 - x0) * (y1 - y0) < pw * ph * 0.95:
        return None
    meta = img.get_metadata()
    if meta.colorspace not in (raw.FPDF_COLORSPACE_DEVICEGRAY, raw.FPDF_COLORSPACE_DEVICERGB):
        return None  # CMYK, indexed, ICC: drawn instead
    shown = img.get_bitmap(render=True).to_pil()  # with its transparency, as the page shows it
    if "A" in shown.getbands() and shown.getchannel("A").getextrema()[0] < 255:
        return None  # see-through: drawn on white instead, as a reader shows it
    if img.get_filters() == ["DCTDecode"]:
        return bytes(img.get_data(decode_simple=False)), "jpg"
    out = io.BytesIO()
    shown.convert("RGB" if meta.colorspace == raw.FPDF_COLORSPACE_DEVICERGB else "L").save(out, "PNG")
    return out.getvalue(), "png"


# ---- vectors: what a page draws, in page space (PDF points, y up)

def _matrix(obj) -> tuple[float, ...]:
    from pypdfium2 import raw
    m = raw.FS_MATRIX()
    if not raw.FPDFPageObj_GetMatrix(obj, m):
        return (1, 0, 0, 1, 0, 0)
    return (m.a, m.b, m.c, m.d, m.e, m.f)


def _mul(m, n):
    """m then n (row vectors, as PDF has it)."""
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return (a * a2 + b * c2, a * b2 + b * d2, c * a2 + d * c2, c * b2 + d * d2,
            e * a2 + f * c2 + e2, e * b2 + f * d2 + f2)


def _apply(m, x, y):
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


def _segments(path_handle, count_fn, get_fn) -> list[tuple[int, float, float, bool]]:
    from pypdfium2 import raw
    out = []
    x, y = ctypes.c_float(), ctypes.c_float()
    for i in range(count_fn(path_handle)):
        seg = get_fn(path_handle, i)
        if not seg or not raw.FPDFPathSegment_GetPoint(seg, x, y):
            continue
        out.append((raw.FPDFPathSegment_GetType(seg), x.value, y.value, bool(raw.FPDFPathSegment_GetClose(seg))))
    return out


def _runs(segments, m, curve_steps: int = 16) -> list[tuple[list[tuple[float, float]], bool]]:
    """Path segments as runs of points (curves flattened), each with whether it closes."""
    runs: list[tuple[list[tuple[float, float]], bool]] = []
    pts: list[tuple[float, float]] = []
    closed = False
    k = 0
    while k < len(segments):
        kind, x, y, close = segments[k]
        if kind == MOVETO:
            if len(pts) > 1:
                runs.append((pts, closed))
            pts, closed = [_apply(m, x, y)], False
        elif kind == LINETO:
            pts.append(_apply(m, x, y))
        elif kind == BEZIERTO and k + 2 < len(segments) and pts:
            p0 = pts[-1]
            p1, p2, p3 = (_apply(m, s[1], s[2]) for s in segments[k:k + 3])
            for i in range(1, curve_steps + 1):
                t = i / curve_steps
                a, b, c, d = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t ** 2, t ** 3
                pts.append((a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0], a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]))
            close = segments[k + 2][3]
            k += 2
        closed = closed or close
        k += 1
    if len(pts) > 1:
        runs.append((pts, closed))
    return runs


def _color(obj, getter) -> tuple[int, int, int, int] | None:
    r, g, b, a = (ctypes.c_uint() for _ in range(4))
    if not getter(obj, r, g, b, a):
        return None
    return (r.value, g.value, b.value, a.value)


def _objects(page_or_form, m, is_form: bool) -> Iterator[tuple[object, tuple]]:
    """(object handle, its matrix to page space) for everything drawn, inside forms too."""
    from pypdfium2 import raw
    count = raw.FPDFFormObj_CountObjects(page_or_form) if is_form else raw.FPDFPage_CountObjects(page_or_form)
    for i in range(max(count, 0)):
        obj = raw.FPDFFormObj_GetObject(page_or_form, i) if is_form else raw.FPDFPage_GetObject(page_or_form, i)
        if not obj:
            continue
        mo = _mul(_matrix(obj), m)
        if raw.FPDFPageObj_GetType(obj) == FORM:
            yield from _objects(obj, mo, True)
        else:
            yield obj, mo


def shapes(page) -> Iterator[dict]:
    """What the page draws, in page space (points, y up, the media box's corner at its origin):
    {"kind": "path", "runs", "fill", "stroke", "width"}, {"kind": "text", "text", "origin", "size"}
    and {"kind": "image", "matrix", "picture"}."""
    from pypdfium2 import raw
    x0, y0 = page.get_mediabox()[:2]
    base = (1, 0, 0, 1, -x0, -y0)
    textpage = page.get_textpage()
    try:
        for obj, m in _objects(page.raw, base, False):
            kind = raw.FPDFPageObj_GetType(obj)
            if kind == PATH:
                fillmode, stroke = ctypes.c_int(), ctypes.c_int()
                raw.FPDFPath_GetDrawMode(obj, fillmode, stroke)
                segs = _segments(obj, raw.FPDFPath_CountSegments, raw.FPDFPath_GetPathSegment)
                width = ctypes.c_float()
                raw.FPDFPageObj_GetStrokeWidth(obj, width)
                yield {"kind": "path", "runs": _runs(segs, m), "segments": segs, "matrix": m,
                       "fill": _color(obj, raw.FPDFPageObj_GetFillColor) if fillmode.value else None,
                       "evenodd": fillmode.value == 1,
                       "stroke": _color(obj, raw.FPDFPageObj_GetStrokeColor) if stroke.value else None,
                       "width": width.value * math.sqrt(abs(m[0] * m[3] - m[1] * m[2])) or 1.0}
            elif kind == TEXT:
                n = raw.FPDFTextObj_GetText(obj, textpage.raw, None, 0)
                buf = ctypes.create_string_buffer(max(n, 2))
                raw.FPDFTextObj_GetText(obj, textpage.raw, ctypes.cast(buf, ctypes.POINTER(ctypes.c_ushort)), n)
                words = buf.raw[:max(n - 2, 0)].decode("utf-16-le", "replace")
                size = ctypes.c_float()
                raw.FPDFTextObj_GetFontSize(obj, size)
                yield {"kind": "text", "text": words, "origin": (m[4], m[5]), "matrix": m, "handle": obj,
                       "size": size.value * math.sqrt(abs(m[0] * m[3] - m[1] * m[2])),
                       "fill": _color(obj, raw.FPDFPageObj_GetFillColor)}
            elif kind == IMAGE:
                yield {"kind": "image", "matrix": m, "handle": obj}
    finally:
        textpage.close()


# ---- a page as SVG: shapes as paths, text as outlines (looks the same everywhere), pictures inline

def _rgb(c) -> str:
    return f"#{c[0]:02x}{c[1]:02x}{c[2]:02x}"


def _svg_path(segments, m, to_svg) -> str:
    out = []
    k = 0
    while k < len(segments):
        kind, x, y, close = segments[k]
        px, py = to_svg(*_apply(m, x, y))
        if kind == MOVETO:
            out.append(f"M{px:.2f} {py:.2f}")
        elif kind == LINETO:
            out.append(f"L{px:.2f} {py:.2f}")
        elif kind == BEZIERTO and k + 2 < len(segments):
            (ax, ay), (bx, by), (cx, cy) = (to_svg(*_apply(m, s[1], s[2])) for s in segments[k:k + 3])
            out.append(f"C{ax:.2f} {ay:.2f} {bx:.2f} {by:.2f} {cx:.2f} {cy:.2f}")
            close = segments[k + 2][3]
            k += 2
        if close:
            out.append("Z")
        k += 1
    return "".join(out)


def _glyph_outlines(page, to_svg) -> list[str]:
    """Each character on the page as its glyph outline, placed where it is drawn."""
    from pypdfium2 import raw
    out = []
    textpage = page.get_textpage()
    try:
        x0, y0 = page.get_mediabox()[:2]
        for i in range(raw.FPDFText_CountChars(textpage.raw)):
            code = raw.FPDFText_GetUnicode(textpage.raw, i)
            if code in (0, 0x20, 0x0A, 0x0D, 0xFFFE, 0xFFFF):
                continue
            obj = raw.FPDFText_GetTextObject(textpage.raw, i)
            font = obj and raw.FPDFTextObj_GetFont(obj)
            if not font:
                continue
            size = raw.FPDFText_GetFontSize(textpage.raw, i)
            glyph = raw.FPDFFont_GetGlyphPath(font, code, ctypes.c_float(size or 1))
            if not glyph:
                continue
            fm = raw.FS_MATRIX()
            raw.FPDFText_GetMatrix(textpage.raw, i, fm)
            ox, oy = ctypes.c_double(), ctypes.c_double()
            raw.FPDFText_GetCharOrigin(textpage.raw, i, ox, oy)
            # the outline comes in em units: the font size, then the text's matrix, place it
            s = size or 1.0
            m = (fm.a * s, fm.b * s, fm.c * s, fm.d * s, ox.value - x0, oy.value - y0)
            segs = _segments(glyph, raw.FPDFGlyphPath_CountGlyphSegments, raw.FPDFGlyphPath_GetGlyphPathSegment)
            if not segs:
                continue
            fill = _color(obj, raw.FPDFPageObj_GetFillColor) or (0, 0, 0, 255)
            out.append(f'<path d="{_svg_path(segs, m, to_svg)}" fill="{_rgb(fill)}"/>')
    finally:
        textpage.close()
    return out


def page_svg(page) -> str:
    import pypdfium2 as pdfium
    width, height = page.get_size()
    mx0, my0, _, _ = page.get_mediabox()
    cx0, cy0, cx1, cy1 = page.get_cropbox()
    left, top = cx0 - mx0, cy1 - my0

    def to_svg(x, y):
        return x - left, top - y

    parts = []
    for s in shapes(page):
        if s["kind"] == "path" and (s["fill"] or s["stroke"]):
            d = _svg_path(s["segments"], s["matrix"], to_svg)
            if not d:
                continue
            fill = f'fill="{_rgb(s["fill"])}"' + (' fill-rule="evenodd"' if s["evenodd"] else "") if s["fill"] else 'fill="none"'
            if s["fill"] and s["fill"][3] < 255:
                fill += f' fill-opacity="{s["fill"][3] / 255:.3f}"'
            stroke = (f'stroke="{_rgb(s["stroke"])}" stroke-width="{s["width"]:.2f}"' if s["stroke"] else "")
            parts.append(f'<path d="{d}" {fill} {stroke}/>')
        elif s["kind"] == "image":
            try:
                img = pdfium.PdfImage(s["handle"], page=page, pdf=page.pdf).get_bitmap(render=True).to_pil()
            except Exception:  # noqa: BLE001, S112 - a picture PDFium can't decode is left out
                continue
            buf = io.BytesIO()
            img.save(buf, "PNG")
            a, b, c, d, e, f = s["matrix"]
            ex, fy = to_svg(c + e, d + f)
            parts.append(f'<image width="1" height="1" preserveAspectRatio="none" '
                         f'transform="matrix({a:.4f} {-b:.4f} {-c:.4f} {d:.4f} {ex:.4f} {fy:.4f})" '
                         f'href="data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}"/>')
    parts += _glyph_outlines(page, to_svg)
    w, h = cx1 - cx0, cy1 - cy0
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.2f}pt" height="{h:.2f}pt" '
            f'viewBox="0 0 {w:.2f} {h:.2f}">\n<rect width="100%" height="100%" fill="white"/>\n'
            + "\n".join(parts) + "\n</svg>\n") if width and height else ""
