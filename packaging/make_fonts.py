"""The fonts text is drawn with (latheport/assets/fonts), from Google Fonts' originals.

    python packaging/make_fonts.py <folder with the downloads>

Download into that folder, from https://raw.githubusercontent.com/google/fonts/main/ofl/:
inter/Inter[opsz,wght].ttf, lora/Lora[wght].ttf, anton/Anton-Regular.ttf, caveat/Caveat[wght].ttf,
and each family's OFL.txt saved as OFL-<family>.txt. All are under the SIL Open Font License 1.1.

Inter and Caveat are cut down to the one weight Latheport draws with (every letter kept), which
makes them a fraction of the size; they have no Reserved Font Name, so they keep their names.
Lora has one, so it's copied as it is (its weight is picked when drawing). Anton is one weight.
"""
import shutil
import sys
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

ROOT = Path(__file__).resolve().parent.parent
FONTS = ROOT / "latheport" / "assets" / "fonts"
LICENSES = ROOT / "packaging" / "licenses"
CUT = {"Inter[opsz,wght].ttf": ("Inter-SemiBold.ttf", {"wght": 600, "opsz": 32}),
       "Caveat[wght].ttf": ("Caveat-SemiBold.ttf", {"wght": 600})}
AS_IS = ("Lora[wght].ttf", "Anton-Regular.ttf")


def main(src: Path) -> None:
    FONTS.mkdir(parents=True, exist_ok=True)
    for name, (out, axes) in CUT.items():
        font = instantiateVariableFont(TTFont(src / name), axes)
        font.save(FONTS / out)
    for name in AS_IS:
        shutil.copyfile(src / name, FONTS / name.replace("[wght]", "-Variable"))
    for family in ("inter", "lora", "anton", "caveat"):
        shutil.copyfile(src / f"OFL-{family}.txt", LICENSES / f"{family.capitalize()}-OFL.txt")
    for f in sorted(FONTS.iterdir()):
        print(f"{f.name}: {f.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
