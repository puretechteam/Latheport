"""Raster images via Pillow (+ pillow-heif for HEIC), small writers for what Pillow can only read
(PSD, CUR, XPM), and tracing pictures into SVG vectors (vtracer)."""

import contextlib
import functools
import importlib.util
import io
import math
import re
import string
import struct
from pathlib import Path

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps, ImageSequence

from .. import options, overlay
from ..errors import ConversionError
from ..losses import MAJOR, loss
from ..options import area_box
from ..registry import converter, expect, note, option, take

PIL_FORMAT = {
    "png": "PNG", "jpg": "JPEG", "webp": "WEBP", "gif": "GIF", "bmp": "BMP", "tiff": "TIFF",
    "ico": "ICO", "tga": "TGA", "avif": "AVIF", "ppm": "PPM", "jp2": "JPEG2000", "pcx": "PCX",
    "dds": "DDS", "qoi": "QOI", "icns": "ICNS", "pdf": "PDF", "sgi": "SGI", "xbm": "XBM",
    "eps": "EPS",  # written only: EPS is read as PostScript (bookport.ps, pdf.py)
}
if importlib.util.find_spec("pillow_heif"):  # without it, HEIC just isn't offered
    PIL_FORMAT["heic"] = "HEIF"


@functools.cache
def enable_heif() -> None:
    """Teach Pillow HEIC on first use; importing pillow-heif up front would slow every run."""
    if "heic" in PIL_FORMAT:
        import pillow_heif
        pillow_heif.register_heif_opener()



def _pixels(img: Image.Image):
    """img's pixel values in order (Pillow 12.1 renamed getdata, which 14 removes)."""
    return img.get_flattened_data() if hasattr(img, "get_flattened_data") else img.getdata()


# ---- writers for formats Pillow reads but can't save

def write_psd(img: Image.Image, dst: Path) -> None:
    """Flattened Photoshop file: one image, no layers, raw channels (transparency as a 4th channel)."""
    img = img.convert("RGBA" if "A" in img.getbands() or "transparency" in img.info else "RGB")
    w, h = img.size
    if max(w, h) > 30000:
        raise ConversionError("Photoshop files are at most 30,000 pixels on a side")
    head = b"8BPS" + struct.pack(">H6xHIIHH", 1, len(img.getbands()), h, w, 8, 3)
    planes = b"".join(band.tobytes() for band in img.split())
    # empty colour-mode, resources and layer sections; compression 0 = raw
    dst.write_bytes(head + struct.pack(">III", 0, 0, 0) + struct.pack(">H", 0) + planes)


def write_cur(img: Image.Image, dst: Path) -> None:
    """A Windows cursor is an icon with type 2 and a hotspot (top-left) instead of planes/bit depth."""
    img = img.convert("RGBA")
    side = max(*img.size, 32)  # 32 px is the smallest cursor Windows draws
    if img.size != (side, side):
        sq = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        sq.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
        img = sq
    buf = io.BytesIO()
    sizes = [(s, s) for s in (32, 48, 64, 96, 128) if s <= side]
    img.save(buf, "ICO", sizes=sizes, bitmap_format="bmp")
    data = bytearray(buf.getvalue())
    data[2] = 2
    for i in range(struct.unpack_from("<H", data, 4)[0]):
        struct.pack_into("<HH", data, 6 + 16 * i + 4, 0, 0)  # hotspot x, y
    dst.write_bytes(bytes(data))


_XPM_CHARS = "".join(c for c in string.printable[:94] if c not in '"\\')


def write_xpm(img: Image.Image, dst: Path) -> None:
    """X pixmap: C source text with a palette (up to 256 colours; transparent pixels are None)."""
    rgba = img.convert("RGBA")
    alpha = rgba.getchannel("A").point(lambda a: 255 if a >= 128 else 0)
    pal_img = rgba.convert("RGB").quantize(255, dither=Image.Dither.NONE)
    palette = pal_img.getpalette()[: 3 * 255]
    px = list(_pixels(pal_img))
    clear = 255  # index for transparent pixels
    px = [clear if a == 0 else v for v, a in zip(px, _pixels(alpha))]
    used = sorted(set(px))
    cpp = 1 if len(used) <= len(_XPM_CHARS) else 2
    codes = [a + b for a in _XPM_CHARS for b in (_XPM_CHARS if cpp == 2 else [""])]
    code = {idx: codes[i] for i, idx in enumerate(used)}
    w, h = rgba.size
    lines = [f'"{w} {h} {len(used)} {cpp}"']
    for idx in used:
        color = "None" if idx == clear else "#{:02X}{:02X}{:02X}".format(*palette[3 * idx: 3 * idx + 3])
        lines.append(f'"{code[idx]} c {color}"')
    for y in range(h):
        lines.append('"' + "".join(code[v] for v in px[y * w:(y + 1) * w]) + '"')
    name = "".join(c if c.isalnum() else "_" for c in dst.stem) or "image"
    if name[0].isdigit():
        name = f"_{name}"
    dst.write_text(f"/* XPM */\nstatic char *{name}[] = {{\n" + ",\n".join(lines) + "};\n", encoding="ascii")


OWN_WRITERS = {"psd": write_psd, "cur": write_cur, "xpm": write_xpm}
WRITE = set(PIL_FORMAT) | set(OWN_WRITERS)
READ = (set(PIL_FORMAT) - {"pdf", "eps"}) | {"psd", "cur", "xpm"}
NO_ALPHA = {"jpg", "ppm", "pdf", "pcx", "xbm", "eps"}
ANIMATED = {"gif", "webp", "png", "avif", "tiff", "pdf"}
SAVE_OPTS = {
    "jpg": {"quality": 92, "optimize": True},
    "webp": {"quality": 90, "method": 6},
    "avif": {"quality": 80},
    "heic": {"quality": 90},
    "png": {"optimize": True},
    "tiff": {"compression": "tiff_lzw"},
    "pdf": {"resolution": 150},
}
HEIC_READABLE = 16 << 20
QUALITY = {"jpg", "webp", "avif", "heic"}  # lossy: these take the quality setting
# where the photo's details (EXIF: camera, date, place) and color profile go along
KEEPS_INFO = {"jpg", "png", "webp", "avif", "heic", "tiff"}
ICONS = {"ico": "16 to 256", "icns": "16 to 1024", "cur": "32 to 128"}  # fixed square sizes, in pixels


def prepare(img: Image.Image, fmt: str) -> Image.Image:
    """Convert img's mode to something fmt can store."""
    if img.mode in ("I", "I;16", "I;16B", "F"):
        img = img.convert("I").point(lambda v: v / 256).convert("L") if img.mode != "F" else img.convert("L")
    has_alpha = img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info)
    if fmt in NO_ALPHA:
        if has_alpha:
            rgba = img.convert("RGBA")
            bg = Image.new("RGB", rgba.size, "white")
            bg.paste(rgba, mask=rgba.getchannel("A"))
            img = bg
        if fmt == "xbm":  # black and white only
            return img.convert("1")
        return img if img.mode in ("RGB", "L") else img.convert("RGB")
    if fmt in ("heic", "avif", "webp", "dds", "qoi", "icns", "ico", "jp2", "tga", "bmp", "tiff", "png", "sgi") and (
            img.mode not in ("RGB", "RGBA", "L") or (fmt in ("heic", "avif", "dds", "qoi", "icns") and img.mode == "L")):
        return img.convert("RGBA" if has_alpha else "RGB")
    return img


def fit(img: Image.Image, fmt: str) -> Image.Image:
    """img scaled down to the max_size setting (its longest side, in pixels), if it's bigger."""
    size = option("max_size")
    if not size or fmt in ICONS or max(img.size) <= size:
        return img
    scale = size / max(img.size)
    return img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.Resampling.LANCZOS)


# ---- deep fried: the meme look

def fry_recipe(heat: int) -> tuple:
    """How hard each part of the fry goes at heat 1 to 100: (side it's fried at, warmth, contrast,
    saturation, sharpening %, grain, JPEG quality, times saved). The picture is fried at a fixed size,
    smaller the hotter it is (bigger blocks), then scaled back, so a 4000 px photo fries like a meme."""
    t = (min(max(heat, 1), 100) - 1) / 99
    lerp = lambda a, b: a + (b - a) * t
    return (round(1400 * (420 / 1400) ** t), lerp(1.03, 1.4), lerp(1.15, 3.0), lerp(1.5, 8.0),
            round(lerp(100, 650)), lerp(4, 32), round(lerp(45, 4)), round(lerp(1, 6)))


def deepfry(img: Image.Image, heat: int) -> Image.Image:
    """img with its colours, contrast and sharpness pushed up (more the hotter, 1 to 100) and
    JPEG-crunched, the same size (transparency kept). The grain is the same on every run."""
    side, warmth, contrast, colour, sharpen, noise, quality, times = fry_recipe(heat)
    has_alpha = img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info)
    alpha = img.convert("RGBA").getchannel("A") if has_alpha else None
    rgb = img.convert("RGB")
    size = rgb.size
    scale = min(1.0, side / max(size))
    if scale < 1:
        rgb = rgb.resize((max(1, round(size[0] * scale)), max(1, round(size[1] * scale))), Image.Resampling.BILINEAR)
    px = np.asarray(rgb, dtype=np.float32) * np.array([warmth, 1.0, 1 / warmth], dtype=np.float32)
    rgb = Image.fromarray(np.clip(px, 0, 255).astype(np.uint8))
    rgb = ImageEnhance.Contrast(rgb).enhance(contrast)
    rgb = ImageEnhance.Color(rgb).enhance(colour)
    rng = np.random.default_rng(1337)
    for i in range(times):  # saved again and again at a low quality: the blocks and colour smears,
        if i < 2:  # sharpened and grainy, so the blocks' edges glow
            rgb = rgb.filter(ImageFilter.UnsharpMask(radius=2, percent=sharpen, threshold=0))
            grain = rng.normal(0, noise, (rgb.height, rgb.width, 3)).astype(np.float32)
            rgb = Image.fromarray(np.clip(np.asarray(rgb, dtype=np.float32) + grain, 0, 255).astype(np.uint8))
        buf = io.BytesIO()
        rgb.save(buf, "JPEG", quality=quality, subsampling=2)
        buf.seek(0)
        rgb = Image.open(buf).convert("RGB")
    if rgb.size != size:
        rgb = rgb.resize(size, Image.Resampling.BICUBIC)
    # a last pass at full size: the crunch mixes colours and smooths edges, so they're pushed again
    rgb = ImageEnhance.Color(rgb).enhance(1 + (colour - 1) * 0.15)
    rgb = rgb.filter(ImageFilter.UnsharpMask(radius=1, percent=sharpen // 2, threshold=0))
    if alpha is not None:
        rgb.putalpha(alpha)
    return rgb


# ---- effects: turned, cropped, touched up, in black and white or sepia (and deep fried)

QUARTERS = {1: Image.Transpose.ROTATE_270, 2: Image.Transpose.ROTATE_180, 3: Image.Transpose.ROTATE_90}


def turned(img: Image.Image, turn: str | None) -> Image.Image:
    """img as the Turn setting lies it: mirrored first if it says so, then turned clockwise."""
    mirrored, quarters = options.orientation(turn)
    if mirrored:
        img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    return img.transpose(QUARTERS[quarters]) if quarters else img
SHAPES = {"square": (1, 1), "4:5": (4, 5), "16:9": (16, 9), "9:16": (9, 16)}  # width : height


def shape_name(shape: str) -> str:
    return "a square" if shape == "square" else shape


def _alpha(img: Image.Image) -> Image.Image | None:
    has_alpha = img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info)
    return img.convert("RGBA").getchannel("A") if has_alpha else None


def _with(img: Image.Image, alpha: Image.Image | None) -> Image.Image:
    if alpha is not None:
        img.putalpha(alpha)
    return img


def shape_box(size: tuple[int, int], shape: tuple[int, int]) -> tuple[int, int, int, int]:
    """The middle of a picture of size, as wide as shape says for its height (or as high for its
    width): (left, top, right, bottom)."""
    (w, h), (sw, sh) = size, shape
    if w * sh > h * sw:  # wider than the shape: trim the sides
        nw = max(1, round(h * sw / sh))
        return ((w - nw) // 2, 0, (w - nw) // 2 + nw, h)
    nh = max(1, round(w * sh / sw))
    return (0, (h - nh) // 2, w, (h - nh) // 2 + nh)


def straighten_scale(size: tuple[int, int], angle: float) -> float:
    """How much bigger a picture of size is drawn, turned by angle degrees, for it to fill its own
    frame with no empty corner (and a pixel to spare, so none of the edge shows)."""
    w, h = size
    a = math.radians(abs(angle))
    return math.cos(a) + max(w / h, h / w) * math.sin(a) + 3 / max(1, min(w, h))


def straightened(img: Image.Image, angle: float) -> Image.Image:
    """img turned by angle degrees (clockwise when more than 0) and cropped in so no corner is empty,
    the same size."""
    if img.mode not in ("L", "LA", "RGB", "RGBA"):
        img = img.convert("RGBA" if _alpha(img) is not None else "RGB")
    w, h = img.size
    scale, turn = straighten_scale(img.size, angle), math.radians(angle)
    cx, cy = w / 2, h / 2
    # one pass, each pixel of the result read from where it lies in the original (turned back, drawn smaller)
    a, b = math.cos(turn) / scale, math.sin(turn) / scale
    d, e = -b, a
    return img.transform((w, h), Image.Transform.AFFINE, (a, b, cx - a * cx - b * cy, d, e, cy - d * cx - e * cy),
                         resample=Image.Resampling.BICUBIC)


def hide_box(hide: dict, w: int, h: int, even: bool = False) -> tuple[int, int, int, int]:
    """A part to hide as (left, top, width, height) in pixels of a w x h picture, at least 2 each way
    (even ones for video, which most encoders need)."""
    step = 2 if even else 1
    x, y = int(hide["x"] * w) // step * step, int(hide["y"] * h) // step * step
    pw = max(2, math.ceil(hide["w"] * w / step) * step)
    ph = max(2, math.ceil(hide["h"] * h / step) * step)
    pw, ph = min(pw, w - x) // step * step or step, min(ph, h - y) // step * step or step
    return min(x, w - pw), min(y, h - ph), pw, ph


def pixel_grid(pw: int, ph: int, strength: int) -> tuple[int, int]:
    """How many blocks across and down a pixelated part is: from about 30 on its shorter side at
    strength 1 to 4 at 100 (12 at 70, the start)."""
    blocks = 4 + round(28 * (1 - strength / 100))
    side = min(pw, ph)
    return max(1, round(pw * blocks / side)), max(1, round(ph * blocks / side))


def blur_sigma(pw: int, ph: int, strength: int) -> float:
    """How much a blurred part is blurred: a share of its shorter side, more the stronger."""
    return max(1.5, min(pw, ph) * (0.02 + 0.18 * strength / 100))


def hidden(img: Image.Image, hides: list[dict]) -> Image.Image:
    """img with each part blurred, pixelated or covered in black (options.check_hide)."""
    if img.mode not in ("L", "LA", "RGB", "RGBA"):
        img = img.convert("RGBA" if _alpha(img) is not None else "RGB")
    out = img.copy()
    for hide in hides:
        x, y, pw, ph = hide_box(hide, *out.size)
        part = out.crop((x, y, x + pw, y + ph))
        if hide["how"] == "pixelate":
            part = part.resize(pixel_grid(pw, ph, hide["strength"]), Image.Resampling.BOX).resize((pw, ph), Image.Resampling.NEAREST)
        elif hide["how"] == "blur":
            part = part.filter(ImageFilter.GaussianBlur(blur_sigma(pw, ph, hide["strength"])))
        else:
            part = Image.new(part.mode, part.size, 0 if part.mode == "L" else (0,) * len(part.mode))
            if part.mode in ("LA", "RGBA"):  # covered all the way, see-through parts too
                part.putalpha(255)
        out.paste(part, (x, y))
    return out


def adjust_curve(brightness: int, contrast: int) -> list[int]:
    """The lookup table for the brightness of each pixel (0 to 255), as FFmpeg's eq filter makes it:
    contrast around the middle, then a gamma curve (brighter keeps black black and white white)."""
    k, gamma = 2 ** (contrast / 100), 2 ** (brightness / 100)
    out = []
    for i in range(256):
        v = k * (i / 255 - 0.5) + 0.5
        out.append(0 if v <= 0 else 255 if v >= 1 else min(255, int(256 * v ** (1 / gamma))))
    return out


WARMTH = 0.15  # at 100: red up by this share, blue down by it


def adjusted(img: Image.Image, adjust: dict[str, int]) -> Image.Image:
    """img with brightness, contrast, saturation, warmth and sharpness changed (-100 to 100 each)."""
    alpha = _alpha(img)
    rgb = img.convert("RGB")
    b, c, sat, warm, sharp = (adjust[k] for k in options.ADJUSTMENTS)
    if b or c or sat:
        y, cb, cr = rgb.convert("YCbCr").split()
        if b or c:
            y = y.point(adjust_curve(b, c))
        if sat:
            k = 1 + sat / 100
            chroma = [max(0, min(255, round((i - 128) * k + 128))) for i in range(256)]
            cb, cr = cb.point(chroma), cr.point(chroma)
        rgb = Image.merge("YCbCr", (y, cb, cr)).convert("RGB")
    if warm:
        r, g, bl = rgb.split()
        t = warm / 100
        r = r.point(lambda v: min(255, round(v * (1 + WARMTH * t))))
        bl = bl.point(lambda v: min(255, round(v * (1 - WARMTH * t))))
        rgb = Image.merge("RGB", (r, g, bl))
    if sharp:  # on the brightness only, as video's unsharp does: no coloured fringes
        y, cb, cr = rgb.convert("YCbCr").split()
        y = y.filter(ImageFilter.UnsharpMask(radius=2, percent=round(2 * sharp), threshold=2) if sharp > 0
                     else ImageFilter.GaussianBlur(-sharp / 100 * 2))
        rgb = Image.merge("YCbCr", (y, cb, cr)).convert("RGB")
    return _with(rgb, alpha)


def auto_levels(img: Image.Image) -> list[int]:
    """A lookup table (Image.point) that stretches each colour so the darkest bit is near black and
    the brightest near white, taking out most of a colour cast. Measured once (on the first frame)
    so an animation's frames all change alike."""
    small = img.convert("RGB")
    small.thumbnail((512, 512))
    px = np.asarray(small, dtype=np.float32).reshape(-1, 3)
    grey = px @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    g_lo, g_hi = np.percentile(grey, (0.5, 99.5))
    lut = []
    for c in range(3):
        lo, hi = np.percentile(px[:, c], (0.5, 99.5))
        lo, hi = 0.7 * lo + 0.3 * g_lo, 0.7 * hi + 0.3 * g_hi  # most of the cast out, not all of it
        lo, hi = min(lo, 96.0), max(hi, 160.0)  # a dark or a flat picture isn't stretched past 2.5x
        v = (np.arange(256, dtype=np.float32) - lo) * 255 / (hi - lo)
        lut += np.clip(np.round(v), 0, 255).astype(int).tolist()
    return lut


def auto_fix(img: Image.Image, lut: list[int]) -> Image.Image:
    alpha = _alpha(img)
    rgb = img.convert("RGB").point(lut)
    return _with(rgb.filter(ImageFilter.UnsharpMask(radius=1.2, percent=50, threshold=2)), alpha)


def clean_page(img: Image.Image) -> Image.Image:
    """A photo or scan of a page made into a clean one: the paper evenly white (shadows and uneven
    light taken out), the writing dark and sharp, in grey."""
    alpha = _alpha(img)
    grey = img.convert("L")
    step = max(1, max(grey.size) // 300)  # the paper's light, measured on a small copy without the writing
    paper = grey.reduce(step).filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(3))
    paper = paper.resize(grey.size, Image.Resampling.BILINEAR)
    v = np.asarray(grey, dtype=np.float32) * 255 / np.maximum(np.asarray(paper, dtype=np.float32), 1)
    black = min(float(np.percentile(v, 0.5)), 100.0)
    v = np.clip((v - black) * 255 / (230 - black), 0, 255)  # nearly white is white: no grey speckle
    page = Image.fromarray(v.astype(np.uint8)).filter(ImageFilter.UnsharpMask(radius=1, percent=80, threshold=3))
    return _with(page, alpha)


def recolour(img: Image.Image, colour: str) -> Image.Image:
    alpha = _alpha(img)
    grey = img.convert("L")
    if colour == "sepia":
        grey = ImageOps.colorize(grey, black=(38, 26, 16), mid=(160, 122, 84), white=(255, 244, 226))
    return _with(grey, alpha)


LOOKS = WRITE - {"xbm"}  # a 1-bit picture has no colour or tones to change


def effects(frames: list[Image.Image], fmt: str) -> list[Image.Image]:
    """frames with the effects the settings ask for, scaled to max_size, in a mode fmt can store,
    every frame alike; the size the result should be is checked afterwards."""
    turn, shape, touch, colour, heat = (option(k) for k in ("rotate", "crop", "enhance", "colour", "deepfry"))
    area, boxes, mark, logo = option("area"), option("texts"), option("watermark"), option("watermark_image")
    angle, hides, marks, adjust = option("straighten"), option("hides"), option("marks"), option("adjust")
    back, up = option("background"), option("upscale")
    if back and fmt in LOOKS:  # first: the subject is found on the picture as it was taken
        from ..smart import background, need
        need("background")
        frames = [background.changed(f, back) for f in frames]
    if turn:
        frames = [turned(f, turn) for f in frames]
    if angle:
        frames = [straightened(f, float(angle)) for f in frames]
    if hides:  # on every kind of picture: what's hidden stays hidden
        frames = [hidden(f, options.items(hides)) for f in frames]
    # drawings are placed on the whole picture (they stay on what they point at), then cut as it is
    drawn = overlay.marks_layer(frames[0].size, options.items(marks)) if marks and fmt in LOOKS else None
    box = area_box(area, *frames[0].size) if area else shape_box(frames[0].size, SHAPES[shape]) if shape else None
    if box:  # chosen by hand wins over a shape from the middle
        frames = [f.crop(box) for f in frames]
        drawn = drawn.crop(box) if drawn is not None else None
    if up and fmt not in ICONS:  # the part kept, made bigger (an icon's sizes are fixed)
        from ..smart import need, upscale
        need("upscale")
        factor = int(up)
        upscale.check(frames[0].size, factor)
        frames = [upscale.bigger(f, factor) for f in frames]
        if not option("max_size"):
            expect("width", frames[0].width, f"Upscaled {factor}x")
            expect("height", frames[0].height, f"Upscaled {factor}x")
    frames = [fit(f, fmt) for f in frames]
    if adjust and fmt in LOOKS:
        frames = [adjusted(f, options.adjustments(adjust)) for f in frames]
    if touch and fmt in LOOKS:
        lut = auto_levels(frames[0]) if touch == "auto" else None
        frames = [auto_fix(f, lut) if lut else clean_page(f) for f in frames]
    if colour and fmt in LOOKS:
        frames = [recolour(f, colour) for f in frames]
    if (drawn is not None or boxes) and fmt in LOOKS:  # under the fry: they fry with the picture, as memes do
        layer = drawn if drawn is not None else Image.new("RGBA", frames[0].size, (0, 0, 0, 0))
        if layer.size != frames[0].size:  # scaled down with the picture
            layer = layer.resize(frames[0].size, Image.Resampling.LANCZOS)
        if boxes:
            layer = Image.alpha_composite(layer, overlay.text_layer(frames[0].size, options.boxes(boxes)))
        frames = [overlay.lay_over(f, layer) for f in frames]
    if heat and fmt in FRIES:
        frames = [deepfry(f, int(heat)) for f in frames]
    if (mark or logo) and fmt in LOOKS:  # last: it stays clear
        corner = option("watermark_at") or "bottom-right"
        layer = overlay.watermark_layer(frames[0].size, mark or "", corner, overlay.bright_corner(frames[0], corner),
                                        int(option("watermark_opacity") or 60),
                                        overlay.watermark_picture(logo) if logo else None)
        frames = [overlay.lay_over(f, layer) for f in frames]
    if (options.sideways(turn) or shape or area) and fmt not in ICONS:  # an icon's sizes are its own
        why = ", ".join(filter(None, (options.sideways(turn) and options.turn_words(turn),
                                      area and "Cropped by hand",
                                      shape and not area and f"Cropped to {shape_name(shape)}")))
        expect("width", frames[0].width, why)
        expect("height", frames[0].height, why)
    return [prepare(f, fmt) for f in frames]


def photo_info(src: Image.Image, out: Image.Image, fmt: str) -> dict:
    """The photo details and color profile src carries, to save with out (turned upright already)."""
    if fmt not in KEEPS_INFO:
        return {}
    info = {}
    family = lambda mode: "L" if mode in ("L", "LA", "1") else mode if mode in ("CMYK", "LAB") else "RGB"
    icc = src.info.get("icc_profile")
    if icc and family(src.mode) == family(out.mode):  # a CMYK profile is wrong for the RGB copy
        info["icc_profile"] = icc
    exif = src.getexif()
    if exif:
        exif[0x0112] = 1  # orientation: the pixels are upright now
        info["exif"] = exif.tobytes()
    return info


def _opens(path: Path) -> bool:
    try:
        with Image.open(path) as img:
            img.load()
    except ValueError:
        return False
    return True


def _heic_for_every_reader(img: Image.Image, dst: Path, opts: dict) -> None:
    """Past HEIC_READABLE bytes x265's picture is valid and FFmpeg opens it, but libde265, the
    decoder in pillow-heif and many apps, can't: give up quality until it opens."""
    start = quality = opts["quality"]
    while quality > 5 and dst.stat().st_size > HEIC_READABLE and not _opens(dst):
        quality -= 5
        img.save(dst, PIL_FORMAT["heic"], **dict(opts, quality=quality))
    if quality != start:
        note(f"Quality lowered to {quality} from {start}: at a higher quality this picture is too much data for many HEIC readers to open")


def save_image(img: Image.Image, dst: Path, fmt: str, frames: list[Image.Image] | None = None,
               duration=None, info: dict | None = None) -> None:
    if fmt in OWN_WRITERS:
        return OWN_WRITERS[fmt](img, dst)
    if fmt == "heic":
        enable_heif()
    opts = dict(SAVE_OPTS.get(fmt, {}), **(info or {}))
    if fmt in QUALITY and option("quality"):
        opts["quality"] = int(option("quality"))
    if frames and len(frames) > 1 and fmt in ANIMATED:
        opts.update(save_all=True, append_images=frames[1:])
        if fmt in ("gif", "webp", "png", "avif"):
            opts.update(duration=duration or 100, loop=0)
    if fmt == "ico":
        side = max(*img.size, 16)  # tiny images still get the smallest icon size
        opts["sizes"] = [(s, s) for s in (16, 24, 32, 48, 64, 128, 256) if s <= side]
        if img.size != (side, side):  # icons must be square (and >= 16 px): pad, don't stretch
            sq = Image.new("RGBA", (side, side), (0, 0, 0, 0))
            sq.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
            img = sq
    img.save(dst, PIL_FORMAT[fmt], **opts)
    if fmt == "heic":
        _heic_for_every_reader(img, dst, opts)


def image_notes(src: str, dst: str, opts: dict) -> list:
    out = []
    if dst in ICONS:
        out.append(loss(f"{dst.upper()} icons come in fixed square sizes ({ICONS[dst]} pixels): the picture is "
                        "padded to a square and scaled to fit", weight=0, affects=("width", "height")))
    elif opts.get("max_size"):
        out.append(loss(f"Pictures bigger than {opts['max_size']} pixels are scaled down to fit", weight=0,
                        affects=("width", "height")))
    if opts.get("background") and dst in LOOKS:
        out.append(loss(BACKGROUND_WORDS[opts["background"]] + (
            ": this format can't be see-through, so it's white" if opts["background"] == "remove" and dst in NO_ALPHA
            else ""), weight=0))
    if opts.get("upscale") and dst not in ICONS:
        out.append(loss(f"Upscaled {opts['upscale']}x by a smart tool (Real-ESRGAN): it draws in sharp edges and "
                        "texture, which can look painted up close", weight=0, affects=("width", "height")))
    if opts.get("rotate"):
        out.append(loss(options.turn_words(opts["rotate"]), weight=0,
                        affects=("width", "height") if options.sideways(opts["rotate"]) else ()))
    if opts.get("area"):
        out.append(loss("Cropped by hand: what's outside the box is cut off", weight=0, affects=("width", "height")))
    elif opts.get("crop"):
        out.append(loss(f"Cropped to {shape_name(opts['crop'])} from the middle: what's past it is cut off", weight=0,
                        affects=("width", "height")))
    if opts.get("enhance") and dst in LOOKS:
        out.append(loss("Touched up: colours and contrast stretched to the full range, and sharpened a little"
                        if opts["enhance"] == "auto" else
                        "Cleaned up as a page: the paper made evenly white and the writing dark, in grey", weight=0))
    if opts.get("colour") and dst in LOOKS:
        out.append(loss("Made black and white" if opts["colour"] == "bw" else "Made sepia, like an old photo", weight=0))
    out += editor_notes(opts, dst in LOOKS)
    if opts.get("deepfry") and dst in FRIES:
        out.append(loss("Deep fried, on purpose: colours, contrast and sharpness turned way up, with JPEG crunch",
                        weight=0))
    elif src == dst and dst in QUALITY:
        out.append(loss(f"Saving a {dst.upper()} again loses a little detail each time", weight=0))
    return out


def editor_notes(opts: dict, looks: bool = True) -> list:
    """What Straighten, Hide, Adjust, drawings, texts and the watermark did, for pictures and videos alike."""
    out = []
    if opts.get("straighten"):
        out.append(loss(f"Straightened {float(opts['straighten']):g}° and cropped in a little so no corner is empty", weight=0))
    if opts.get("hides"):
        hides = options.items(opts["hides"])
        hows = sorted({h["how"] for h in hides})
        words = {"blur": "blurred", "pixelate": "pixelated", "cover": "covered"}
        out.append(loss(f"{len(hides)} part{'s' if len(hides) != 1 else ''} hidden ({', '.join(words[h] for h in hows)})", weight=0))
    if not looks:
        return out
    if opts.get("adjust"):
        changed = ", ".join(f"{k} {v:+d}" for k, v in options.adjustments(opts["adjust"]).items() if v)
        out.append(loss(f"Adjusted: {changed}", weight=0))
    if opts.get("marks"):
        n = len(options.items(opts["marks"]))
        out.append(loss("A drawing added" if n == 1 else f"{n} drawings added", weight=0))
    if opts.get("texts"):
        n = len(options.boxes(opts["texts"]))
        out.append(loss("Text added" if n == 1 else f"{n} texts added", weight=0))
    if opts.get("watermark") or opts.get("watermark_image"):
        what = " and ".join(filter(None, (opts.get("watermark"), opts.get("watermark_image") and Path(opts["watermark_image"]).name)))
        out.append(loss(f"Watermarked: {what}", weight=0))
    return out


FRIES = WRITE - {"xbm"} - set(ICONS)  # a 1-bit picture or a tiny icon fries into mush


BACKGROUND_WORDS = {"remove": "Background taken out by a smart tool", "white": "Background made white by a smart tool",
                    "black": "Background made black by a smart tool",
                    "blur": "Background blurred by a smart tool, like portrait mode"}


def image_options(src: str, dst: str) -> tuple[str, ...]:
    return ((("quality",) if dst in QUALITY else ()) + (() if dst in ICONS or dst in ("eps", "pdf") else ("max_size",))
            + (() if dst in ICONS else ("upscale",))
            + ("rotate", "straighten", "hides", "crop", "area")
            + (("adjust", "enhance", "colour", "background", "marks", "texts", "watermark", "watermark_at",
                "watermark_image", "watermark_opacity") if dst in LOOKS else ())
            + (("deepfry",) if dst in FRIES else ()))


# a picture can also be saved as its own format: to shrink it, turn, crop or touch it up, or deep fry it
SAME = READ & WRITE - {"pdf", "eps"}


@converter(inputs=READ, outputs=WRITE - {"pdf", "eps"}, notes=image_notes, options=image_options)
# an image PDF (or EPS) holds no text or vectors to chain into docx, dxf etc.
@converter(inputs=READ, outputs={"pdf", "eps"}, terminal=True, notes=image_notes, options=image_options)
def pillow_convert(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
    if src_fmt == "heic":
        enable_heif()
    kept = take(src, src_fmt, "picture")  # decoded by the check already (one frame, not too big)
    with contextlib.nullcontext(kept) if kept is not None else Image.open(src) as img:
        # a Photoshop file's "frames" are its layers: the picture is the flattened whole
        n = 1 if src_fmt == "psd" else getattr(img, "n_frames", 1)
        if n > 1 and dst_fmt in ANIMATED:
            frames, times = [], []
            for f in ImageSequence.Iterator(img):
                frames.append(prepare(f.copy(), dst_fmt))
                times.append(f.info.get("duration") or 100)  # each frame's own time, not the first one's for all
            frames = effects(frames, dst_fmt)
            save_image(frames[0], dst, dst_fmt, frames, times)
        else:
            first = ImageOps.exif_transpose(img) or img
            out = effects([prepare(first, dst_fmt)], dst_fmt)[0]
            save_image(out, dst, dst_fmt, info=photo_info(img, out, dst_fmt))


for _fmt in sorted(SAME):  # each format to itself only (not every pair again)
    converter(inputs={_fmt}, outputs={_fmt}, same=True, notes=image_notes, options=image_options)(pillow_convert)


# ---- pictures -> SVG: trace the shapes into vector paths

@converter(inputs=READ - {"ico", "icns", "cur"}, outputs={"svg"}, cost=2, terminal=True,
           notes=lambda s, d, o: [loss("The picture is traced into flat shapes: photos and fine detail "
                                       "simplify a lot", weight=MAJOR)])
def trace_to_svg(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
    import vtracer
    if src_fmt == "heic":
        enable_heif()
    with Image.open(src) as img:
        img = ImageOps.exif_transpose(img) or img
        img = img.convert("RGBA")
        if max(img.size) > 2000:  # tracing time grows with pixels; detail beyond this is noise
            img.thumbnail((2000, 2000), Image.Resampling.LANCZOS)
        # vtracer only drops transparent areas it happens to sample, so paint them in a colour the
        # picture doesn't use, trace with shapes cut out of each other, then delete that colour's
        # shapes: what's left are real holes.
        opaque = img.getchannel("A").point(lambda a: 255 if a >= 128 else 0)
        key = _unused_color(img) if opaque.getextrema()[0] == 0 else None
        flat = Image.new("RGB", img.size, key or (255, 255, 255))
        flat.paste(img.convert("RGB"), mask=opaque)
        buf = io.BytesIO()
        flat.save(buf, "PNG")
    svg = vtracer.convert_raw_image_to_svg(buf.getvalue(), img_format="png", colormode="color",
                                           hierarchical="cutout" if key else "stacked", mode="spline",
                                           filter_speckle=4, color_precision=6, layer_difference=16,
                                           corner_threshold=60, length_threshold=4.0, splice_threshold=45,
                                           path_precision=3)
    if key:
        svg = re.sub(r'<path [^>]*fill="#{:02X}{:02X}{:02X}"[^>]*/>\s*'.format(*key), "", svg)
    dst.write_text(svg, encoding="utf-8")


def _unused_color(img: Image.Image) -> tuple[int, int, int]:
    """A colour (that survives vtracer's 6-bit colour precision) no pixel of img comes close to."""
    small = img.convert("RGB").resize((min(img.width, 256), min(img.height, 256)))
    used = {(r >> 3, g >> 3, b >> 3) for r, g, b in _pixels(small)}
    for cand in ((252, 0, 252), (0, 252, 252), (252, 252, 0), (4, 252, 128), (128, 4, 252), (252, 128, 4)):
        if (cand[0] >> 3, cand[1] >> 3, cand[2] >> 3) not in used:
            return cand
    return next((r * 8, g * 8, b * 8) for r in range(32) for g in range(32) for b in range(32)
                if (r, g, b) not in used)
