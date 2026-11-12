"""A picture's background taken out (made see-through) or changed: white, black, or the picture's own
background blurred, like a phone's portrait mode. BiRefNet (lite, at 512 x 512) finds the subject: a map of
how much each pixel belongs to it. Made as big as the picture, its edges are fitted to the picture's own
edges with a guided filter (He et al.), and it becomes the picture's alpha, soft where the edge is soft.

Chosen over IS-Net, which draws a pixel or two sharper edges but on 7 of 15 test pictures took in something
of the background touching the subject (IoU 0.75 to 0.87, against 0.99 for BiRefNet on all 15).

Where an edge is part subject and part background, its colour is a mix of both: the background's share
is taken out again (from the background's colour nearby), so the subject doesn't keep a fringe of the
old background on its new one."""

from __future__ import annotations

import hashlib

import numpy as np
from PIL import Image

from . import session

MODEL = "birefnet.onnx"
SIDE = 512      # the model looks at the picture at this size
MEAN = np.array([0.485, 0.456, 0.406], np.float32)
STD = np.array([0.229, 0.224, 0.225], np.float32)
FIT_SIDE = 2048  # the edges are fitted on the picture at most this big (then made as big as it is)
FIT_EPS = 1e-5   # the guided filter's smoothing: lower follows the picture's edges more closely
FIT_RADIUS = 3   # its reach, in the model's pixels (swept on 12 test pictures: 3 and 1e-5 best, 28% less error)
HOW = ("remove", "white", "black", "blur")

_masks: dict[bytes, np.ndarray] = {}  # the last few pictures' maps: the editor's preview, then its conversion
KEEP = 4


def subject(img: Image.Image) -> np.ndarray:
    """How much each pixel is the subject, 0 to 1 (float32, the picture's height x width)."""
    rgb = img.convert("RGB")
    small = rgb.resize((SIDE, SIDE), Image.Resampling.BILINEAR)
    key = hashlib.sha1(small.tobytes() + repr(img.size).encode()).digest()
    if key not in _masks:
        _masks[key] = fitted(rgb, found(small))
        while len(_masks) > KEEP:
            del _masks[next(iter(_masks))]
    return _masks[key].copy()


def found(small: Image.Image) -> np.ndarray:
    """The model's own map of the subject, SIDE x SIDE, 0 to 1, for the picture made that size."""
    model = session("background", MODEL, lean=True)
    x = (np.asarray(small.convert("RGB"), np.float32) / 255 - MEAN) / STD
    out = model.run(None, {model.get_inputs()[0].name: x.transpose(2, 0, 1)[None].astype(np.float32)})[0][0, 0]
    return (1 / (1 + np.exp(-np.clip(out.astype(np.float64), -60, 60)))).astype(np.float32)  # it gives logits


def _box_mean(a: np.ndarray, r: int) -> np.ndarray:
    n = 2 * r + 1
    c = np.cumsum(np.cumsum(np.pad(a, ((r + 1, r), (r + 1, r)), mode="edge"), 0, dtype=np.float64), 1)
    return ((c[n:, n:] - c[:-n, n:] - c[n:, :-n] + c[:-n, :-n]) / (n * n)).astype(np.float32)


def fitted(rgb: Image.Image, prob: np.ndarray) -> np.ndarray:
    """The model's map made as big as the picture, its edges fitted to the picture's (a guided filter on the
    picture's brightness): sharper than the map enlarged alone (its error at the edges 28% lower on the tests)."""
    w, h = rgb.size
    scale = min(1.0, FIT_SIDE / max(w, h))
    ws, hs = max(1, round(w * scale)), max(1, round(h * scale))
    grey = np.asarray(rgb.convert("L").resize((ws, hs), Image.Resampling.BILINEAR), np.float32) / 255
    p = np.asarray(Image.fromarray(prob, "F").resize((ws, hs), Image.Resampling.BILINEAR), np.float32)
    r = max(2, round(max(ws, hs) / SIDE * FIT_RADIUS))
    mi, mp = _box_mean(grey, r), _box_mean(p, r)
    a = (_box_mean(grey * p, r) - mi * mp) / (_box_mean(grey * grey, r) - mi * mi + FIT_EPS)
    b = mp - a * mi
    out = np.clip(_box_mean(a, r) * grey + _box_mean(b, r), 0, 1)
    if (ws, hs) != (w, h):
        out = np.asarray(Image.fromarray(out, "F").resize((w, h), Image.Resampling.BILINEAR), np.float32)
    return out


def box_blur(a: np.ndarray, radius: int) -> np.ndarray:
    """a (2D, float) blurred: a box this many pixels each way, three times over (close to a Gaussian)."""
    r = max(1, int(radius))
    for _ in range(3):
        for axis in (0, 1):
            pad = [(0, 0), (0, 0)]
            pad[axis] = (r + 1, r)
            c = np.cumsum(np.pad(a, pad, mode="edge"), axis=axis, dtype=np.float64)
            hi = np.take(c, np.arange(2 * r + 1, c.shape[axis]), axis=axis)
            lo = np.take(c, np.arange(0, c.shape[axis] - 2 * r - 1), axis=axis)
            a = ((hi - lo) / (2 * r + 1)).astype(np.float32)
    return a


def _spread(rgb: np.ndarray, weight: np.ndarray, radius: float) -> np.ndarray:
    """The colours where weight is high, spread out into the rest (a blur that only takes from there)."""
    h, w = weight.shape
    scale = max(1, max(h, w) // 512)  # blurred on a small copy: the result is smooth anyway
    sw, sh = max(1, w // scale), max(1, h // scale)

    def blur(a: np.ndarray) -> np.ndarray:
        small = np.asarray(Image.fromarray(a.astype(np.float32), "F").resize((sw, sh), Image.Resampling.BOX))
        small = box_blur(small, round(max(1.0, radius / scale) * 0.8))
        return np.asarray(Image.fromarray(small, "F").resize((w, h), Image.Resampling.BILINEAR), np.float32)

    total = blur(weight)
    return np.stack([blur(rgb[:, :, c] * weight) / np.maximum(total, 1e-4) for c in range(3)], axis=2)


def cut_out(img: Image.Image, alpha: np.ndarray | None = None) -> Image.Image:
    """The subject alone, on see-through (RGBA): the edges' colour freed of the old background."""
    a = subject(img) if alpha is None else alpha
    rgb = np.asarray(img.convert("RGB"), np.float32)
    edge = (a > 0.02) & (a < 0.98)
    if edge.any():
        back = _spread(rgb, (a < 0.1).astype(np.float32), max(4.0, max(a.shape) / 100))
        ae = np.maximum(a[edge], 0.05)[:, None]
        rgb = rgb.copy()
        rgb[edge] = np.clip((rgb[edge] - (1 - ae) * back[edge]) / ae, 0, 255)
    keep = _alpha(img)
    if keep is not None:  # a picture see-through already stays so where it was
        a = a * keep
    out = np.dstack([rgb, a * 255]).round().astype(np.uint8)
    return Image.fromarray(out, "RGBA")


def _alpha(img: Image.Image) -> np.ndarray | None:
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        return np.asarray(img.convert("RGBA").getchannel("A"), np.float32) / 255
    return None


def changed(img: Image.Image, how: str) -> Image.Image:
    """The picture with its background taken out (remove), made white or black, or blurred."""
    a = subject(img)
    if how == "blur":
        rgb = np.asarray(img.convert("RGB"), np.float32)
        back = _spread(rgb, 1 - a, max(6.0, np.hypot(*a.shape) / 60))  # the subject doesn't bleed into it
        out = rgb * a[:, :, None] + back * (1 - a[:, :, None])
        result = Image.fromarray(out.round().astype(np.uint8), "RGB")
        keep = _alpha(img)
        if keep is not None:
            result.putalpha(Image.fromarray((keep * 255).round().astype(np.uint8), "L"))
        return result
    cut = cut_out(img, a)
    if how == "remove":
        return cut
    plate = Image.new("RGBA", cut.size, (255, 255, 255, 255) if how == "white" else (0, 0, 0, 255))
    return Image.alpha_composite(plate, cut).convert("RGB")
