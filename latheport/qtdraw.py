"""Drawing with Qt (already in Latheport for its window): SVG pictures and XPS documents into PDFs
and images. Qt's PDF writer keeps shapes as shapes and text as text, with the fonts embedded.

Qt needs its application object for fonts; the window has one, and the terminal tool gets one here
(no window is shown)."""

from __future__ import annotations

import gzip
import math
import os
import re
import sys
import threading
import zipfile
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET

from PIL import Image

from .errors import ConversionError
from .safety import read_capped

PX_TO_PT = 0.75  # SVG and XPS measure in 1/96 inch; PDF pages in 1/72
_APP: list = []  # the application made here lives as long as the process: Qt can't make a second one


def app():
    """The Qt application, made if there's none yet (the terminal tool). A full QApplication, not
    just a QGuiApplication: a window opened later in the same process needs one."""
    from PySide6.QtWidgets import QApplication
    existing = QApplication.instance()
    if existing is not None:
        return existing
    if threading.current_thread() is not threading.main_thread():
        raise ConversionError("Drawing this needs Latheport's main thread")
    if sys.platform != "win32" and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    _APP.append(QApplication(["latheport"]))  # one made and dropped leaves the next with no fonts
    return _APP[0]


def _qimage_to_pil(img) -> Image.Image:
    from PySide6.QtGui import QImage
    img = img.convertToFormat(QImage.Format_RGBA8888)
    return Image.frombuffer("RGBA", (img.width(), img.height()), bytes(img.constBits()), "raw", "RGBA",
                            img.bytesPerLine(), 1).copy()


def _pdf_writer(dst: Path, width_pt: float, height_pt: float):
    from PySide6.QtCore import QMarginsF, QSizeF
    from PySide6.QtGui import QPageLayout, QPageSize, QPdfWriter
    writer = QPdfWriter(str(dst))
    writer.setResolution(96)  # one unit is one SVG/XPS pixel
    writer.setCreator("Latheport")
    size = QPageSize(QSizeF(max(width_pt, 1), max(height_pt, 1)), QPageSize.Unit.Point, "", QPageSize.SizeMatchPolicy.ExactMatch)
    writer.setPageLayout(QPageLayout(size, QPageLayout.Orientation.Portrait, QMarginsF(0, 0, 0, 0)))
    return writer


# ---- SVG

def svg_bytes(src: Path) -> bytes:
    """The SVG's text, unpacked first if it's an .svgz."""
    with src.open("rb") as f:
        zipped = f.read(2) == b"\x1f\x8b"
    if not zipped:
        return src.read_bytes()
    try:
        with gzip.open(src) as f:
            return read_capped(f, src.name)
    except (OSError, EOFError) as exc:
        raise ConversionError(f"{src.name} is a damaged .svgz: {exc}") from None


SVG_NS, XLINK_NS = "http://www.w3.org/2000/svg", "http://www.w3.org/1999/xlink"
_IMPORT = re.compile(r"@import\b[^;{]*;?", re.IGNORECASE)


def safe_svg(data: bytes, name: str = "the SVG") -> bytes:
    """The SVG with nothing in it that reaches outside it. Qt's SVG renderer loads an <image> by
    file path (absolute, or relative to the working folder), so a downloaded SVG could copy any
    picture on the computer into its result. Python's XML parser reads it (internal entities
    expanded, external ones refused), every link that isn't to something inside it (#id) or
    carried in it (data:) is dropped, as are CSS @import rules, and Qt gets the result: no
    entities, no outside references."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ConversionError(f"Couldn't read {name}: it isn't a well-formed SVG ({exc})") from None
    for el in root.iter():
        for attr in [a for a in el.attrib if _local(a) == "href"]:
            value = el.attrib[attr].strip()
            if not (value.startswith("#") or value[:5].lower() == "data:"):
                del el.attrib[attr]
        if _local(el.tag) == "style" and el.text:
            el.text = _IMPORT.sub("", el.text)
    ET.register_namespace("", SVG_NS)
    ET.register_namespace("xlink", XLINK_NS)
    return ET.tostring(root, encoding="utf-8")


def _svg(data: bytes, name: str):
    from PySide6.QtCore import QByteArray
    from PySide6.QtSvg import QSvgRenderer
    app()
    data = safe_svg(data, name)
    r = QSvgRenderer(QByteArray(data))
    if not r.isValid():
        raise ConversionError(f"Couldn't read {name}: it isn't an SVG Qt can draw (it may be damaged)")
    box = r.viewBoxF()
    size = r.defaultSize()
    w, h = (size.width(), size.height()) if size.width() > 0 and size.height() > 0 else (box.width(), box.height())
    if w <= 0 or h <= 0:
        raise ConversionError(f"{name} has no size to draw at")
    return r, w, h


_UNIT_PT = {"pt": 1.0, "px": PX_TO_PT, "": PX_TO_PT, "mm": 72 / 25.4, "cm": 72 / 2.54, "in": 72.0, "pc": 12.0}


def _size_pt(data: bytes, w: float, h: float) -> tuple[float, float]:
    """The SVG's page size in points: its width and height as written (in mm, in, pt...) when they
    have units, else its pixels at 96 dpi."""
    head = data[:4096].decode("utf-8", "replace")
    tag = re.search(r"<svg\b[^>]*>", head, re.DOTALL)
    out = []
    for attr, px in (("width", w), ("height", h)):
        m = tag and re.search(r"\b" + attr + r"\s*=\s*[\"']\s*([0-9.]+)\s*(pt|px|mm|cm|in|pc)?\s*[\"']", tag.group(0))
        out.append(float(m.group(1)) * _UNIT_PT[m.group(2) or ""] if m else px * PX_TO_PT)
    return out[0], out[1]


def svg_to_pdf(data: bytes, dst: Path, name: str = "the SVG") -> None:
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QPainter
    r, w, h = _svg(data, name)
    wpt, hpt = _size_pt(data, w, h)
    writer = _pdf_writer(dst, wpt, hpt)
    painter = QPainter(writer)
    try:
        r.render(painter, QRectF(0, 0, wpt / PX_TO_PT, hpt / PX_TO_PT))
    finally:
        painter.end()


def svg_to_image(data: bytes, zoom: float | None = None, name: str = "the SVG", dpi: float | None = None,
                 background=None) -> Image.Image:
    """The SVG drawn on a transparent background (or background, a colour name); zoom None: small
    icons drawn at 1024 pixels on their long side, big drawings at their own size; dpi: drawn at
    its physical size (a DXF drawing's millimetres) at that many dots per inch."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QImage, QPainter
    r, w, h = _svg(data, name)
    if dpi:
        wpt, hpt = _size_pt(data, w, h)
        pw, ph = max(1, round(wpt / 72 * dpi)), max(1, round(hpt / 72 * dpi))
    else:
        zoom = zoom or max(1.0, 1024 / max(w, h))
        pw, ph = max(1, round(w * zoom)), max(1, round(h * zoom))
    if pw * ph > 200_000_000:
        raise ConversionError(f"{name} is too big to draw as a picture")
    img = QImage(pw, ph, QImage.Format_ARGB32_Premultiplied)
    img.fill(QColor(background) if background else Qt.transparent)
    painter = QPainter(img)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setRenderHint(QPainter.SmoothPixmapTransform)
    try:
        r.render(painter, QRectF(0, 0, pw, ph))
    finally:
        painter.end()
    return _qimage_to_pil(img)


# ---- XPS and OpenXPS (ECMA-388): fixed pages of paths, glyphs in embedded fonts, and pictures

_NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


class Xps:
    """An XPS document: its pages, in order, each drawn with a QPainter."""

    def __init__(self, src: Path):
        try:
            self.zip = zipfile.ZipFile(src)
        except (zipfile.BadZipFile, OSError) as exc:
            raise ConversionError(f"Couldn't open {src.name}: it isn't an XPS document ({exc})") from None
        self.name = src.name
        self.names = {n.lower(): n for n in self.zip.namelist()}
        self.fonts: dict[str, object] = {}
        self.families: dict[str, str | None] = {}  # a font's family, registered with Qt for the text
        self.registered: list[int] = []
        self.images: dict[str, object] = {}
        try:
            self.pages = self._pages()
        except (KeyError, ET.ParseError, ValueError) as exc:
            raise ConversionError(f"{src.name} is a damaged XPS document ({exc})") from None
        if not self.pages:
            raise ConversionError(f"{src.name} has no pages")

    def close(self) -> None:
        from PySide6.QtGui import QFontDatabase
        for fid in self.registered:
            QFontDatabase.removeApplicationFont(fid)
        self.registered.clear()
        self.zip.close()

    def part(self, name: str, base: str = "/") -> bytes:
        full = self._resolve(name, base)
        key = self.names.get(full.lstrip("/").lower())
        if key is None:
            raise KeyError(f"{full} isn't in the document")
        info = self.zip.getinfo(key)
        if info.file_size > 256 << 20:
            raise ValueError(f"{full} is too big")
        return self.zip.read(key)

    @staticmethod
    def _resolve(name: str, base: str) -> str:
        name = name.split("#", 1)[0]
        if name.startswith("/"):
            return name
        parts = []
        for p in (PurePosixPath(base).parent / name).parts:
            if p == "..":
                if parts:
                    parts.pop()
            elif p not in (".", "/"):
                parts.append(p)
        return "/" + "/".join(parts)

    def _xml(self, name: str, base: str = "/") -> ET.Element:
        return ET.fromstring(self.part(name, base))

    def _pages(self) -> list[tuple[str, float, float]]:
        rels = self._xml("/_rels/.rels")
        seq = next((r.get("Target") for r in rels if (r.get("Type") or "").endswith("/fixedrepresentation")), None)
        if not seq:
            raise ValueError("no fixed document sequence")
        seq_path = self._resolve(seq, "/")
        pages = []
        for ref in self._xml(seq_path):
            if _local(ref.tag) != "DocumentReference":
                continue
            doc_path = self._resolve(ref.get("Source", ""), seq_path)
            for pc in self._xml(doc_path):
                if _local(pc.tag) == "PageContent":
                    page_path = self._resolve(pc.get("Source", ""), doc_path)
                    pages.append((page_path, float(pc.get("Width") or 0), float(pc.get("Height") or 0)))
        return pages

    def page_size(self, k: int) -> tuple[float, float, ET.Element]:
        path, w, h = self.pages[k]
        root = self._xml(path)
        return float(root.get("Width") or w or 816), float(root.get("Height") or h or 1056), root

    def draw(self, k: int, painter, root: ET.Element | None = None) -> None:
        """Page k onto painter; root: its XML when page_size has read it already (each page is read once)."""
        path = self.pages[k][0]
        if root is None:
            _w, _h, root = self.page_size(k)
        _Page(self, path, painter).element(root, [])

    # -- resources

    def font(self, uri: str, base: str):
        from PySide6.QtCore import QByteArray
        from PySide6.QtGui import QRawFont
        full = self._resolve(uri, base)
        if full not in self.fonts:
            data = bytearray(self.part(full))
            stem = PurePosixPath(full).stem
            if PurePosixPath(full).suffix.lower() == ".odttf" or re.fullmatch(r"[0-9A-Fa-f-]{36}", stem):
                _deobfuscate(data, stem)
            font = QRawFont(QByteArray(bytes(data)), 16)
            self.fonts[full] = font if font.isValid() else None
            self.families[full] = self._register(bytes(data))
        return self.fonts[full]

    def family(self, uri: str, base: str) -> str | None:
        """The family name the document's font is registered under with Qt (for its text)."""
        full = self._resolve(uri, base)
        self.font(uri, base)
        return self.families.get(full)

    def _register(self, data: bytes) -> str | None:
        from PySide6.QtCore import QByteArray
        from PySide6.QtGui import QFontDatabase
        fid = QFontDatabase.addApplicationFontFromData(QByteArray(data))
        if fid < 0:
            return None
        self.registered.append(fid)
        names = QFontDatabase.applicationFontFamilies(fid)
        return names[0] if names else None

    def image(self, uri: str, base: str):
        from PySide6.QtGui import QImage
        full = self._resolve(uri.strip("{}").split("}")[0], base)
        if full not in self.images:
            img = QImage()
            data = self.part(full)
            if not img.loadFromData(data):
                import io
                try:
                    with Image.open(io.BytesIO(data)) as pil:
                        rgba = pil.convert("RGBA")
                    img = QImage(rgba.tobytes(), rgba.width, rgba.height, rgba.width * 4, QImage.Format_RGBA8888).copy()
                except Exception:  # noqa: BLE001 - a picture nothing here can decode (JPEG XR) is left out
                    img = None
            self.images[full] = img
        return self.images[full]


def _deobfuscate(data: bytearray, guid: str) -> None:
    """An obfuscated font (.odttf): its first 32 bytes XORed with its part name's GUID (ECMA-388)."""
    digits = guid.replace("-", "")
    if len(digits) != 32:
        return
    g = guid if len(guid) == 36 else f"{digits[:8]}-{digits[8:12]}-{digits[12:16]}-{digits[16:20]}-{digits[20:]}"
    order = (6, 4, 2, 0, 11, 9, 16, 14, 19, 21, 24, 26, 28, 30, 32, 34)
    key = [int(g[i:i + 2], 16) for i in order]
    for i in range(min(32, len(data))):
        data[i] ^= key[15 - (i % 16)]


def _color(text: str):
    from PySide6.QtGui import QColor
    t = (text or "").strip()
    if t.startswith("#"):
        h = t[1:]
        if len(h) == 6:
            h = "ff" + h
        if len(h) == 8:
            v = int(h, 16)
            return QColor((v >> 16) & 255, (v >> 8) & 255, v & 255, (v >> 24) & 255)
    if t.startswith("sc#"):
        n = [float(x) for x in _NUM.findall(t[3:])]
        if len(n) == 3:
            n = [1.0, *n]
        if len(n) == 4:
            a, r, g, b = (max(0.0, min(1.0, x)) for x in n)
            srgb = lambda c: c * 12.92 if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055
            return QColor.fromRgbF(srgb(r), srgb(g), srgb(b), a)
    if t.startswith("ContextColor"):
        n = [float(x) for x in _NUM.findall(t.split(" ", 2)[-1])]
        if len(n) >= 4:  # alpha then the profile's channels: taken as RGB (or gray) where they fit
            a, rest = n[0], n[1:]
            r, g, b = (rest + rest * 2)[:3] if len(rest) < 3 else rest[:3]
            return QColor.fromRgbF(*(max(0.0, min(1.0, x)) for x in (r, g, b, a)))
    return QColor(0, 0, 0)


def _matrix(text: str):
    from PySide6.QtGui import QTransform
    n = [float(x) for x in _NUM.findall(text or "")]
    if len(n) != 6:
        return QTransform()
    return QTransform(n[0], n[1], n[2], n[3], n[4], n[5])


def _geometry(text: str):
    """XPS's abbreviated geometry syntax (much like SVG path data) as a QPainterPath."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QPainterPath
    path = QPainterPath()
    tokens = re.findall(r"[A-Za-z]|" + _NUM.pattern, text or "")
    i, cmd = 0, ""
    x = y = 0.0
    last_ctrl = None

    def nums(k):
        nonlocal i
        vals = []
        while len(vals) < k and i < len(tokens) and not tokens[i].isalpha():
            vals.append(float(tokens[i]))
            i += 1
        if len(vals) < k:
            raise ValueError("geometry ends part-way")
        return vals

    while i < len(tokens):
        if tokens[i].isalpha():
            cmd = tokens[i]
            i += 1
            if cmd in "Zz":
                path.closeSubpath()
                continue
            if cmd == "F":
                path.setFillRule(Qt.WindingFill if nums(1)[0] else Qt.OddEvenFill)
                continue
        elif not cmd:
            raise ValueError("geometry without a command")
        rel = cmd.islower()
        c = cmd.upper()
        ox, oy = (x, y) if rel else (0.0, 0.0)
        if c == "M":
            x, y = (v + o for v, o in zip(nums(2), (ox, oy)))
            path.moveTo(x, y)
            cmd = "l" if rel else "L"
            last_ctrl = None
        elif c == "L":
            x, y = (v + o for v, o in zip(nums(2), (ox, oy)))
            path.lineTo(x, y)
            last_ctrl = None
        elif c == "H":
            x = nums(1)[0] + ox
            path.lineTo(x, y)
        elif c == "V":
            y = nums(1)[0] + oy
            path.lineTo(x, y)
        elif c == "C":
            a = nums(6)
            path.cubicTo(a[0] + ox, a[1] + oy, a[2] + ox, a[3] + oy, a[4] + ox, a[5] + oy)
            last_ctrl = (a[2] + ox, a[3] + oy)
            x, y = a[4] + ox, a[5] + oy
        elif c == "S":
            a = nums(4)
            c1 = (2 * x - last_ctrl[0], 2 * y - last_ctrl[1]) if last_ctrl else (x, y)
            path.cubicTo(c1[0], c1[1], a[0] + ox, a[1] + oy, a[2] + ox, a[3] + oy)
            last_ctrl = (a[0] + ox, a[1] + oy)
            x, y = a[2] + ox, a[3] + oy
        elif c == "Q":
            a = nums(4)
            path.quadTo(a[0] + ox, a[1] + oy, a[2] + ox, a[3] + oy)
            x, y = a[2] + ox, a[3] + oy
        elif c == "A":
            rx, ry, rot, large, sweep, ex, ey = nums(7)
            _arc(path, x, y, rx, ry, rot, bool(large), bool(sweep), ex + ox, ey + oy)
            x, y = ex + ox, ey + oy
        else:
            raise ValueError(f"geometry command {cmd}")
    return path


def _arc(path, x1, y1, rx, ry, phi, large, sweep, x2, y2) -> None:
    """An elliptical arc from its end points (SVG's parametrisation), as cubic curves."""
    if rx == 0 or ry == 0 or (x1 == x2 and y1 == y2):
        path.lineTo(x2, y2)
        return
    rx, ry = abs(rx), abs(ry)
    p = math.radians(phi)
    cp, sp = math.cos(p), math.sin(p)
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    x1p, y1p = cp * dx + sp * dy, -sp * dx + cp * dy
    lam = x1p ** 2 / rx ** 2 + y1p ** 2 / ry ** 2
    if lam > 1:
        rx, ry = rx * math.sqrt(lam), ry * math.sqrt(lam)
    num = rx ** 2 * ry ** 2 - rx ** 2 * y1p ** 2 - ry ** 2 * x1p ** 2
    den = rx ** 2 * y1p ** 2 + ry ** 2 * x1p ** 2
    co = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large == sweep:
        co = -co
    cxp, cyp = co * rx * y1p / ry, -co * ry * x1p / rx
    cx, cy = cp * cxp - sp * cyp + (x1 + x2) / 2, sp * cxp + cp * cyp + (y1 + y2) / 2

    def angle(ux, uy, vx, vy):
        return math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)

    t1 = angle(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dt = angle((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and dt > 0:
        dt -= 2 * math.pi
    elif sweep and dt < 0:
        dt += 2 * math.pi
    n = max(1, math.ceil(abs(dt) / (math.pi / 2)))
    step = dt / n
    k = 4 / 3 * math.tan(step / 4)
    for j in range(n):
        a1, a2 = t1 + j * step, t1 + (j + 1) * step
        e1 = (math.cos(a1) - k * math.sin(a1), math.sin(a1) + k * math.cos(a1))
        e2 = (math.cos(a2) + k * math.sin(a2), math.sin(a2) - k * math.cos(a2))
        e3 = (math.cos(a2), math.sin(a2))
        pts = [(cx + rx * ex * cp - ry * ey * sp, cy + rx * ex * sp + ry * ey * cp) for ex, ey in (e1, e2, e3)]
        path.cubicTo(*pts[0], *pts[1], *pts[2])


class _Page:
    """Draws one FixedPage's elements."""

    def __init__(self, doc: Xps, path: str, painter):
        self.doc, self.path, self.p = doc, path, painter

    # resources: {"x:Key": element}, innermost dictionary first
    def _resources(self, el: ET.Element, scopes: list) -> list:
        for child in el:
            if _local(child.tag).endswith(".Resources"):
                for d in child:
                    if _local(d.tag) == "ResourceDictionary":
                        entries = {}
                        src = d.get("Source")
                        if src:
                            try:
                                remote = self.doc._xml(src, self.path)
                                entries.update(self._keyed(remote))
                            except (KeyError, ET.ParseError, ValueError):
                                pass
                        entries.update(self._keyed(d))
                        scopes = [entries, *scopes]
        return scopes

    @staticmethod
    def _keyed(d: ET.Element) -> dict:
        out = {}
        for e in d:
            key = next((v for k, v in e.attrib.items() if _local(k) == "Key"), None)
            if key:
                out[key] = e
        return out

    @staticmethod
    def _lookup(value: str | None, scopes: list):
        if value and value.startswith("{StaticResource"):
            key = value[len("{StaticResource"):].strip(" }")
            for s in scopes:
                if key in s:
                    return s[key]
            return None
        return value

    def _prop(self, el: ET.Element, name: str, scopes: list):
        """An attribute (resolved if it names a resource) or a property element's first child."""
        value = el.get(name)
        if value is not None:
            return self._lookup(value, scopes)
        tag = f"{_local(el.tag)}.{name}"
        for child in el:
            if _local(child.tag) == tag:
                return next(iter(child), None)
        return None

    def _transform(self, el, scopes):
        t = self._prop(el, "RenderTransform", scopes)
        if t is None:
            return None
        return _matrix(t if isinstance(t, str) else t.get("Matrix", ""))

    def _path_geometry(self, g):
        """A Data/Clip value (abbreviated syntax or a PathGeometry element) as a QPainterPath."""
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QPainterPath
        if g is None:
            return None
        if isinstance(g, str):
            return _geometry(g)
        if _local(g.tag) != "PathGeometry":
            return None
        path = QPainterPath()
        if g.get("Figures"):
            path = _geometry(g.get("Figures"))
        path.setFillRule(Qt.WindingFill if g.get("FillRule") == "NonZero" else Qt.OddEvenFill)
        for fig in g:
            if _local(fig.tag) != "PathFigure":
                continue
            sx, sy = (float(v) for v in _NUM.findall(fig.get("StartPoint", "0,0"))[:2])
            path.moveTo(sx, sy)
            x, y = sx, sy
            for seg in fig:
                kind = _local(seg.tag)
                pts = [float(v) for v in _NUM.findall(seg.get("Points") or seg.get("Point") or "")]
                if kind in ("PolyLineSegment",):
                    for j in range(0, len(pts) - 1, 2):
                        path.lineTo(pts[j], pts[j + 1])
                elif kind == "PolyBezierSegment":
                    for j in range(0, len(pts) - 5, 6):
                        path.cubicTo(*pts[j:j + 6])
                elif kind == "PolyQuadraticBezierSegment":
                    for j in range(0, len(pts) - 3, 4):
                        path.quadTo(*pts[j:j + 4])
                elif kind == "ArcSegment" and len(pts) >= 2:
                    rx, ry = (float(v) for v in _NUM.findall(seg.get("Size", "0,0"))[:2])
                    _arc(path, x, y, rx, ry, float(seg.get("RotationAngle") or 0), seg.get("IsLargeArc") == "true",
                         seg.get("SweepDirection") == "Clockwise", pts[0], pts[1])
                if len(pts) >= 2:
                    x, y = pts[-2], pts[-1]
            if fig.get("IsClosed") == "true":
                path.closeSubpath()
        t = g.get("Transform")
        return _matrix(t).map(path) if t else path

    def _brush(self, b, scopes):
        """A QBrush for a Fill/Stroke value, or None (and draws image brushes itself: see _fill)."""
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QBrush, QLinearGradient, QRadialGradient
        if b is None:
            return None
        if isinstance(b, str):
            return QBrush(_color(b))
        kind = _local(b.tag)
        opacity = float(b.get("Opacity") or 1)
        if kind == "SolidColorBrush":
            c = _color(b.get("Color", "#FF000000"))
            c.setAlphaF(c.alphaF() * opacity)
            return QBrush(c)
        if kind in ("LinearGradientBrush", "RadialGradientBrush"):
            stops = []
            for prop in b:
                for st in prop:
                    if _local(st.tag) == "GradientStop":
                        c = _color(st.get("Color", "#FF000000"))
                        c.setAlphaF(c.alphaF() * opacity)
                        stops.append((max(0.0, min(1.0, float(st.get("Offset") or 0))), c))
            if kind == "LinearGradientBrush":
                s = [float(v) for v in _NUM.findall(b.get("StartPoint", "0,0"))[:2]]
                e = [float(v) for v in _NUM.findall(b.get("EndPoint", "0,0"))[:2]]
                grad = QLinearGradient(QPointF(*s), QPointF(*e))
            else:
                ctr = [float(v) for v in _NUM.findall(b.get("Center", "0,0"))[:2]]
                org = [float(v) for v in _NUM.findall(b.get("GradientOrigin", "0,0"))[:2]]
                grad = QRadialGradient(QPointF(*ctr), max(float(b.get("RadiusX") or 0), float(b.get("RadiusY") or 0)),
                                       QPointF(*org))
            for off, c in sorted(stops, key=lambda s: s[0]):
                grad.setColorAt(off, c)
            tprop = self._prop(b, "Transform", scopes)  # an attribute, or a MatrixTransform element
            t = tprop if isinstance(tprop, str) else tprop.get("Matrix") if tprop is not None else None
            brush = QBrush(grad)
            if t:
                brush.setTransform(_matrix(t))
            return brush
        return None  # ImageBrush and VisualBrush: see _fill

    def _fill(self, path, b, scopes) -> None:
        from PySide6.QtCore import QRectF, Qt
        if b is None:
            return
        if not isinstance(b, str) and _local(b.tag) == "ImageBrush":
            img = self.doc.image(b.get("ImageSource", ""), self.path)
            if img is None or img.isNull():
                return
            vb = [float(v) for v in _NUM.findall(b.get("Viewbox", ""))]
            vp = [float(v) for v in _NUM.findall(b.get("Viewport", ""))]
            if len(vb) != 4 or len(vp) != 4:
                return
            dpi_x = (img.dotsPerMeterX() or 3780) * 0.0254 or 96
            dpi_y = (img.dotsPerMeterY() or 3780) * 0.0254 or 96
            src = QRectF(vb[0] * dpi_x / 96, vb[1] * dpi_y / 96, vb[2] * dpi_x / 96, vb[3] * dpi_y / 96)
            self.p.save()
            t = b.get("Transform")
            self.p.setClipPath(path, Qt.IntersectClip)
            if t:
                self.p.setTransform(_matrix(t), True)
            self.p.setOpacity(self.p.opacity() * float(b.get("Opacity") or 1))
            self.p.drawImage(QRectF(*vp), img, src)
            self.p.restore()
            return
        brush = self._brush(b, scopes)
        if brush is not None:
            self.p.fillPath(path, brush)

    def element(self, el: ET.Element, scopes: list) -> None:
        from PySide6.QtCore import Qt
        kind = _local(el.tag)
        if "." in kind:
            return  # a property element: read by its owner
        scopes = self._resources(el, scopes)
        self.p.save()
        try:
            t = self._transform(el, scopes)
            if t is not None:
                self.p.setTransform(t, True)
            clip = self._path_geometry(self._prop(el, "Clip", scopes))
            if clip is not None:
                self.p.setClipPath(clip, Qt.IntersectClip)
            if el.get("Opacity"):
                self.p.setOpacity(self.p.opacity() * float(el.get("Opacity")))
            if kind in ("FixedPage", "Canvas"):
                for child in el:
                    self.element(child, scopes)
            elif kind == "Path":
                self._path(el, scopes)
            elif kind == "Glyphs":
                self._glyphs(el, scopes)
        finally:
            self.p.restore()

    def _path(self, el, scopes) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QPen
        path = self._path_geometry(self._prop(el, "Data", scopes))
        if path is None:
            return
        self._fill(path, self._prop(el, "Fill", scopes), scopes)
        stroke = self._prop(el, "Stroke", scopes)
        if stroke is not None:
            brush = self._brush(stroke, scopes)
            if brush is not None:
                pen = QPen(brush, float(el.get("StrokeThickness") or 1))
                pen.setJoinStyle({"Round": Qt.RoundJoin, "Bevel": Qt.BevelJoin}.get(el.get("StrokeLineJoin"), Qt.MiterJoin))
                cap = {"Round": Qt.RoundCap, "Square": Qt.SquareCap}
                pen.setCapStyle(cap.get(el.get("StrokeStartLineCap"), Qt.FlatCap))
                dashes = [float(v) for v in _NUM.findall(el.get("StrokeDashArray") or "")]
                if dashes and any(dashes):
                    pen.setDashPattern([max(d, 0.01) for d in dashes])
                self.p.strokePath(path, pen)

    def _glyphs(self, el, scopes) -> None:
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QGlyphRun, QPainterPath
        font = self.doc.font(el.get("FontUri", ""), self.path)
        if font is None:
            return
        size = float(el.get("FontRenderingEmSize") or 0)
        if size <= 0:
            return
        font.setPixelSize(size)
        text = el.get("UnicodeString") or ""
        text = text.removeprefix("{}")
        ids = list(font.glyphIndexesForString(text)) if text else []
        advances = [a.x() for a in font.advancesForGlyphIndexes(ids)] if ids else []
        offsets = [(0.0, 0.0)] * len(ids)
        # Indices: "gid,advance,uOffset,vOffset;" per glyph, each part optional (advances in 1/100 em)
        if el.get("Indices"):
            cells = el.get("Indices").split(";")
            ids_out, adv_out, off_out = [], [], []
            for k, cell in enumerate(cells):
                cluster = cell.split(")", 1)[-1]  # a cluster map "(c:g)" in front: the glyph after it
                parts = cluster.split(",")
                gid = int(parts[0]) if parts and parts[0].strip().lstrip("-").isdigit() else (ids[k] if k < len(ids) else None)
                if gid is None:
                    continue
                adv = float(parts[1]) * size / 100 if len(parts) > 1 and parts[1].strip() else \
                    (advances[k] if k < len(advances) else font.advancesForGlyphIndexes([gid])[0].x())
                uo = float(parts[2]) * size / 100 if len(parts) > 2 and parts[2].strip() else 0.0
                vo = float(parts[3]) * size / 100 if len(parts) > 3 and parts[3].strip() else 0.0
                ids_out.append(gid)
                adv_out.append(adv)
                off_out.append((uo, -vo))
            if ids_out:
                ids, advances, offsets = ids_out, adv_out, off_out
        if not ids:
            return
        rtl = int(el.get("BidiLevel") or 0) % 2 == 1
        x = 0.0
        positions = []
        for adv, (uo, vo) in zip(advances, offsets):
            if rtl:
                x -= adv
            positions.append(QPointF(x + uo, vo))
            if not rtl:
                x += adv
        origin = QPointF(float(el.get("OriginX") or 0), float(el.get("OriginY") or 0))
        fill = self._prop(el, "Fill", scopes)
        sim = el.get("StyleSimulations") or ""
        if "Italic" in sim:
            from PySide6.QtGui import QTransform
            self.p.translate(origin)
            self.p.setTransform(QTransform(1, 0, -0.33, 1, 0, 0), True)
            origin = QPointF(0, 0)
        if fill is not None and (isinstance(fill, str) or _local(fill.tag) == "SolidColorBrush"):
            brush = self._brush(fill, scopes)
            run = QGlyphRun()
            run.setRawFont(font)
            run.setGlyphIndexes(ids)
            run.setPositions(positions)
            self.p.setPen(brush.color())
            self.p.drawGlyphRun(origin, run)
            if "Bold" in sim:
                self.p.drawGlyphRun(origin + QPointF(size / 50, 0), run)
        else:
            outline = QPainterPath()  # a picture or gradient fill: the glyphs' outlines are filled with it
            for gid, pos in zip(ids, positions):
                outline.addPath(font.pathForGlyph(gid).translated(origin + pos))
            self._fill(outline, fill, scopes)
        width = abs(positions[-1].x() - positions[0].x()) + (advances[-1] if advances else size / 2)
        self._hidden_text(text, origin + positions[0] if not rtl else origin + positions[-1], width, size,
                          self.doc.family(el.get("FontUri", ""), self.path))

    def _hidden_text(self, text: str, start, width: float, size: float, family: str | None) -> None:
        """The glyphs' characters, drawn invisibly over the glyphs: glyphs drawn by index don't say
        which characters they are, and this keeps the PDF's text searchable and copyable (as a
        scanned page's recognised text is). The line goes as one piece (a word or a character at
        a time reads back as "w o r d" in some readers, a line per word in others), its size cut
        down where it would run past the glyphs."""
        from PySide6.QtGui import QColor, QFont, QFontMetricsF
        if not text.strip():
            return
        # in the document's own font where Qt takes it (it's embedded with its character map, and
        # its widths are the glyphs'), else Arial
        f = QFont(family or "Arial")
        f.setPointSizeF(size * PX_TO_PT)  # the PDF writer runs at 96 dpi: points are 4/3 pixels
        natural = QFontMetricsF(f, self.p.device()).horizontalAdvance(text)
        if natural > width > 0:
            f.setPointSizeF(max(0.5, size * PX_TO_PT * width / natural))
        self.p.setFont(f)
        self.p.setPen(QColor(0, 0, 0, 0))
        self.p.drawText(start, text)


def xps_to_pdf(src: Path, dst: Path) -> int:
    """Every page of an XPS document into a PDF; returns the page count."""
    from PySide6.QtCore import QMarginsF, QSizeF
    from PySide6.QtGui import QPageLayout, QPageSize, QPainter
    app()
    doc = Xps(src)
    try:
        w, h, first = doc.page_size(0)
        writer = _pdf_writer(dst, w * PX_TO_PT, h * PX_TO_PT)
        painter = QPainter(writer)
        try:
            for k in range(len(doc.pages)):
                try:
                    w, h, root = doc.page_size(k) if k else (w, h, first)  # each page's XML read once
                    if k:
                        size = QPageSize(QSizeF(w * PX_TO_PT, h * PX_TO_PT), QPageSize.Unit.Point, "",
                                         QPageSize.SizeMatchPolicy.ExactMatch)
                        writer.setPageLayout(QPageLayout(size, QPageLayout.Orientation.Portrait,
                                                         QMarginsF(0, 0, 0, 0)))
                        writer.newPage()
                    doc.draw(k, painter, root)
                except (KeyError, ET.ParseError, ValueError) as exc:
                    raise ConversionError(f"{src.name} is a damaged XPS document (page {k + 1}: {exc})") from None
        finally:
            painter.end()
        return len(doc.pages)
    finally:
        doc.close()
