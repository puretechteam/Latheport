"""Reading text files whose encoding nobody wrote down."""

from pathlib import Path


def _utf16_without_bom(data: bytes) -> str | None:
    """"utf-16-le" or "-be" when data looks like UTF-16 with no BOM (some Excel and Windows exports): text
    is never full of NUL bytes, and UTF-16 of mostly Latin text has one in every other byte."""
    head = data[:4096]
    pairs = len(head) // 2
    if pairs < 2:
        return None
    odd, even = head[1::2].count(0), head[0::2].count(0)
    if odd >= 0.4 * pairs and even <= 0.05 * pairs:
        return "utf-16-le"
    if even >= 0.4 * pairs and odd <= 0.05 * pairs:
        return "utf-16-be"
    return None


def decode(data: bytes) -> str:
    """UTF-8 (with or without a BOM) or UTF-16 (with a BOM, or plainly so without one), else Windows-1252,
    which is what older files saved on Windows (subtitles, CSV exports, web pages) usually are. Never fails."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    if wide := _utf16_without_bom(data):
        return data.decode(wide, "replace")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", "replace")


def read_text(path: Path) -> str:
    return decode(Path(path).read_bytes())
