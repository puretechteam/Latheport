"""Text on pictures and videos: text boxes placed in the editor (or --text), drawings (arrows,
shapes, pen, highlighter, stickers) and a watermark (text, a picture or both), drawn with the fonts
in assets/fonts so they look the same everywhere. Each is drawn onto a see-through layer the size of
the picture (or of the video's frames), laid over it afterwards."""

from __future__ import annotations

import functools
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from .errors import ConversionError

FONT_DIR = Path(__file__).resolve().parent / "assets" / "fonts"
FONT_FILES = {"sans": "Inter-SemiBold.ttf", "serif": "Lora-Variable.ttf", "bold": "Anton-Regular.ttf",
              "hand": "Caveat-SemiBold.ttf"}
WIDEST = 0.94       # of the picture's width: longer text wraps, then shrinks
MOST_LINES = 3


@functools.lru_cache(maxsize=64)
def font(name: str, px: int) -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(str(FONT_DIR / FONT_FILES[name]), max(4, px))
    if name == "serif":
        f.set_variation_by_axes([600])  # Lora ships as it is (its licence reserves the name): semibold here
    return f


def _wrap(draw: ImageDraw.ImageDraw, text: str, f, width: float) -> list[str]:
    """text in lines no wider than width, breaking between words (a single long word stays whole)."""
    lines: list[str] = []
    for word in text.split():
        trial = f"{lines[-1]} {word}" if lines else word
        if lines and draw.textlength(trial, font=f) <= width:
            lines[-1] = trial
        else:
            lines.append(word)
    return lines or [""]


def _fit(draw: ImageDraw.ImageDraw, text: str, name: str, px: int, width: float) -> tuple:
    """(font, lines) for text at px letters, wrapped to width in at most MOST_LINES lines, made smaller
    until it fits (as meme captions do)."""
    while True:
        f = font(name, px)
        lines = _wrap(draw, text, f, width)
        if (len(lines) <= MOST_LINES and max(draw.textlength(ln, font=f) for ln in lines) <= width) or px <= 6:
            return f, lines
        px = max(6, int(px * 0.9))


def _rgb(colour: str) -> tuple[int, int, int]:
    return tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5))


def _inside(draw: ImageDraw.ImageDraw, at: tuple[float, float], body: str, f, size: tuple[int, int],
            stroke: int = 0) -> tuple:
    """at, moved as little as it takes for the text centred there to be inside the picture, a small
    margin from its edges (a caption near the top that wraps to three lines grows downwards)."""
    w, h = size
    margin = 0.02 * min(w, h)
    x0, y0, x1, y1 = draw.multiline_textbbox(at, body, font=f, anchor="mm", align="center",
                                             spacing=round(f.size * 0.12), stroke_width=stroke)
    dx = max(0.0, margin - x0) - max(0.0, x1 - (w - margin)) if x1 - x0 < w - 2 * margin else 0.0
    dy = max(0.0, margin - y0) - max(0.0, y1 - (h - margin)) if y1 - y0 < h - 2 * margin else 0.0
    return at[0] + dx, at[1] + dy


def _place(draw: ImageDraw.ImageDraw, box: dict, size: tuple[int, int]) -> tuple:
    """(font, the lines as one text, where its middle goes, outline width) for a text box on a
    picture of size."""
    w, h = size
    text = box["text"].upper() if box.get("meme") else box["text"]
    f, lines = _fit(draw, text, box["font"], max(4, round(box["size"] * h)), WIDEST * w)
    body = "\n".join(lines)
    stroke = max(1, round(f.size * (0.09 if box.get("meme") else 0.06))) if box["edge"] == "outline" else 0
    return f, body, _inside(draw, (box["x"] * w, box["y"] * h), body, f, size, stroke), stroke


def text_bounds(size: tuple[int, int], boxes: list[dict]) -> list[tuple[float, float, float, float]]:
    """Where each text box ends up on a picture of size: (left, top, right, bottom), for picking
    one with the mouse."""
    draw = ImageDraw.Draw(Image.new("L", (1, 1)))
    out = []
    for box in boxes:
        f, body, at, stroke = _place(draw, box, size)
        out.append(draw.multiline_textbbox(at, body, font=f, anchor="mm", align="center",
                                           spacing=round(f.size * 0.12), stroke_width=stroke))
    return out


def text_layer(size: tuple[int, int], boxes: list[dict]) -> Image.Image:
    """A see-through picture of size with each text box drawn where it says (options.check_box)."""
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    shadows = Image.new("RGBA", size, (0, 0, 0, 0))
    draw, shade = ImageDraw.Draw(layer), ImageDraw.Draw(shadows)
    blur = 0
    for box in boxes:
        f, body, at, stroke = _place(draw, box, size)
        px = f.size
        colour = _rgb(box["colour"])
        dark = sum(colour) < 3 * 110  # dark text gets a light edge
        kw = {"font": f, "anchor": "mm", "align": "center", "spacing": round(px * 0.12)}
        if box["edge"] == "shadow":
            off = max(1, round(px * 0.05))
            shade.multiline_text((at[0] + off, at[1] + off), body, fill=(255, 255, 255, 150) if dark else (0, 0, 0, 170), **kw)
            blur = max(blur, px * 0.05)
        draw.multiline_text(at, body, fill=(*colour, 255), stroke_width=stroke,
                            stroke_fill=(255, 255, 255, 255) if dark else (0, 0, 0, 255), **kw)
    if blur:
        return Image.alpha_composite(_blurred(shadows, blur), layer)
    return layer


def _blurred(img: Image.Image, radius: float) -> Image.Image:
    """img blurred, working only around what's drawn on it (a few words on a 24-megapixel layer)."""
    box = img.getbbox()
    if box is None:
        return img
    pad = int(radius * 4) + 4
    x0, y0 = max(0, box[0] - pad), max(0, box[1] - pad)
    x1, y1 = min(img.width, box[2] + pad), min(img.height, box[3] + pad)
    out = Image.new("RGBA", img.size, (0, 0, 0, 0))
    out.paste(img.crop((x0, y0, x1, y1)).filter(ImageFilter.GaussianBlur(radius)), (x0, y0))
    return out


LOGO_WIDE, LOGO_TALL = 0.16, 0.12  # the most of the picture's width and height a watermark picture takes


def watermark_layer(size: tuple[int, int], text: str, corner: str = "bottom-right", dark: bool = False,
                    opacity: int = 60, image: Image.Image | None = None) -> Image.Image:
    """A see-through picture of size with the watermark small in a corner: the text white with a soft
    dark shadow (or, dark, for a bright corner, the other way round), the picture (a logo) beside the
    corner with the text between it and the edge, both as solid as opacity says (percent)."""
    w, h = size
    margin = max(4, round(0.02 * min(w, h)))
    right, bottom = corner.endswith("right"), corner.startswith("bottom")
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    solid = max(0, min(100, opacity)) / 100
    used = 0  # the height the text takes from the corner
    if text:
        f, lines = _fit(ImageDraw.Draw(Image.new("L", (1, 1))), text, "sans", max(10, round(0.03 * h)), 0.6 * w)
        at = (w - margin if right else margin, h - margin if bottom else margin)
        kw = {"font": f, "anchor": ("r" if right else "l") + ("d" if bottom else "a"),
              "align": "right" if right else "left", "spacing": round(f.size * 0.12)}
        body = "\n".join(lines)
        shadow = Image.new("RGBA", size, (0, 0, 0, 0))
        off = max(1, round(f.size * 0.06))
        a_ink, a_glow = round(255 * solid), round(255 * solid * 110 / 153)
        ink, glow = ((20, 20, 20, a_ink), (255, 255, 255, a_glow)) if dark else ((255, 255, 255, a_ink), (0, 0, 0, a_glow))
        ImageDraw.Draw(shadow).multiline_text((at[0] + off, at[1] + off), body, fill=glow, **kw)
        layer = _blurred(shadow, f.size * 0.06)
        ImageDraw.Draw(layer).multiline_text(at, body, fill=ink, **kw)
        _, y0, _, y1 = ImageDraw.Draw(layer).multiline_textbbox(at, body, **kw)
        used = round(y1 - y0 + 0.4 * f.size)
    if image is not None:
        logo = image.convert("RGBA")
        scale = min(LOGO_WIDE * w / logo.width, LOGO_TALL * h / logo.height)
        logo = logo.resize((max(1, round(logo.width * scale)), max(1, round(logo.height * scale))), Image.Resampling.LANCZOS)
        if solid < 1:
            logo.putalpha(logo.getchannel("A").point(lambda v: round(v * solid)))
        x = w - margin - logo.width if right else margin
        y = h - margin - used - logo.height if bottom else margin + used
        layer.alpha_composite(logo, (max(0, x), max(0, y)))
    return layer


_logos: dict = {}


def watermark_picture(path: str) -> Image.Image:
    """The watermark picture at path, read once while it's unchanged; said in plain words if it's
    gone or isn't a picture."""
    file = Path(path)
    try:
        stamp = (str(file), file.stat().st_mtime_ns)
    except OSError:
        raise ConversionError(f"The watermark picture {file.name} isn't there any more: choose it again "
                              "in the editor, or take it off") from None
    if _logos.get("at") != stamp:
        try:
            with Image.open(file) as img:
                img.seek(0)
                logo = img.convert("RGBA")
        except Exception:  # noqa: BLE001 - anything Pillow throws means not a picture
            raise ConversionError(f"The watermark picture {file.name} doesn't open as a picture") from None
        _logos.update(at=stamp, logo=logo)
    return _logos["logo"]


# ---- drawings: arrows, lines, boxes, ellipses, pen and highlighter strokes, stickers

SS = 3                # drawn this many times bigger, then made smaller: smooth edges
HIGHLIGHT = 0.4       # how solid the highlighter is
SS_MOST = 12_000_000  # pixels: a drawing past this is drawn less big


def _px(mark: dict, size: tuple[int, int]) -> list[tuple[float, float]]:
    return [(x * size[0], y * size[1]) for x, y in mark["points"]]


def _pen_width(mark: dict, size: tuple[int, int]) -> float:
    width = mark["width"] * min(size)
    return max(1.0, width * (4 if mark["kind"] == "highlight" else 1))


def _sticker_side(mark: dict, size: tuple[int, int]) -> float:
    return max(8.0, mark.get("size", 0.08) * min(size))


def mark_bounds(mark: dict, size: tuple[int, int]) -> tuple[float, float, float, float]:
    """Where a drawing is on a picture of size (left, top, right, bottom), room for its line included."""
    pts = _px(mark, size)
    if mark["kind"] == "sticker":
        side = _sticker_side(mark, size) / 2
        (x, y), = pts
        return x - side, y - side, x + side, y + side
    pad = _pen_width(mark, size) * (3.5 if mark["kind"] == "arrow" else 1) + 2
    xs, ys = [q[0] for q in pts], [q[1] for q in pts]
    return min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad


def _rgba(colour: str, alpha: int = 255) -> tuple[int, int, int, int]:
    return (*_rgb(colour), alpha)


def _star(cx: float, cy: float, r: float) -> list[tuple[float, float]]:
    return [(cx + (r if i % 2 == 0 else r * 0.45) * math.sin(math.pi * i / 5),
             cy - (r if i % 2 == 0 else r * 0.45) * math.cos(math.pi * i / 5)) for i in range(10)]


def _heart(cx: float, cy: float, r: float) -> list[tuple[float, float]]:
    pts = []
    for i in range(72):
        t = 2 * math.pi * i / 72
        x = 16 * math.sin(t) ** 3
        y = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
        pts.append((cx + x * r / 17, cy - y * r / 17 - r * 0.08))
    return pts


def _draw_one(draw: ImageDraw.ImageDraw, mark: dict, pts: list, width: float, side: float) -> None:
    """One drawing, at the scale it's drawn at (points and widths already scaled)."""
    kind, ink = mark["kind"], _rgba(mark["colour"])
    w = max(1, round(width))
    if kind in ("pen", "highlight", "line"):
        if len(pts) > 1:
            draw.line(pts, fill=ink, width=w, joint="curve")
        for x, y in (pts[0], pts[-1]):  # round ends
            draw.ellipse((x - width / 2, y - width / 2, x + width / 2, y + width / 2), fill=ink)
    elif kind == "arrow":
        (x0, y0), (x1, y1) = pts
        length = math.hypot(x1 - x0, y1 - y0) or 1.0
        head = min(max(width * 4.5, 10.0), length * 0.6)
        ux, uy = (x1 - x0) / length, (y1 - y0) / length
        bx, by = x1 - ux * head, y1 - uy * head
        draw.line([(x0, y0), (bx + ux * head * 0.2, by + uy * head * 0.2)], fill=ink, width=w)
        draw.ellipse((x0 - width / 2, y0 - width / 2, x0 + width / 2, y0 + width / 2), fill=ink)
        half = head * 0.55
        draw.polygon([(x1, y1), (bx - uy * half, by + ux * half), (bx + uy * half, by - ux * half)], fill=ink)
    elif kind in ("box", "ellipse"):
        (x0, y0), (x1, y1) = pts
        shape = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        how = draw.rectangle if kind == "box" else draw.ellipse
        if mark.get("fill"):
            how(shape, fill=ink)
        else:
            how(shape, outline=ink, width=w)
    else:  # a sticker: a shape in the colour, edged in white so it shows on anything
        (cx, cy), = pts
        r = side / 2
        edge = max(1, round(side * 0.05))
        white = (255, 255, 255, 255)
        name = mark["name"]
        if name == "star":
            draw.polygon(_star(cx, cy, r * 0.98), fill=ink, outline=white, width=edge)
        elif name == "heart":
            draw.polygon(_heart(cx, cy, r * 0.98), fill=ink, outline=white, width=edge)
        else:
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=ink, outline=white, width=edge)
            stroke = max(2, round(side * 0.11))
            if name == "check":
                draw.line([(cx - r * 0.45, cy + r * 0.02), (cx - r * 0.12, cy + r * 0.35), (cx + r * 0.48, cy - r * 0.32)],
                          fill=white, width=stroke, joint="curve")
            elif name == "cross":
                k = r * 0.38
                draw.line([(cx - k, cy - k), (cx + k, cy + k)], fill=white, width=stroke)
                draw.line([(cx - k, cy + k), (cx + k, cy - k)], fill=white, width=stroke)
            else:  # a number
                draw.text((cx, cy), str(mark.get("n", 1)), fill=white, font=font("sans", round(side * 0.55)), anchor="mm")


def marks_layer(size: tuple[int, int], marks: list[dict]) -> Image.Image:
    """A see-through picture of size with each drawing (options.check_mark) where it says, in order."""
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    for mark in marks:
        x0, y0, x1, y1 = mark_bounds(mark, size)
        x0, y0 = max(0, math.floor(x0)), max(0, math.floor(y0))
        x1, y1 = min(size[0], math.ceil(x1)), min(size[1], math.ceil(y1))
        if x1 <= x0 or y1 <= y0:
            continue
        ss = SS if (x1 - x0) * (y1 - y0) * SS * SS <= SS_MOST else 1
        canvas = Image.new("RGBA", ((x1 - x0) * ss, (y1 - y0) * ss), (0, 0, 0, 0))
        pts = [((x - x0) * ss, (y - y0) * ss) for x, y in _px(mark, size)]
        _draw_one(ImageDraw.Draw(canvas), mark, pts, _pen_width(mark, size) * ss, _sticker_side(mark, size) * ss)
        if ss > 1:
            canvas = canvas.resize((x1 - x0, y1 - y0), Image.Resampling.LANCZOS)
        if mark["kind"] == "highlight":  # see-through, the same all along (no darker where it crosses itself)
            canvas.putalpha(canvas.getchannel("A").point(lambda v: round(v * HIGHLIGHT)))
        layer.alpha_composite(canvas, (x0, y0))
    return layer


def bright_corner(img: Image.Image, corner: str) -> bool:
    """Whether the corner a watermark goes in is bright (white text wouldn't show there)."""
    w, h = img.size
    cw, ch = max(1, w // 3), max(1, h // 8)
    x = w - cw if corner.endswith("right") else 0
    y = h - ch if corner.startswith("bottom") else 0
    grey = img.crop((x, y, x + cw, y + ch)).convert("RGBA")  # the corner only: not a copy of the whole picture
    bg = Image.new("RGBA", grey.size, (128, 128, 128, 255))  # see-through counts as middling
    return float(np.asarray(Image.alpha_composite(bg, grey).convert("L"), dtype=np.float32).mean()) > 170


def lay_over(img: Image.Image, layer: Image.Image) -> Image.Image:
    """img with layer on top, in RGB, or RGBA if img has transparency (text over a see-through part
    is still seen)."""
    has_alpha = img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info)
    base = img.convert("RGBA")
    out = Image.alpha_composite(base, layer)
    if not has_alpha:
        return out.convert("RGB")
    out.putalpha(ImageChops.lighter(base.getchannel("A"), layer.getchannel("A")))
    return out
