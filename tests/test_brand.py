"""Latheport's look: the copper of its icon (a lathe's chuck) is the window's accent, readable in both themes (WCAG
contrast), and the icon file has every size Windows asks for."""

from pathlib import Path

from PIL import Image

from latheport import gui
from latheport.editor import look

ASSETS = Path(gui.__file__).parent / "assets"


def _lum(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _copper(hex_colour: str) -> bool:
    r, g, b = (int(hex_colour.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    return r > g > b and r - b > 100  # warm: red over green over blue, well apart


def test_the_accent_is_the_icons_copper_and_readable():
    for theme in (gui.LIGHT, gui.DARK, look.INK):
        assert _copper(theme["accent"]), theme["accent"]
        assert contrast(theme["accent_text"], theme["accent"]) >= 4.5  # the words on a button (WCAG AA)
    assert contrast(gui.LIGHT["accent"], gui.LIGHT["accent_soft"]) >= 4.5  # a picked preset's words
    assert contrast(gui.LIGHT["accent"], gui.LIGHT["surface"]) >= 4.5
    assert contrast(gui.DARK["accent"], gui.DARK["surface"]) >= 4.5


def test_the_icon_has_every_size_and_is_copper():
    with Image.open(ASSETS / "icon.ico") as ico:
        assert set(ico.info["sizes"]) >= {(16, 16), (24, 24), (32, 32), (48, 48), (256, 256)}
    with Image.open(ASSETS / "icon.png") as png:
        png = png.convert("RGBA")
        r, g, b, _ = png.getpixel((18, 128))  # the tile beside the chuck
    assert r > 200 and g < 140 and b < 90, (r, g, b)


def test_no_indigo_left():
    old = ("#6366f1", "#818cf8", "#eef2ff", "#26264a", "#30315a")
    root = Path(gui.__file__).parent
    for p in root.rglob("*.py"):
        text = p.read_text(encoding="utf-8").lower()
        assert not any(c in text for c in old), p.name
