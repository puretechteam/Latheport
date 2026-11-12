"""A picture made 2 or 4 times bigger with Real-ESRGAN, which draws in the detail plain resizing can't:
sharp edges and texture instead of blur. The model works on 128-pixel squares, so the picture goes through
in overlapping tiles and only each tile's middle is kept, so no seams show. 2x is the 4x picture scaled down
by half, a tile at a time, so memory follows the size asked for: 3 bytes a pixel of the result."""

from __future__ import annotations

import numpy as np
from PIL import Image

from .. import cancel
from ..errors import ConversionError
from . import session

MODEL = "real_esrgan_general_x4v3.onnx"
TILE = 128      # the model's input: a square of this many pixels
OVERLAP = 12    # pixels each tile shares with its neighbours (their middles are kept)
LARGEST = 80_000_000  # pixels: the most an upscaled picture may have


def tiles(length: int) -> list[int]:
    """Where the tiles start along one side: TILE apart less the overlap, the last one flush with the end."""
    if length <= TILE:
        return [0]
    step = TILE - 2 * OVERLAP
    starts = list(range(0, length - TILE, step))
    return starts + [length - TILE]


def check(size: tuple[int, int], factor: int) -> None:
    w, h = size
    if w * h * factor * factor > LARGEST:
        raise ConversionError(f"Too big to upscale {factor}x: {w} x {h} would become {w * factor} x {h * factor} "
                              f"({w * h * factor * factor / 1e6:.0f} megapixels; {LARGEST / 1e6:.0f} at most). "
                              "Choose 2x or a smaller picture")


def bigger(img: Image.Image, factor: int, progress=None) -> Image.Image:
    """img (any mode) factor (2 or 4) times bigger; see-through stays see-through, its edge made bigger smoothly."""
    check(img.size, factor)
    see_through = img.mode in ("RGBA", "LA", "PA", "RGBa", "La") or "transparency" in img.info
    alpha = img.convert("RGBA").getchannel("A") if see_through else None  # a palette picture has no "A" band
    rgb = np.asarray(img.convert("RGB"), np.float32) / 255
    h, w = rgb.shape[:2]
    pad_h, pad_w = max(0, TILE - h), max(0, TILE - w)
    if pad_h or pad_w:  # a picture smaller than a tile: its edge repeated to fill one
        rgb = np.pad(rgb, ((0, pad_h), (0, pad_w), (0, 0)), mode="edge")
    H, W = rgb.shape[:2]
    f = factor
    out = np.zeros((H * f, W * f, 3), np.uint8)
    model = session("upscale", MODEL)
    name = model.get_inputs()[0].name
    ys, xs = tiles(H), tiles(W)
    done, total = 0, len(ys) * len(xs)
    for y in ys:
        for x in xs:
            cancel.check()  # a tile at a time: a 400-tile picture stops within moments of Cancel
            tile = rgb[y:y + TILE, x:x + TILE].transpose(2, 0, 1)[None]
            got = model.run(None, {name: np.ascontiguousarray(tile)})[0][0].transpose(1, 2, 0)
            got = (np.clip(got, 0, 1) * 255).round().astype(np.uint8)
            if f == 2:  # the whole tile scaled, then its middle kept: the same pixels as scaling the whole picture
                got = np.asarray(Image.fromarray(got, "RGB").resize((TILE * 2, TILE * 2), Image.Resampling.LANCZOS))
            # keep the tile's middle: its edges were drawn without what lies past them
            top = 0 if y == 0 else OVERLAP
            left = 0 if x == 0 else OVERLAP
            bottom = TILE if y + TILE >= H else TILE - OVERLAP
            right = TILE if x + TILE >= W else TILE - OVERLAP
            out[(y + top) * f:(y + bottom) * f, (x + left) * f:(x + right) * f] = got[top * f:bottom * f, left * f:right * f]
            done += 1
            if progress:
                progress(done, total)
    result = Image.fromarray(np.ascontiguousarray(out[:h * f, :w * f]), "RGB")
    if alpha is not None:
        result.putalpha(alpha.resize(result.size, Image.Resampling.LANCZOS))
    return result
