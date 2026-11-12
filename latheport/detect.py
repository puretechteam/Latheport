"""Figure out what format a file is: extension first, magic bytes as fallback."""

from pathlib import Path

from .formats import ALIASES, FORMATS, MULTI_EXT

# puremagic extension -> our format key, for files with missing/unknown extensions
_MAGIC_MAP = {"jpeg": "jpg", "tif": "tiff", "htm": "html", "mpeg": "mpg", "tgz": "tar.gz"}

COMICS = {"cbz", "cbr", "cb7", "cbt"}
# Comic files are often renamed archives (a ".cbr" that is really a zip): trust the content
_COMIC_MAGIC = ((b"PK\x03\x04", "cbz"), (b"PK\x05\x06", "cbz"), (b"Rar!\x1a\x07", "cbr"),
                (b"7z\xbc\xaf\x27\x1c", "cb7"))


def normalize(ext: str) -> str:
    ext = ext.lower().lstrip(".")
    return ALIASES.get(ext, ext)


def detect(path: str | Path) -> str:
    path = Path(path)
    if path.is_dir():
        return "folder"
    name = path.name.lower()
    for multi in MULTI_EXT:
        if name.endswith("." + multi):
            return multi
    ext = normalize(path.suffix)
    if ext in COMICS:
        return _comic_kind(path) or ext
    if ext in FORMATS:
        return _really(path, ext) or ext
    sniffed = _sniff(path)
    if sniffed:
        return sniffed
    return ext or "bin"


# Pictures and PDFs often travel under the wrong extension (a JPEG saved as .png). Where the file
# doesn't start the way its extension's format always does, but the way another one does, the
# content wins.
def _signature(head: bytes) -> str | None:
    for magic, fmt in ((b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpg"), (b"GIF87a", "gif"),
                       (b"GIF89a", "gif"), (b"II*\x00", "tiff"), (b"MM\x00*", "tiff"), (b"%PDF-", "pdf"),
                       (b"8BPS", "psd"), (b"BM", "bmp")):
        if head.startswith(magic):
            return fmt
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"heim", b"heis"):
            return "heic"
        if brand in (b"avif", b"avis"):
            return "avif"
    return None


CHECKED = {"png", "jpg", "gif", "tiff", "pdf", "psd", "bmp", "webp", "heic", "avif"}


def _really(path: Path, ext: str) -> str | None:
    """The format a file's content shows, when its extension names another checked one."""
    if ext not in CHECKED:
        return None
    try:
        with path.open("rb") as f:
            head = f.read(16)
    except OSError:
        return None
    real = _signature(head)
    return real if real and real != ext else None


def mislabelled(path: str | Path) -> str | None:
    """A line saying what a file really is, when its extension says something else."""
    path = Path(path)
    if path.is_dir():
        return None
    ext = normalize(path.suffix)
    real = _really(path, ext) if ext in FORMATS else None
    if not real:
        return None
    return f"This .{path.suffix.lstrip('.').lower()} file is really a {real.upper()}: it was converted as one"


def _comic_kind(path: Path) -> str | None:
    try:
        with path.open("rb") as f:
            head = f.read(262)
    except OSError:
        return None
    for magic, kind in _COMIC_MAGIC:
        if head.startswith(magic):
            return kind
    return "cbt" if head[257:262] == b"ustar" else None


# Signatures puremagic doesn't know (or confuses), checked first
_SIGNATURES = (
    (b"\xc5\xd0\xd3\xc6", "eps"), (b"AT&TFORM", "djvu"), (b"\x04\x22\x4d\x18", "lz4"), (b"\x1f\x9d", "z"),
    (b"070701", "cpio"), (b"070702", "cpio"), (b"070707", "cpio"), (b"xar!", "xar"), (b"KDMV", "vmdk"),
    (b"QFI\xfb", "qcow2"), (b"conectix", "vhd"), (b"vhdxfile", "vhdx"), (b"ITSF", "chm"),
    (b"<<< Oracle VM VirtualBox Disk Image >>>", "vdi"), (b"!<arch>\n", "ar"), (b"hsqs", "squashfs"),
)
# Text formats recognised by what they start with (after any BOM and blank space)
_TEXT_STARTS = ((b"BEGIN:VCARD", "vcf"), (b"BEGIN:VCALENDAR", "ics"), (b"WEBVTT", "vtt"))


MSI_CLSIDS = {"000C1084-0000-0000-C000-000000000046": "msi", "000C1086-0000-0000-C000-000000000046": "msp"}


def _subtype(path: Path, fmt: str, head: bytes) -> str:
    """A more exact kind for a signature several formats share: a deb is an ar archive whose
    first member is debian-binary, a macOS pkg is a xar archive with a Payload."""
    if fmt == "ar" and head[8:24].startswith(b"debian-binary"):
        return "deb"
    if fmt == "xar":
        import packport
        try:
            if packport.detect(path) == "pkg":
                return "pkg"
        except Exception:  # noqa: BLE001, S110 - damaged: it's still a xar archive
            pass
    return fmt


def _deeper(path: Path, head: bytes) -> str | None:
    """Signatures that aren't at the start: APFS at 32, HFS+ at 1024, UDF at 32 KB, and the class
    of an OLE file (Windows Installer packages and patches, rather than Word documents)."""
    if head[32:36] == b"NXSB":
        return "apfs"
    with path.open("rb") as f:
        f.seek(1024)
        if f.read(2) in (b"H+", b"HX"):
            return "hfs"
        f.seek(16 * 2048)
        vrs = f.read(6 * 2048)
    if vrs[1:6] == b"BEA01" and any(vrs[k * 2048 + 1:k * 2048 + 6] in (b"NSR02", b"NSR03") for k in range(6)):
        return "udf"
    if head.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
        import olefile
        try:
            with olefile.OleFileIO(str(path)) as ole:
                return MSI_CLSIDS.get((ole.root.clsid or "").upper())
        except Exception:  # noqa: BLE001 - not an OLE file after all
            return None
    return None


def _is_man(path: Path, text: bytes) -> bool:
    """A man page named by its section (ls.1, printf.3): after its comment lines it starts with .TH
    (or .so, an alias page). A rotated log (syslog.1) doesn't."""
    if not (len(path.suffix) == 2 and path.suffix[1:] in "123456789"):
        return False
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith((b'.\\"', b"'\\\"", b'\\"')):
            continue
        return line.startswith((b".TH ", b".so ", b".Dd "))
    return False


def _sniff(path: Path) -> str | None:
    try:
        with path.open("rb") as f:
            head = f.read(512)
        if head[128:132] == b"DICM":  # DICOM files often have no extension at all
            return "dcm"
        if head.startswith(b"SQLite format 3\x00"):  # .db files are often SQLite
            return "sqlite"
        if head.startswith(b"%!PS"):
            return "eps" if b"EPSF" in head.split(b"\n", 1)[0] else "ps"
        for magic, fmt in _SIGNATURES:
            if head.startswith(magic):
                return _subtype(path, fmt, head)
        deeper = _deeper(path, head)
        if deeper:
            return deeper
        text = head.lstrip(b"\xef\xbb\xbf \t\r\n")
        if _is_man(path, text):
            return "man"
        for start, fmt in _TEXT_STARTS:
            if text.upper().startswith(start):
                return fmt
        if text.startswith(b"<"):  # <?xml or a bare root tag
            for tag, fmt in ((b"<gpx", "gpx"), (b"<kml", "kml"), (b"<tt ", "ttml"), (b"<tt>", "ttml")):
                if tag in head:
                    return fmt
    except OSError:
        return None
    try:
        import puremagic
        for match in puremagic.magic_file(str(path)):
            ext = normalize(_MAGIC_MAP.get(match.extension.lstrip("."), match.extension))
            if ext in FORMATS:
                return ext
    except Exception:  # noqa: BLE001, S110 - puremagic chokes on some files; then it's unknown
        pass
    return None


def strip_ext(path: Path) -> str:
    """'photos.tar.gz' -> 'photos', 'song.mp3' -> 'song'."""
    name = path.name
    if path.is_dir():
        return name
    low = name.lower()
    for multi in MULTI_EXT:
        if low.endswith("." + multi):
            return name[: -len(multi) - 1]
    return path.stem or name
