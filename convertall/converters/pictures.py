"""Camera RAW, GIMP XCF, OpenEXR, Radiance HDR and JPEG XL via pixport (no ImageMagick).

RAW photos come out at the camera's full size, developed the way LibRaw does by default (camera
white balance, automatic brightness); PNG and TIFF keep 16 bits a channel for RAW, EXR, HDR and
high-bit XCF, as ImageMagick wrote them."""

from pathlib import Path

import numpy as np

from ..errors import ConversionError
from ..losses import loss
from ..registry import converter, option, take

RAW = {"dng", "cr2", "cr3", "nef", "arw", "orf", "rw2", "raf"}
PIXPORT_IN = RAW | {"xcf", "exr", "hdr", "jxl"}
PIXPORT_OUT = {"jxl", "exr", "hdr"}
TO = {"png", "jpg", "tiff"}
JPEG_QUALITY = 92  # what ConvertAll's other JPEGs get by default


def read(src: Path, fmt: str):
    """The picture, as pixport reads it (or as the check already read it)."""
    import pixport
    kept = take(src, fmt, "pixport")
    if kept is not None:
        return kept
    try:
        return pixport.read(src, fmt)
    except pixport.UnsupportedError as exc:
        raise ConversionError(str(exc)) from None
    except pixport.PixportError as exc:
        raise ConversionError(f"{src.name} can't be read: {exc}") from None


def _fit(img):
    """img scaled down to the max_size setting (its longest side), if it's bigger."""
    size = option("max_size")
    if not size or max(img.width, img.height) <= size:
        return img
    from PIL import Image
    scale = size / max(img.width, img.height)
    w, h = max(1, round(img.width * scale)), max(1, round(img.height * scale))
    px = np.asarray(img.pixels, dtype=np.float32)
    px = px if px.ndim == 3 else px[:, :, None]
    planes = [np.asarray(Image.fromarray(np.ascontiguousarray(px[:, :, c])).resize((w, h), Image.Resampling.LANCZOS))
              for c in range(px.shape[2])]
    img.pixels = np.stack(planes, axis=2)
    return img


def _notes(src: str, dst: str, opts: dict) -> list:
    out = []
    if src in ("exr", "hdr") and dst in TO:
        out.append(loss("PNG, JPG and TIFF hold what a screen shows: brightness above white is clipped",
                        "hdr", weight=0))
    if opts.get("bit_depth") == "8" and src != "jxl" and dst in ("png", "tiff"):
        out.append(loss("8 bits a channel keeps fewer steps of colour than the picture may have", weight=0))
    if opts.get("max_size"):
        out.append(loss(f"Pictures bigger than {opts['max_size']} pixels are scaled down to fit", weight=0,
                        affects=("width", "height")))
    return out


@converter(inputs=PIXPORT_IN, outputs=TO, notes=_notes,
           options=lambda s, d: (("quality",) if d == "jpg" else ("bit_depth",) if s != "jxl" else ()) + ("max_size",))
def pixport_to_picture(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
    import pixport
    from pixport.image import upright_exif
    img = _fit(read(src, src_fmt))
    if dst_fmt == "jpg":
        pil = img.to_pil(bits=8)
        if pil.mode in ("RGBA", "LA"):  # JPEG has no transparency: on white, as the other routes do
            from PIL import Image
            bg = Image.new("RGB", pil.size, "white")
            bg.paste(pil.convert("RGBA"), mask=pil.convert("RGBA").getchannel("A"))
            pil = bg
        extra = {"exif": upright_exif(img.metadata["exif"])} if img.metadata.get("exif") else {}
        if img.metadata.get("icc"):
            extra["icc_profile"] = img.metadata["icc"]
        pil.save(dst, "JPEG", quality=int(option("quality") or JPEG_QUALITY), **extra)
    else:
        depth = option("bit_depth")
        pixport.write(img, dst, "png" if dst_fmt == "png" else "tiff", **({"bits": int(depth)} if depth else {}))


def _jxl_notes(src: str, dst: str, opts: dict) -> list:
    if dst == "jxl" and src == "jpg" and not opts.get("quality"):
        return []  # recompressed: every pixel the same, and the JPEG can be rebuilt from it
    if dst == "jxl":
        return [loss("JPEG XL at this quality loses a little detail", weight=0)]
    return []


@converter(inputs={"png", "jpg", "tiff"}, outputs=PIXPORT_OUT, notes=_jxl_notes,
           options=lambda s, d: ("quality",) if d == "jxl" else ())
def picture_to_pixport(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
    import pixport
    from PIL import Image, ImageOps
    if dst_fmt == "jxl" and src_fmt == "jpg" and not option("quality"):
        # the JPEG itself, repacked: 12 to 28% smaller (median 17% on 22 photos), nothing lost, and the
        # identical JPEG can be rebuilt from it (ImageMagick encoded it again, lossy)
        try:
            from pixport import jxl
            dst.write_bytes(jxl.from_jpeg(src.read_bytes()))
            return
        except pixport.PixportError:
            pass  # a JPEG libjxl can't repack: its pixels are encoded below
    with Image.open(src) as pil:
        pil = ImageOps.exif_transpose(pil) or pil
        sixteen = pil.mode in ("I;16", "I;16B", "I")
        if pil.mode not in ("RGB", "RGBA", "L", "LA") and not sixteen:
            pil = pil.convert("RGBA" if "A" in pil.getbands() or "transparency" in pil.info else "RGB")
        px = np.asarray(pil)
        icc = pil.info.get("icc_profile")
        exif = pil.getexif().tobytes() if pil.getexif() else None
    if px.ndim == 2:
        px = px[:, :, None]
    top = 65535.0 if sixteen else 255.0
    img = pixport.Image(px.astype(np.float32) / top, "srgb", {"bits": 16 if sixteen else 8, "icc": icc,
                                                              "exif": exif[6:] if exif and exif[:6] == b"Exif\0\0" else exif})
    if dst_fmt in ("exr", "hdr"):  # those hold light, not screen values: the colour channels are made linear
        lit = img.pixels.copy()
        colour = lit.shape[2] - 1 if lit.shape[2] in (2, 4) else lit.shape[2]
        lit[:, :, :colour] = pixport.image.srgb_to_linear(lit[:, :, :colour])
        img = pixport.Image(lit, "linear", img.metadata)
    try:
        pixport.write(img, dst, dst_fmt, **({"quality": int(option("quality"))} if dst_fmt == "jxl" and option("quality") else {}))
    except pixport.PixportError as exc:
        raise ConversionError(str(exc)) from None
