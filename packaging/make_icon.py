"""Draw the Latheport icon (a lathe's three-jaw chuck, turning, on a rounded square) -> latheport/assets/.

Drawn at 4096 px and scaled down, so the edges stay smooth at every size Windows asks for (16 to 256)."""

import math
from pathlib import Path

from PIL import Image, ImageDraw

S = 1024
K = 4  # drawn K times bigger, then scaled down
ASSETS = Path(__file__).resolve().parent.parent / "latheport" / "assets"
PALETTES = {"copper": ((235, 125, 60), (176, 58, 46)), "steel": ((56, 72, 92), (24, 32, 44))}
PALETTE = "copper"


def _p(*pts):
    return [(x * K, y * K) for x, y in pts]


def _arrow(d, c, r, a0, a1, width, head) -> None:
    """An arc around the centre (degrees, on screen) ending in an arrowhead: the chuck turns."""
    pts = [(c + r * math.cos(math.radians(a0 + (a1 - a0) * i / 60)), c + r * math.sin(math.radians(a0 + (a1 - a0) * i / 60)))
           for i in range(61)]
    d.line(_p(*pts), fill="white", width=width * K, joint="curve")
    (x0, y0), (x1, y1) = pts[-2], pts[-1]
    length = math.hypot(x1 - x0, y1 - y0)
    tx, ty = (x1 - x0) / length, (y1 - y0) / length
    nx, ny = -ty, tx
    d.polygon(_p((x1 + tx * head, y1 + ty * head), (x1 + nx * head * 0.75, y1 + ny * head * 0.75),
                 (x1 - nx * head * 0.75, y1 - ny * head * 0.75)), fill="white")


def draw(palette: str = PALETTE) -> Image.Image:
    top, bottom = PALETTES[palette]
    n = S * K
    grad = Image.new("RGBA", (n, n))
    gd = ImageDraw.Draw(grad)
    for y in range(n):
        t = y / (n - 1)
        gd.line([(0, y), (n, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,))
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    mask = Image.new("L", (n, n), 0)
    ImageDraw.Draw(mask).rounded_rectangle([40 * K, 40 * K, (S - 40) * K, (S - 40) * K], radius=220 * K, fill=255)
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)
    c, r = 512, 290
    d.ellipse(_p((c - r, c - r), (c + r, c + r)), fill="white")  # the chuck's face
    d.ellipse(_p((c - 70, c - 70), (c + 70, c + 70)), fill=bottom)  # its bore
    for k in range(3):  # three stepped jaws, 120 degrees apart
        a = math.radians(-90 + 120 * k)
        ux, uy = math.cos(a), math.sin(a)
        vx, vy = -uy, ux
        for r0, r1, w in ((100, 165, 34), (165, 225, 50), (225, 262, 66)):
            d.polygon(_p((c + ux * r0 + vx * w, c + uy * r0 + vy * w), (c + ux * r1 + vx * w, c + uy * r1 + vy * w),
                         (c + ux * r1 - vx * w, c + uy * r1 - vy * w), (c + ux * r0 - vx * w, c + uy * r0 - vy * w)),
                      fill=bottom)
    _arrow(d, c, 385, 205, 330, 44, 74)
    _arrow(d, c, 385, 25, 150, 44, 74)
    return img.resize((S, S), Image.LANCZOS)


if __name__ == "__main__":
    ASSETS.mkdir(parents=True, exist_ok=True)
    icon = draw()
    icon.resize((256, 256), Image.LANCZOS).save(ASSETS / "icon.png")
    icon.save(ASSETS / "icon.ico", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    print("wrote", ASSETS)
