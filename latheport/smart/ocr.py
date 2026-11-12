"""Reading the text in a picture (OCR) with PaddleOCR's PP-OCR models: one finds where the lines of text
are (a map of how likely each pixel is text), the other reads each line it found. Latin script: English
and 31 more languages that write with it (French, German, Spanish, Polish, Turkish...).

Lines are found as the connected parts of the map, each made a little bigger again (the map marks the
middle of the text, not its edges), then read in batches of similar widths; every letter comes with
where it was read, so each word gets its own box (for a PDF's invisible text layer)."""

from __future__ import annotations

import itertools
import math
import os
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

from . import need, session

DETECT, READ, LETTERS = "detect.onnx", "read.onnx", "letters.txt"
FIND_SIDE = 1920    # the longest side the line finder looks at: a page's small print still finds its lines
TEXT_LIKELY = 0.3   # a pixel this likely to be text counts as text
LINE_LIKELY = 0.6   # a line found is kept when its pixels are this likely to be text on average
GROW = 1.6          # how much each line found is made bigger again (DB's unclip ratio)
LINE_HEIGHT = 48    # the reader's input height
SIDES = 0.25        # each line is read with this much of its height more on either side
BATCH = 8
MEAN, STD = np.array([0.485, 0.456, 0.406], np.float32), np.array([0.229, 0.224, 0.225], np.float32)


@dataclass
class Word:
    text: str
    box: tuple[float, float, float, float]  # x0, y0, x1, y1 in the picture's pixels


@dataclass
class Line:
    text: str
    box: tuple[float, float, float, float]
    score: float                          # how sure the reader is, 0 to 1
    words: list[Word] = field(default_factory=list)


def _letters() -> list[str]:
    with open(os.path.join(need("ocr"), LETTERS), encoding="utf-8") as f:
        chars = [line.rstrip("\r\n") for line in f]
    return ["", *chars, " "]  # 0 is CTC's blank; the reader also knows a space, last


@dataclass
class Parts:
    """The connected parts of a map, as arrays with one entry a part."""
    x0: np.ndarray
    y0: np.ndarray
    x1: np.ndarray       # past the end
    y1: np.ndarray
    pixels: np.ndarray
    likely: np.ndarray   # the map's average over the part's own pixels
    slope: np.ndarray    # how the part leans: down per pixel across (its pixels' line of best fit)


def parts(mask: np.ndarray, values: np.ndarray) -> Parts:
    """The 8-connected parts of a boolean picture, with values averaged over each."""
    h, _ = mask.shape
    edges = np.diff(np.pad(mask, ((0, 0), (1, 1))).astype(np.int8), axis=1)
    sy, sx = np.nonzero(edges == 1)
    _, ex = np.nonzero(edges == -1)  # in the same order: row by row, left to right
    labels = _join_runs(sy, sx, ex, h)
    _, comp = np.unique(labels, return_inverse=True)
    k = int(comp.max()) + 1 if len(comp) else 0
    n = (ex - sx).astype(np.float64)
    big = np.iinfo(np.int64).max
    x0 = np.full(k, big, np.int64)
    y0 = np.full(k, big, np.int64)
    x1 = np.zeros(k, np.int64)
    y1 = np.zeros(k, np.int64)
    np.minimum.at(x0, comp, sx)
    np.minimum.at(y0, comp, sy)
    np.maximum.at(x1, comp, ex)
    np.maximum.at(y1, comp, sy + 1)
    sums = np.concatenate([np.zeros((h, 1)), np.cumsum(values, axis=1, dtype=np.float64)], axis=1)
    count = np.bincount(comp, n, k)
    likely = np.bincount(comp, sums[sy, ex] - sums[sy, sx], k) / np.maximum(count, 1)
    # each run's sums of x, x squared, y and x*y, for the line of best fit through a part's pixels
    tri = lambda m: m * (m + 1) * (2 * m + 1) / 6
    fx = n * (sx + ex - 1) / 2
    fxx = tri(ex - 1.0) - tri(sx - 1.0)
    mx = np.bincount(comp, fx, k) / np.maximum(count, 1)
    my = np.bincount(comp, n * sy, k) / np.maximum(count, 1)
    vxx = np.bincount(comp, fxx, k) / np.maximum(count, 1) - mx * mx
    vxy = np.bincount(comp, fx * sy, k) / np.maximum(count, 1) - mx * my
    return Parts(x0, y0, x1, y1, count, likely, vxy / np.maximum(vxx, 1e-9))


def _join_runs(sy: np.ndarray, sx: np.ndarray, ex: np.ndarray, h: int) -> np.ndarray:
    """Which part each run of a row belongs to: runs touching in the rows above and below join (corners too)."""
    n = len(sx)
    parent = list(range(n))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    row_start = np.searchsorted(sy, np.arange(h + 1))
    for y in range(1, h):
        a, a_end = int(row_start[y - 1]), int(row_start[y])
        b, b_end = int(row_start[y]), int(row_start[y + 1])
        while a < a_end and b < b_end:
            if sx[b] <= ex[a] and ex[b] >= sx[a]:  # touching, corners too (ends are past the run)
                ra, rb = root(a), root(b)
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)
            if ex[a] < ex[b]:
                a += 1
            else:
                b += 1
    return np.array([root(i) for i in range(n)], np.int64)


def _found(img: Image.Image) -> tuple[list[tuple[float, float, float, float]], float]:
    """Where the lines of text are (boxes in the picture's pixels, in no order) and how much they lean
    (degrees clockwise, the middle of what the long lines say)."""
    w, h = img.size
    scale = min(1.0, FIND_SIDE / max(w, h))
    sw, sh = max(32, round(w * scale / 32) * 32), max(32, round(h * scale / 32) * 32)
    small = img.convert("RGB").resize((sw, sh), Image.Resampling.BILINEAR)
    x = (np.asarray(small, np.float32)[:, :, ::-1] / 255 - MEAN) / STD  # BGR, as the model learned
    prob = session("ocr", DETECT).run(None, {"x": x.transpose(2, 0, 1)[None]})[0][0, 0]
    p = parts(prob > TEXT_LIKELY, prob)
    bw, bh = p.x1 - p.x0, p.y1 - p.y0
    keep = (np.minimum(bw, bh) >= 3) & (p.likely >= LINE_LIKELY)
    boxes = []
    fx, fy = w / sw, h / sh
    for x0, y0, x1, y1 in zip(p.x0[keep], p.y0[keep], p.x1[keep], p.y1[keep]):
        bw_, bh_ = x1 - x0, y1 - y0
        grow = bw_ * bh_ * GROW / (2 * (bw_ + bh_))
        boxes.append((max(0.0, (x0 - grow) * fx), max(0.0, (y0 - grow) * fy),
                      min(float(w), (x1 + grow) * fx), min(float(h), (y1 + grow) * fy)))
    long = keep & (bw > 4 * bh)
    lean = 0.0
    if long.any():
        order = np.argsort(p.slope[long])
        weight = np.cumsum(bw[long][order])
        mid = p.slope[long][order][np.searchsorted(weight, weight[-1] / 2)]
        lean = float(np.degrees(np.arctan(mid * fy / fx)))
    return boxes, lean


def find_lines(img: Image.Image) -> list[tuple[float, float, float, float]]:
    """Where the lines of text are: boxes in the picture's pixels, in no order."""
    return _found(img)[0]


LEVEL = 0.4  # degrees: a page leaning more than this is read levelled


def read_lines(img: Image.Image, boxes: list) -> list[Line]:
    """The text in each box, with a box for each word."""
    letters = _letters()
    rgb = img.convert("RGB")
    crops = []
    for box in boxes:
        pad = (box[3] - box[1]) * SIDES  # the finder can stop short of a full stop at a line's end
        box = (max(0.0, box[0] - pad), box[1], min(float(rgb.width), box[2] + pad), box[3])
        x0, y0, x1, y1 = (round(v) for v in box)
        if x1 - x0 < 2 or y1 - y0 < 2:
            continue
        crops.append((box, rgb.crop((x0, y0, x1, y1))))
    crops.sort(key=lambda c: c[1].width / c[1].height)
    out = []
    model = session("ocr", READ)
    for k in range(0, len(crops), BATCH):
        batch = crops[k:k + BATCH]
        widths = [max(8, math.ceil(LINE_HEIGHT * c.width / c.height)) for _, c in batch]
        full = max(widths)
        x = np.zeros((len(batch), 3, LINE_HEIGHT, full), np.float32)
        for i, ((_, crop), cw) in enumerate(zip(batch, widths)):
            a = np.asarray(crop.resize((cw, LINE_HEIGHT), Image.Resampling.BILINEAR), np.float32)[:, :, ::-1]
            x[i, :, :, :cw] = ((a / 255 - 0.5) / 0.5).transpose(2, 0, 1)
        probs = model.run(None, {"x": x})[0]
        step = full / probs.shape[1]  # pixels of the input each step reads
        for i, ((box, crop), cw) in enumerate(zip(batch, widths)):
            line = _decode(probs[i], letters, box, step * crop.width / cw)
            if line is not None:
                out.append(line)
    return out


def _decode(p: np.ndarray, letters: list[str], box, step_px: float) -> Line | None:
    """CTC: the likeliest letter at each step, repeats and blanks dropped; words boxed from where their
    letters were read."""
    best = p.argmax(axis=1)
    sure = p.max(axis=1)
    chars, where, scores = [], [], []
    prev = 0
    for t, c in enumerate(best):
        if c != prev and c != 0 and c < len(letters):
            chars.append(letters[c])
            where.append(t)
            scores.append(float(sure[t]))
        prev = c
    text = "".join(chars).strip()
    if not text:
        return None
    x0, y0, x1, y1 = box
    words, start = [], None
    for i, ch in enumerate(chars + [" "]):
        if ch != " " and start is None:
            start = i
        elif ch == " " and start is not None:
            a = x0 + max(0.0, (where[start] - 0.5)) * step_px
            b = x0 + (where[i - 1] + 1.5) * step_px
            words.append(Word("".join(chars[start:i]), (a, y0, min(x1, max(b, a + 1)), y1)))
            start = None
    return Line(text, box, sum(scores) / len(scores), words)


def _rows(img: Image.Image):
    """The picture's lines in rows (see rows), read levelled when the page leans, and how to put a box
    from the levelled page back on the picture as it is (None: it wasn't levelled)."""
    boxes, lean = _found(img)
    if abs(lean) <= LEVEL or abs(lean) > MOST_LEAN:
        return rows(read_lines(img, boxes)), None
    rgb = img.convert("RGB")
    edge = np.concatenate([np.asarray(rgb)[[0, -1]].reshape(-1, 3), np.asarray(rgb)[:, [0, -1]].reshape(-1, 3)])
    paper = tuple(int(v) for v in np.median(edge, axis=0))  # the page's own colour in the corners it turns in
    level = rgb.rotate(lean, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=paper)
    turn = math.radians(lean)
    c, s = math.cos(turn), math.sin(turn)
    (w, h), (lw, lh) = img.size, level.size

    def back(box):
        xs, ys = [], []
        for px, py in ((box[0], box[1]), (box[2], box[1]), (box[0], box[3]), (box[2], box[3])):
            dx, dy = px - lw / 2, py - lh / 2
            xs.append(w / 2 + dx * c - dy * s)
            ys.append(h / 2 + dx * s + dy * c)
        return (max(0.0, min(xs)), max(0.0, min(ys)), min(float(w), max(xs)), min(float(h), max(ys)))
    return rows(read_lines(level, find_lines(level))), back


MOST_LEAN = 20  # degrees: past this it's not a page that leans, and it's read as it is


def read(img: Image.Image) -> list[Line]:
    """Every line of text in the picture, top to bottom, each row left to right; boxes on the picture as it
    is, even when it was read levelled."""
    found, back = _rows(img)
    out = [line for row in found for line in row]
    if back:
        for line in out:
            line.box = back(line.box)
            for word in line.words:
                word.box = back(word.box)
    return out


def rows(lines: list[Line]) -> list[list[Line]]:
    """Lines side by side on the page together, top to bottom; each row left to right."""
    out: list[list[Line]] = []
    for line in sorted(lines, key=lambda ln: (ln.box[1] + ln.box[3]) / 2):
        y0, y1 = line.box[1], line.box[3]
        if out:
            last = out[-1]
            ly0 = sum(ln.box[1] for ln in last) / len(last)
            ly1 = sum(ln.box[3] for ln in last) / len(last)
            if min(y1, ly1) - max(y0, ly0) > 0.5 * min(y1 - y0, ly1 - ly0):
                last.append(line)
                continue
        out.append([line])
    return [sorted(row, key=lambda ln: ln.box[0]) for row in out]


def text(img: Image.Image) -> str:
    """The picture's text as plain text: a line for each row, a blank line where a gap says a new paragraph."""
    found = _rows(img)[0]
    spans = [(min(ln.box[1] for ln in row), max(ln.box[3] for ln in row)) for row in found]
    gaps = [b[0] - a[1] for a, b in itertools.pairwise(spans)]
    usual = float(np.median(gaps)) if gaps else 0.0  # the page's own line spacing
    out = []
    for i, row in enumerate(found):
        if i:
            height = spans[i - 1][1] - spans[i - 1][0]
            if gaps[i - 1] > max(1.8 * usual, usual + 0.6 * height):  # a gap well past the usual: a new paragraph
                out.append("")
        out.append(" ".join(ln.text for ln in row))
    return "\n".join(out)
