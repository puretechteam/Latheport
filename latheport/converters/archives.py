"""Compress anything, extract any archive, and repack archives into other archive types."""

import bz2
import contextlib
import functools
import gzip
import io
import lzma
import os
import re
import shutil
import stat
import struct
import tarfile
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path

try:
    from compression import zstd  # Python 3.14+
except ImportError:
    from backports import zstd

from .. import cancel, engines
from ..detect import COMICS, strip_ext
from ..errors import ConversionError
from ..formats import category
from ..registry import Note, converter, note
from ..safety import Budget, Namer, Refused, _size, copy_capped, safe_name, sweep


class _BrotliFile(io.RawIOBase):
    """brotli compresses and decompresses chunks but has no file object; this is one."""
    OUT_LIMIT = 1 << 20  # never inflate more than this per call: a tiny .br can unpack to gigabytes

    def __init__(self, path, mode: str = "rb", quality: int = 11):
        import brotli
        super().__init__()
        self._writing = "w" in mode
        self._f = open(path, "wb" if self._writing else "rb")  # noqa: SIM115 - closed by close()
        self._codec = brotli.Compressor(quality=quality) if self._writing else brotli.Decompressor()
        self._buf = bytearray()

    def readable(self) -> bool:
        return not self._writing

    def writable(self) -> bool:
        return self._writing

    def write(self, b) -> int:
        self._f.write(self._codec.process(bytes(b)))
        return len(b)

    def read(self, n: int = -1) -> bytes:
        import brotli
        want = n if n >= 0 else 1 << 62
        try:
            while len(self._buf) < want:
                if self._codec.can_accept_more_data():
                    if self._codec.is_finished():
                        break
                    chunk = self._f.read(1 << 16)
                    if not chunk:
                        raise ConversionError("The Brotli file is cut short")
                else:
                    chunk = b""
                self._buf += self._codec.process(chunk, output_buffer_limit=self.OUT_LIMIT)
        except brotli.error as exc:
            raise ConversionError(f"Not a valid Brotli file: {exc}") from exc
        out, self._buf = bytes(self._buf[:want]), self._buf[want:]
        return out

    def close(self) -> None:
        if not self.closed:
            try:
                if self._writing:
                    self._f.write(self._codec.finish())
            finally:
                self._f.close()
        super().close()


def _lz4_open(path, mode="rb"):
    import lz4.frame
    return lz4.frame.open(path, mode)


def _lzma_alone_open(path, mode="rb"):
    return lzma.open(path, mode, format=lzma.FORMAT_ALONE)  # .lzma is the old LZMA-alone format


class _ZReader(io.RawIOBase):
    """Unix compress (.Z) as a readable file, decoded by packport chunk by chunk."""

    def __init__(self, path):
        import packport
        super().__init__()
        self._f = open(path, "rb")  # noqa: SIM115 - closed by close()
        self._codec = packport.ZDecompressor()
        self._buf = bytearray()
        self._done = False

    def readable(self) -> bool:
        return True

    def read(self, n: int = -1) -> bytes:
        import packport
        want = n if n >= 0 else 1 << 62
        try:
            while len(self._buf) < want and not self._done:
                chunk = self._f.read(1 << 16)
                if chunk:
                    self._buf += self._codec.decompress(chunk)
                else:
                    self._buf += self._codec.flush()
                    self._done = True
        except packport.PackportError as exc:
            raise ConversionError(f"Not a valid .Z file: {exc}") from exc
        out, self._buf = bytes(self._buf[:want]), self._buf[want:]
        return out

    def close(self) -> None:
        if not self.closed:
            self._f.close()
        super().close()


def _z_open(path, mode="rb"):
    if "w" in mode:
        import packport
        return packport.ZWriter(path)
    return _ZReader(path)


# Single-file compressors: name -> open(path, mode)
STREAM = {"gz": gzip.open, "bz2": bz2.open, "xz": lzma.open, "zst": zstd.open, "lz4": _lz4_open,
          "br": _BrotliFile, "lzma": _lzma_alone_open, "z": _z_open}
TAR_MODE = {"tar": "w", "tar.gz": "w:gz", "tar.bz2": "w:bz2", "tar.xz": "w:xz"}
TAR_STREAM = {"tar.zst": "zst", "tar.lz4": "lz4"}  # tar inside a compressor tarfile doesn't know
# Zip files under another name (apps, add-ons, packages): extracted and repacked as a ZIP
ZIP_BASED = {"zipx", "jar", "war", "ear", "apk", "aab", "xpi", "vsix", "nupkg", "whl", "appx", "msix",
             "ipa", "crx"}

# Read by packport (pure Python): RAR (4 and 5, solid, volumes), discs (ISO 9660, UDF), installers
# and packages, old archivers, disk images and the file systems on them, DMG
PACKPORT_EXTRACT = {"rar", "cbr", "r00", "iso", "udf", "cab", "arj", "lzh", "wim", "rpm", "deb", "xar", "pkg",
                    "ar", "001", "squashfs", "dmg", "vhd", "vhdx", "vmdk", "vdi", "qcow2", "img", "apfs", "hfs"}
BOOKPORT_EXTRACT = {"chm", "lit"}  # the files inside a help file or a Microsoft Reader book
MSI_EXTRACT = {"msi", "msp"}  # Windows Installer: OLE streams, with the CABs inside unpacked
NATIVE_EXTRACT = ({"zip", "cbz", "7z", "cb7", "tar", "cbt", "cpio"} | set(TAR_MODE) | set(TAR_STREAM)
                  | set(STREAM) | ZIP_BASED | PACKPORT_EXTRACT | BOOKPORT_EXTRACT | MSI_EXTRACT)

COMPRESS_OUT = {"zip", "7z", "iso", "rar", "cpio", "wim"} | set(TAR_MODE) | set(TAR_STREAM) | set(STREAM)
FOLDER_OUT = COMPRESS_OUT - set(STREAM)
# A comic is an archive of page images; this is the container each one is meant to be
COMIC_CONTAINER = {"cbz": "zip", "cbr": "rar", "cb7": "7z", "cbt": "tar"}
CONTAINER = COMIC_CONTAINER | {f: "zip" for f in ZIP_BASED}


def compress(src: Path, dst: Path, fmt: str) -> None:
    """Pack a file or folder (folder name kept as the top-level entry; comics keep pages at the top)."""
    from . import sevenz
    arcname = src.name
    if fmt in COMICS:
        fmt = COMIC_CONTAINER[fmt]
        if src.is_dir():
            arcname = ""
    if fmt == "zip":
        # strict_timestamps=False: a file dated before 1980 (from an old tar or RPM) gets 1980, ZIP's first date
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=9, strict_timestamps=False) as zf:
            if src.is_dir():
                if arcname:
                    zf.write(src, arcname)
                for p in sorted(src.rglob("*")):
                    zf.write(p, Path(arcname) / p.relative_to(src))
            else:
                zf.write(src, arcname)
    elif fmt == "7z":  # packed on several cores (py7zr's one took a minute for 190 MB)
        if src.is_dir():
            inside = [(p.relative_to(src).as_posix(), p) for p in sorted(src.rglob("*"))]
            items = [(arcname, src)] + [(f"{arcname}/{n}", p) for n, p in inside] if arcname else inside
        else:
            items = [(arcname, src)]
        sevenz.write(dst, items)
    elif fmt in TAR_MODE:
        with tarfile.open(dst, TAR_MODE[fmt]) as tf:
            _tar_add(tf, src, arcname)
    elif fmt in TAR_STREAM:
        with STREAM[TAR_STREAM[fmt]](dst, "wb") as f, tarfile.open(fileobj=f, mode="w|") as tf:
            _tar_add(tf, src, arcname)
    elif fmt in STREAM:
        if src.is_dir():
            raise ConversionError(f"{fmt} holds a single file; use zip, 7z or tar for folders")
        opener = STREAM[fmt]
        if fmt == "br" and src.stat().st_size > 64 << 20:  # quality 11 runs at ~1 MB/s
            opener = functools.partial(_BrotliFile, quality=9)
        with src.open("rb") as fin, opener(dst, "wb") as fout:
            shutil.copyfileobj(fin, fout, 1 << 20)
    elif fmt == "iso":
        _write_iso(src, dst)
    elif fmt == "rar":
        _write_rar(src, dst, arcname)
    elif fmt == "cpio":
        import packport
        _packport(packport.write_cpio, dst, src, arcname)
    elif fmt == "wim":
        import packport
        _packport(packport.write_wim, dst, src, arcname)
    else:
        raise ConversionError(f"Can't create {fmt}")


def _tar_add(tf: tarfile.TarFile, src: Path, arcname: str) -> None:
    if arcname:
        tf.add(src, arcname)
    else:
        for p in sorted(src.iterdir()):
            tf.add(p, p.name)


def _packport(write, dst: Path, src: Path, arcname: str) -> None:
    import packport
    try:
        write(dst, packport.tree(src) if src.is_dir() and not arcname else [src])
    except packport.PackportError as exc:
        raise ConversionError(str(exc)) from None


def _write_rar(src: Path, dst: Path, arcname: str) -> None:
    """WinRAR's own Rar.exe when it's installed (real compression); otherwise packport, which
    writes stored (uncompressed) RAR 4. RAR's compression method is proprietary."""
    rar = engines.find("winrar")
    if rar:
        target = str(src / "*") if src.is_dir() and not arcname else str(src)
        engines.run([rar, "a", "-r", "-ep1", "-idq", "-y", "-m5", str(dst), target], timeout=3600)
        return
    import packport
    _packport(packport.write_rar, dst, src, arcname)


# ---- ISO 9660 (+ Joliet for Windows, Rock Ridge for Linux/macOS: both keep the real names)

def _iso_name(name: str, used: set[str], is_dir: bool) -> str:
    """A unique ISO 9660 level 3 name: A-Z 0-9 _, 30 characters, one dot for files."""
    stem, _, ext = name.rpartition(".") if not is_dir and "." in name.strip(".") else (name, "", "")
    clean = lambda s: re.sub(r"[^A-Z0-9_]", "_", s.upper())
    stem, ext = clean(stem) or "_", clean(ext)[:3]
    room = 31 if is_dir else 30 - len(ext) - 1
    cand, i = stem[:room], 1
    while cand in used:
        tag = f"_{i}"
        cand, i = stem[:room - len(tag)] + tag, i + 1
    used.add(cand)
    return cand if is_dir else f"{cand}.{ext}"


def _joliet_name(name: str, used: set[str]) -> str:
    """Joliet allows 64 characters (pycdlib counts them in UTF-8 bytes); long names keep their
    extension and stay unique."""
    def fit(s: str, room: int) -> str:
        while len(s.encode()) > room:
            s = s[:-1]
        return s
    stem, ext = name.rsplit(".", 1) if "." in name.strip(".") else (name, "")
    ext = fit(f".{ext}", 16) if ext else ""
    cand, i = fit(name, 64) if len(name.encode()) <= 64 else fit(stem, 64 - len(ext.encode())) + ext, 1
    while cand.lower() in used:
        tag = f"~{i}"
        cand, i = fit(stem, 64 - len(ext.encode()) - len(tag)) + tag + ext, i + 1
    used.add(cand.lower())
    return cand


def _write_iso(src: Path, dst: Path) -> None:
    import pycdlib
    iso = pycdlib.PyCdlib()
    vol = re.sub(r"[^A-Z0-9_]", "_", strip_ext(src).upper())[:32] or "CDROM"
    iso.new(interchange_level=3, joliet=3, rock_ridge="1.09", vol_ident=vol)
    try:
        def add(path: Path, iso_dir: str, joliet_dir: str, depth: int) -> None:
            used_iso: set[str] = set()
            used_joliet: set[str] = set()
            for p in sorted(path.iterdir()) if path.is_dir() else [path]:
                is_dir = p.is_dir()
                iso_path = f"{iso_dir}/{_iso_name(p.name, used_iso, is_dir)}"
                joliet_path = f"{joliet_dir}/{_joliet_name(p.name, used_joliet)}"
                if is_dir:
                    if depth >= 7:  # ISO 9660 stops at 8 levels
                        raise ConversionError(f"{p.name} is nested too deep for an ISO; use zip or 7z")
                    iso.add_directory(iso_path, rr_name=p.name, joliet_path=joliet_path)
                    add(p, iso_path, joliet_path, depth + 1)
                else:
                    iso.add_file(str(p), iso_path + ";1", rr_name=p.name, joliet_path=joliet_path)
        add(src, "", "", 0)
        iso.write(str(dst))
    finally:
        iso.close()


# ---- extraction. Every extractor gets the same rules (safety.py): names that climb out of the
# folder and links are left out, other names are made safe for Windows, clashing names are
# renamed instead of overwritten, and nothing may fill the disk. A sweep afterwards removes any
# link that slipped through.

ENCRYPTED = "{} is password-protected; Latheport can't open encrypted archives"


@dataclass
class _Report:
    skipped: int = 0
    renamed: int = 0
    notes: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = []
        if self.skipped:  # fewer files (or a comic's pages) than the archive lists
            out.append(Note(f"{self.skipped} {'entry' if self.skipped == 1 else 'entries'} skipped "
                            f"(links, or names that would land outside the folder)", ("files", "pages")))
        if self.renamed:
            out.append(f"{self.renamed} renamed so they don't overwrite another file")
        return out + self.notes


def extract_to(src: Path, dest: Path, fmt: str, notes: list[str] | None = None) -> None:
    """Unpack src into the folder dest. notes, when given, gets a line for anything skipped or
    renamed."""
    import py7zr
    dest.mkdir(parents=True, exist_ok=True)
    dest = dest.resolve()
    fmt = CONTAINER.get(fmt, fmt)
    report = _Report()
    if fmt in PACKPORT_EXTRACT:
        _extract_packport(src, dest, Budget(dest, src.name), report)
    elif fmt in BOOKPORT_EXTRACT:
        _extract_bookport(src, dest, fmt, Budget(dest, src.name), report)
    elif fmt in MSI_EXTRACT:
        _extract_msi(src, dest, Budget(dest, src.name), report)
    else:
        try:
            _native_extract(src, dest, fmt, report)
        except (Refused, cancel.Cancelled):  # stopped is stopped: not damaged, nor one for packport
            raise
        except (NotImplementedError, ConversionError, py7zr.exceptions.ArchiveError, zipfile.BadZipFile,
                tarfile.TarError, zstd.ZstdError, lzma.LZMAError, zlib.error, EOFError, RuntimeError,
                OSError) as exc:
            # zipfile and py7zr don't do every method (Deflate64, PPMd, BCJ2...) or read damaged
            # archives; packport does both
            if fmt not in ("zip", "7z"):
                why = str(exc) or type(exc).__name__
                raise ConversionError(f"Couldn't extract {src.name}; it looks damaged ({why})") from exc
            shutil.rmtree(dest, ignore_errors=True)
            dest.mkdir(parents=True)
            report = _Report()
            _extract_packport(src, dest, Budget(dest, src.name), report)
    report.skipped += sweep(dest)
    if notes is not None:
        notes.extend(report.lines())


def _native_extract(src: Path, dest: Path, fmt: str, report: _Report) -> None:
    budget = Budget(dest, src.name)
    if fmt == "zip":
        _extract_zip(src, dest, budget, report)
    elif fmt == "7z":
        _extract_7z(src, dest, budget, report)
    elif fmt in TAR_STREAM:
        with STREAM[TAR_STREAM[fmt]](src, "rb") as f, tarfile.open(fileobj=f, mode="r|") as tf:
            _extract_tar(tf, dest, budget, report)
    elif fmt.startswith("tar"):
        with tarfile.open(src) as tf:
            _extract_tar(tf, dest, budget, report)
    elif fmt in STREAM:
        with STREAM[fmt](src, "rb") as fin:  # no reliable size up front: the budget stops a bomb
            copy_capped(fin, dest / (safe_name(strip_ext(src)) or "extracted"), None, budget, src.name)
    elif fmt == "cpio":
        _extract_cpio(src, dest, budget, report)
    else:
        raise ConversionError(f"Can't extract {fmt}")


def _place(namer: Namer, dest: Path, name: str, is_dir: bool, report: _Report) -> Path | None:
    """Where an entry goes (its folders made), or None when it must be left out."""
    rel = safe_name(name)
    rel = rel and namer.claim(rel, is_dir)
    if rel is None:
        report.skipped += 1
        return None
    target = dest / rel
    (target if is_dir else target.parent).mkdir(parents=True, exist_ok=True)
    return target


def _extract_zip(src: Path, dest: Path, budget: Budget, report: _Report) -> None:
    namer = Namer()
    with zipfile.ZipFile(src) as zf:
        infos = zf.infolist()
        if any(i.flag_bits & 1 for i in infos):
            raise Refused(ENCRYPTED.format(src.name))
        budget.check(sum(i.file_size for i in infos))
        for info in infos:
            if info.create_system == 3 and stat.S_ISLNK(info.external_attr >> 16):  # a Unix link
                report.skipped += 1
                continue
            target = _place(namer, dest, info.filename, info.is_dir(), report)
            if target and not info.is_dir():
                with zf.open(info) as fin:  # zipfile itself stops at the size and checks the CRC
                    copy_capped(fin, target, info.file_size, budget, info.filename)
    report.renamed += namer.renamed


def _extract_tar(tf: tarfile.TarFile, dest: Path, budget: Budget, report: _Report) -> None:
    """Members one at a time, so it also works on a stream (.tar.zst), each copied a megabyte at a time (so
    Cancel stops inside a big one too) with the Windows names, the clash renaming and the budget. Every link and
    special file is left out; a hard link to a file already unpacked becomes a copy of it."""
    namer = Namer()
    placed: dict[str, Path] = {}  # safe archive name -> where it went
    copies: list[tuple[Path, Path]] = []
    for member in tf:
        cancel.check()
        original = placed.get(safe_name(member.linkname) or "") if member.islnk() else None
        if not (member.isreg() or member.isdir() or original):
            report.skipped += 1
            continue
        target = _place(namer, dest, member.name, member.isdir(), report)
        if target is None or member.isdir():
            continue
        if original:
            budget.take(original.stat().st_size if original.exists() else 0)
            copies.append((original, target))
            continue
        with tf.extractfile(member) as fin:
            copy_capped(fin, target, member.size, budget, member.name)
        placed[safe_name(member.name)] = target
        with contextlib.suppress(OSError, OverflowError, ValueError):
            os.utime(target, (member.mtime, member.mtime))
    for original, target in copies:
        cancel.check()
        shutil.copy2(original, target)
    report.renamed += namer.renamed


def _extract_7z(src: Path, dest: Path, budget: Budget, report: _Report) -> None:
    import py7zr
    from py7zr.helpers import get_sanitized_output_path
    from py7zr.io import Py7zIO, WriterFactory

    class Writer(Py7zIO):
        def __init__(self, target: Path):
            self.f = open(target, "xb")  # noqa: SIM115 - closed by close(), or below on errors
            opened.append(self.f)
            self.n = 0

        def write(self, s) -> int:
            cancel.check()
            budget.take(len(s))  # py7zr itself stops each file at its declared size
            self.n += len(s)
            n = self.f.write(s)
            cancel.moved(len(s))
            return n

        def read(self, size=None) -> bytes:
            return b""

        def seek(self, offset, whence=0) -> int:
            return self.n

        def seekable(self) -> bool:
            return False

        def flush(self) -> None:
            self.f.flush()

        def size(self) -> int:
            return self.n

        def close(self) -> None:
            self.f.close()

    namer, where, opened = Namer(), {}, []

    class Factory(WriterFactory):
        def create(self, filename: str) -> Py7zIO:
            target = where.pop(filename, None)
            if target is None:  # a name py7zr renamed itself (the same name twice)
                target = _place(namer, dest, Path(filename).relative_to(dest).as_posix(), False, report)
            return Writer(target) if target else py7zr.io.NullIO()

    try:
        with py7zr.SevenZipFile(src) as z:
            if z.needs_password():
                raise Refused(ENCRYPTED.format(src.name))
            if any("ppmd" in m.lower() for m in z.archiveinfo().method_names):
                # py7zr's PPMd decoder (pyppmd) crashes the program when it's freed part-way
                # through, which is what Cancel does: packport reads these (extract_to falls back)
                raise NotImplementedError("PPMd")
            infos = z.list()
            budget.check(sum(i.uncompressed or 0 for i in infos if i.is_file))
            targets = []
            for i in infos:
                try:
                    key = get_sanitized_output_path(i.filename, dest).as_posix()
                except py7zr.exceptions.Bad7zFile:  # a name py7zr refuses to extract at all
                    key = None
                if not (i.is_file or i.is_directory) or i.is_symlink or key is None:
                    report.skipped += 1
                    continue
                target = _place(namer, dest, i.filename, i.is_directory, report)
                if target and i.is_file:
                    targets.append(i.filename)
                    where[key] = target
            z.extract(dest, targets=targets, factory=Factory())
    except py7zr.exceptions.PasswordRequired:
        raise Refused(ENCRYPTED.format(src.name)) from None
    finally:
        for f in opened:
            f.close()
    for target in where.values():  # empty files have no data, so py7zr never asks for them
        target.touch()
    report.renamed += namer.renamed


def _extract_cpio(src: Path, dest: Path, budget: Budget, report: _Report) -> None:
    import packport

    def skipped(_name: str) -> None:
        report.skipped += 1

    try:  # streamed; packport applies the same name rules and never writes links
        packport.extract_cpio(src, dest, max_size=max(budget.left, 0), on_skip=skipped)
    except packport.PackportError:  # old binary or odc CPIO: packport's general reader has those
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True)
        _extract_packport(src, dest, budget, report)


def _extract_packport(src: Path, dest: Path, budget: Budget, report: _Report) -> None:
    """Every format packport reads, through one loop: the declared total checked up front, each
    file stopped at its declared size, names made safe and clashes renamed, links and special
    files left out, encrypted archives refused."""
    import packport
    from packport.entries import TooBig
    namer = Namer()
    try:
        # the cap on unpacking ahead (an RPM's payload, a solid installer) is the free space, as the
        # Budget's: packport's default, 200 times the file's size, refuses an honest payload of zeros
        with packport.open(src, max_size=max(budget.left, 0)) as archive:
            files = [m for m in archive.members if m.is_file]
            if any(m.encrypted for m in files):
                raise Refused(ENCRYPTED.format(src.name))
            budget.check(sum(m.size for m in files))
            for m in archive.members:
                if m.is_dir:
                    _place(namer, dest, m.name, True, report)
                elif not m.is_file:
                    report.skipped += 1  # links, devices
            for m, stream in archive.files():
                cancel.check()
                target = _place(namer, dest, m.name, False, report)
                if target is None:
                    continue
                copy_capped(stream, target, m.size, budget, m.name)
                if m.mtime:
                    with contextlib.suppress(OSError, ValueError, OverflowError):
                        os.utime(target, (m.mtime, m.mtime))
    except packport.EncryptedError:
        raise Refused(ENCRYPTED.format(src.name)) from None
    except TooBig:
        raise Refused(f"{src.name} unpacks to more than the {_size(budget.free)} free on that drive") from None
    except packport.PackportError as exc:
        raise ConversionError(f"Couldn't extract {src.name}: {_ours(exc)}{_lone_piece(src)}") from None
    report.renamed += namer.renamed


def _ours(exc: Exception) -> str:
    """packport's message in Latheport's words: it says "packport doesn't ..." about itself."""
    msg = re.sub(r"\bpackport\b", "Latheport", str(exc))
    if msg == "Latheport doesn't know what kind of archive this is":
        msg += "; it may be damaged"
    return msg


def _lone_piece(src: Path) -> str:
    """A hint when a damaged archive is the first piece of a split whose other pieces aren't beside it."""
    stem, _, number = src.name.rpartition(".")
    if not (number.isdigit() and len(number) == 3 and int(number) <= 1):
        return ""
    if src.with_name(f"{stem}.{int(number) + 1:03d}").is_file():
        return ""
    return (f". It may be one piece of a split archive: put the other pieces (.{int(number) + 1:03d}, "
            f".{int(number) + 2:03d}, ...) in the same folder and try again")


def _extract_bookport(src: Path, dest: Path, fmt: str, budget: Budget, report: _Report) -> None:
    """The pages, pictures and indexes inside a CHM help file or a LIT book."""
    import bookport
    try:
        entries = bookport.unpack(src, fmt)
    except bookport.BookportError as exc:
        if type(exc).__name__ == "DRMError":
            raise Refused(f"{src.name} is DRM-protected; Latheport can't open it") from None
        raise ConversionError(str(exc)) from None
    budget.check(sum(len(data) for _, data in entries))
    namer = Namer()
    for name, data in entries:
        cancel.check()
        target = _place(namer, dest, name, False, report)
        if target is not None:
            copy_capped(io.BytesIO(data), target, len(data), budget, name)
    report.renamed += namer.renamed


# Windows Installer packs stream names two characters at a time into one code point
_MSI_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz._"


def _msi_name(name: str) -> str:
    out = []
    for ch in name:
        c = ord(ch)
        if 0x3800 <= c < 0x4800:
            c -= 0x3800
            out += [_MSI_CHARS[c & 0x3F], _MSI_CHARS[c >> 6 & 0x3F]]
        elif 0x4800 <= c < 0x4840:
            out.append(_MSI_CHARS[c - 0x4800])
        elif c == 0x4840:
            out.append("!")  # the installer's own tables
        elif c < 0x20:
            out.append(f"[{c}]")  # a control character, as in "[5]SummaryInformation"
        else:
            out.append(ch)
    return "".join(out)


def _extract_msi(src: Path, dest: Path, budget: Budget, report: _Report) -> None:
    """A Windows Installer package (or patch): its streams, under their real names; a CAB among
    them (where the installed files are) is unpacked into a folder of its own name."""
    import olefile
    import packport
    try:
        ole = olefile.OleFileIO(str(src))
    except (OSError, ValueError) as exc:
        raise ConversionError(f"Couldn't open {src.name} as a Windows Installer package ({exc})") from None
    namer = Namer()

    def read(path) -> bytes:
        try:
            return ole.openstream(path).read()
        except (OSError, ValueError, IndexError, struct.error) as exc:  # olefile on a cut-short or damaged file
            raise ConversionError(f"Couldn't extract {src.name}: the package is damaged ({exc})") from None

    with ole:
        try:
            streams = ole.listdir(streams=True, storages=False)
            sizes = sum(ole.get_size(s) for s in streams)
        except (OSError, ValueError, IndexError, struct.error) as exc:
            raise ConversionError(f"Couldn't extract {src.name}: the package is damaged ({exc})") from None
        budget.check(sizes)
        for path in streams:
            cancel.check()
            name = "/".join(_msi_name(p) for p in path)
            data = read(path)
            if data[:4] == b"MSCF":
                folder = _place(namer, dest, name.removesuffix(".cab") or "cab", True, report)
                if folder is None:
                    continue
                try:
                    with packport.open(data) as cab:
                        inner = Namer()
                        for m, stream in cab.files():
                            target = _place(inner, folder, m.name, False, report)
                            if target is not None:
                                copy_capped(stream, target, m.size, budget, m.name)
                        report.renamed += inner.renamed
                except packport.PackportError as exc:
                    raise ConversionError(f"Couldn't extract {src.name}: {name}: {_ours(exc)}") from None
                continue
            target = _place(namer, dest, name, False, report)
            if target is not None:
                copy_capped(io.BytesIO(data), target, len(data), budget, name)
    report.renamed += namer.renamed


@converter(inputs={"*"}, outputs=COMPRESS_OUT, terminal=True)
@converter(inputs={"folder"}, outputs=FOLDER_OUT, terminal=True)
def compress_convert(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
    compress(src, dst, dst_fmt)


def repack(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
    work = dst.parent / "_extracted" / strip_ext(src)
    found: list[str] = []
    extract_to(src, work, src_fmt, found)
    for line in found:  # entries left out or renamed: the new archive doesn't have them either
        note(line)
    entries = list(work.iterdir())
    root = entries[0] if len(entries) == 1 else work
    compress(root, dst, dst_fmt)


# Only real archives repack; a .chm or .lit is converted as an ebook
_NATIVE_ARCHIVES = {f for f in NATIVE_EXTRACT - COMICS - set(STREAM) if category(f) == "Archive"}
converter(inputs=_NATIVE_ARCHIVES, outputs=FOLDER_OUT, terminal=True)(repack)
converter(inputs=set(STREAM), outputs=COMPRESS_OUT, terminal=True)(repack)
# comic -> comic keeps the pages, just in another container
converter(inputs=COMICS, outputs=COMICS)(repack)
