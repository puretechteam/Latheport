"""Hostile and damaged archives. Each one is built here, extracted into a folder inside a fence,
and must leave everything outside that folder alone, never overwrite a file, never leave a link
behind, and fail (when it fails) only with a readable ConversionError."""

import gzip
import io
import os
import stat
import struct
import subprocess
import tarfile
import uuid
import warnings
import zipfile
import zlib
from pathlib import Path

import pytest

from convertall import engines, safety
from convertall.converters import archives
from convertall.errors import ConversionError
from convertall.pipeline import convert, extract

TAG = uuid.uuid4().hex[:8]  # names an escape would leave at the drive root, unique to this run


def _snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _links(root: Path) -> list[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = Path(dirpath, name)
            st = p.lstat()
            if p.is_symlink() or getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                out.append(str(p))
    return out


class Fence:
    """tmp/fence/out is the extraction folder; tmp/fence and tmp itself hold files that must
    survive untouched."""

    def __init__(self, tmp: Path):
        self.tmp, self.fence, self.out = tmp, tmp / "fence", tmp / "fence" / "out"
        self.out.mkdir(parents=True)
        (self.fence / "outside.txt").write_text("keep")
        (self.out / "existing.txt").write_text("keep")
        self.before = _snapshot(tmp)

    def arc(self, name: str) -> Path:
        (self.tmp / "arcs").mkdir(exist_ok=True)
        return self.tmp / "arcs" / name

    def extract(self, arc: Path, fmt: str | None = None) -> tuple[dict[str, bytes], list[str]]:
        """Extract and check the fence; returns what landed (relative to the result) and the notes."""
        notes: list[str] = []
        result = extract(arc, self.out, src_fmt=fmt, notes=notes)
        self.check(arc)
        files = _snapshot(result) if result.is_dir() else {result.name: result.read_bytes()}
        return files, notes

    def refused(self, arc: Path, match: str, fmt: str | None = None) -> None:
        with pytest.raises(ConversionError, match=match):
            extract(arc, self.out, src_fmt=fmt)
        self.check(arc)
        assert sorted(p.name for p in self.out.iterdir()) == ["existing.txt"]  # no half-extracted leftovers

    def check(self, arc: Path) -> None:
        after = _snapshot(self.tmp)
        for name, data in self.before.items():
            assert after.get(name) == data, f"{name} was changed"
        new = {n for n in after if n not in self.before and not n.startswith("arcs/")}
        assert all(n.startswith("fence/out/") for n in new), f"landed outside: {sorted(new)}"
        assert not _links(self.fence)
        for leftover in (f"{TAG}-abs.txt", f"{TAG}-drive.txt"):
            assert not (Path(self.tmp.anchor) / leftover).exists()


@pytest.fixture
def fence(tmp_path):
    return Fence(tmp_path)


def _zip(path: Path, entries) -> Path:
    """entries: (name, data) or a ready ZipInfo with its data; names go in exactly as given."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # duplicate names are the point
        with zipfile.ZipFile(path, "w") as zf:
            for name, data in entries:
                zf.writestr(name, data)
    return path


def _tar(path: Path, entries, mode: str = "w") -> Path:
    """entries: (name, data) for files, or (name, type, linkname)."""
    def add(tf):
        for e in entries:
            ti = tarfile.TarInfo(e[0])
            if len(e) == 2:
                ti.size = len(e[1])
                tf.addfile(ti, io.BytesIO(e[1]))
            else:
                ti.type, ti.linkname = e[1], e[2]
                tf.addfile(ti)
    if mode == "zst":
        with archives.STREAM["zst"](path, "wb") as f, tarfile.open(fileobj=f, mode="w|") as tf:
            add(tf)
    else:
        with tarfile.open(path, mode, format=tarfile.GNU_FORMAT) as tf:
            add(tf)
    return path


# ---- ZIP

HOSTILE_ZIP_NAMES = [
    ("../up1.txt", b"no"), ("a/../../up2.txt", b"no"), ("..\\..\\up3.txt", b"no"),
    (f"/{TAG}-abs.txt", b"abs"), (f"C:/{TAG}-drive.txt", b"drive"),
    ("CON.txt", b"con"), ("nul", b"nul"), ("dir/a.txt:ads", b"ads"), ("name.", b"dot"),
    ("ok.txt", b"fine"),
]


def test_zip_hostile_names(fence):
    files, notes = fence.extract(_zip(fence.arc("names.zip"), HOSTILE_ZIP_NAMES))
    assert files == {f"{TAG}-abs.txt": b"abs", f"{TAG}-drive.txt": b"drive", "_CON.txt": b"con",
                     "_nul": b"nul", "dir/a.txt_ads": b"ads", "name_": b"dot", "ok.txt": b"fine"}
    assert notes == ["3 entries skipped (links, or names that would land outside the folder)"]


def test_zip_duplicates_and_case_clashes_are_renamed(fence):
    arc = _zip(fence.arc("dups.zip"), [("a.txt", b"1"), ("a.txt", b"2"), ("Readme.md", b"x"),
                                       ("README.md", b"y"), ("d/f", b"3"), ("D/F", b"4")])
    files, notes = fence.extract(arc)
    expected = {"a.txt": b"1", "a (1).txt": b"2", "Readme.md": b"x", "README (1).md": b"y",
                "d/f": b"3", "d/F (1)": b"4"}
    if not safety.FOLD_CASE:
        expected.update({"README.md": b"y", "D/F": b"4"})
        del expected["README (1).md"], expected["d/F (1)"]
    assert {k.lower(): v for k, v in files.items()} == {k.lower(): v for k, v in expected.items()}
    assert any("renamed" in n for n in notes)


def test_zip_symlink_entry_is_left_out(fence):
    link = zipfile.ZipInfo("link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    files, notes = fence.extract(_zip(fence.arc("link.zip"), [(link, "../../outside"), ("ok.txt", b"ok")]))
    assert files == {"ok.txt": b"ok"} and notes[0].startswith("1 entry skipped")


def test_zip_encrypted_is_refused(fence):
    arc = _zip(fence.arc("locked.zip"), [("secret.txt", b"x" * 40), ("b.txt", b"b")])
    data = bytearray(arc.read_bytes())
    for sig, flags in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):  # set "encrypted" in both headers
        i = data.index(sig)
        data[i + flags] |= 1
    arc.write_bytes(bytes(data))
    fence.refused(arc, "password-protected")


@pytest.mark.parametrize("damage", ["truncated", "bad-crc"])
def test_zip_damaged(fence, damage):
    arc = _zip(fence.arc("bad.zip"), [("a.txt", b"hello world " * 200), ("b.txt", b"b" * 3000)])
    data = bytearray(arc.read_bytes())
    if damage == "truncated":
        data = data[: len(data) // 2]
    else:
        data[60] ^= 0xFF  # inside a.txt's data
    arc.write_bytes(bytes(data))
    with pytest.raises(ConversionError) as err:
        extract(arc, fence.out)
    assert str(err.value)
    fence.check(arc)


def test_zip_bigger_than_the_free_space_is_refused(fence, monkeypatch, without_engine):
    arc = fence.arc("big.zip")
    with zipfile.ZipFile(arc, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("zeros.bin", bytes(20 << 20))  # 20 MB that packs to about 20 KB
    free = safety.MARGIN + (5 << 20)
    monkeypatch.setattr(safety.shutil, "disk_usage", lambda p: _usage(free))
    fence.refused(arc, r"unpacks to 20\.0 MB, more than the")


def _usage(free):
    from collections import namedtuple
    return namedtuple("usage", "total used free")(free * 2, free, free)


def test_gz_bomb_stops_at_the_free_space(fence, monkeypatch):
    arc = fence.arc("bomb.bin.gz")
    with gzip.open(arc, "wb") as f:
        f.write(bytes(30 << 20))
    monkeypatch.setattr(safety.shutil, "disk_usage", lambda p: _usage(safety.MARGIN + (8 << 20)))
    fence.refused(arc, "more than the")


# ---- TAR

@pytest.mark.parametrize("mode", ["w", "w:gz", "zst"])
def test_tar_hostile_names_and_links(fence, mode):
    ext = {"w": "tar", "w:gz": "tar.gz", "zst": "tar.zst"}[mode]
    arc = _tar(fence.arc(f"evil.{ext}"), [
        ("../up.txt", b"no"), (f"/{TAG}-abs.txt", b"abs"), ("C:x.txt", b"cx"), ("a:b", b"ab"),
        ("esc", tarfile.SYMTYPE, "../../outside"), ("esc/pwned.txt", b"inside after all"),
        ("hl", tarfile.LNKTYPE, "../../outside_hl"),
        ("dev", tarfile.CHRTYPE, ""), ("fifo", tarfile.FIFOTYPE, ""),
        ("real.txt", b"data"), ("same.txt", tarfile.LNKTYPE, "real.txt"),
    ], mode)
    files, notes = fence.extract(arc)
    assert files == {f"{TAG}-abs.txt": b"abs", "C_x.txt": b"cx", "a_b": b"ab",
                     "esc/pwned.txt": b"inside after all", "real.txt": b"data", "same.txt": b"data"}
    assert notes == ["5 entries skipped (links, or names that would land outside the folder)"]
    assert not (fence.tmp / "outside").exists() and not (fence.tmp / "outside_hl").exists()


def test_tar_truncated(fence):
    arc = _tar(fence.arc("cut.tar.gz"), [("a.bin", os.urandom(50_000)), ("b.bin", os.urandom(50_000))], "w:gz")
    arc.write_bytes(arc.read_bytes()[:30_000])
    with pytest.raises(ConversionError) as err:
        extract(arc, fence.out)
    assert str(err.value)
    fence.check(arc)


# ---- 7z (py7zr)

def _7z(path: Path, entries: dict[str, bytes], rename: dict[str, str] | None = None) -> Path:
    """A 7z with a plain (uncompressed) header, so names can be patched afterwards to ones py7zr
    refuses to write: rename maps a placeholder to a name of the same length."""
    import py7zr
    with py7zr.SevenZipFile(path, "w") as z:
        z.encoded_header_mode = False
        for name, data in entries.items():
            z.writestr(data, name)
    data = bytearray(path.read_bytes())
    for old, new in (rename or {}).items():
        o, n = old.encode("utf-16-le"), new.encode("utf-16-le")
        assert len(o) == len(n)
        i = data.index(o)
        data[i:i + len(o)] = n
    off, size = struct.unpack_from("<QQ", data, 12)
    struct.pack_into("<I", data, 28, zlib.crc32(data[32 + off:32 + off + size]))
    struct.pack_into("<I", data, 8, zlib.crc32(data[12:32]))
    path.write_bytes(bytes(data))
    return path


def test_7z_hostile_names(fence):
    arc = _7z(fence.arc("evil.7z"), {"xx/up.txt": b"no", "d/qqx.txt": b"qx", "zzz.txt": b"drive-like", "CON": b"con", "ok.txt": b"ok",
                                     "empty.txt": b""},
              rename={"xx/up.txt": "../up.txt", "d/qqx.txt": "d/q:x.txt", "zzz.txt": "z:z.txt"})
    files, notes = fence.extract(arc)
    assert files == {"_CON": b"con", "ok.txt": b"ok", "empty.txt": b""}
    # py7zr won't write a name with a ':' on Windows, so those are left out too (zip, tar and
    # 7-Zip turn the ':' into "_")
    assert notes[0].startswith("3 entries skipped")


@pytest.mark.parametrize("damage", ["truncated", "bad-crc"])
def test_7z_damaged(fence, damage):
    import py7zr
    arc = fence.arc("bad.7z")
    with py7zr.SevenZipFile(arc, "w", filters=[{"id": py7zr.FILTER_COPY}]) as z:
        z.writestr(b"hello world " * 300, "a.txt")
    data = bytearray(arc.read_bytes())
    if damage == "truncated":
        data = data[: len(data) // 2]
    else:
        data[100] ^= 0xFF  # stored data: only the CRC can tell
    arc.write_bytes(bytes(data))
    with pytest.raises(ConversionError) as err:
        extract(arc, fence.out)
    assert str(err.value)
    fence.check(arc)


# ---- CPIO (packport)

def test_cpio_hostile_names(fence):
    from packport.cpio import _header

    def member(name, mode, data, ino):
        return _header(ino, mode, 1, 0, len(data), name.encode()) + data + bytes(-len(data) % 4)
    reg = stat.S_IFREG | 0o644
    data = (member("../up.txt", reg, b"no", 1) + member("d/a.txt:ads", reg, b"ads", 2)
            + member("link", stat.S_IFLNK | 0o777, b"/etc", 3) + member("ok.txt", reg, b"ok", 4)
            + _header(0, 0, 1, 0, 0, b"TRAILER!!!"))
    arc = fence.arc("evil.cpio")
    arc.write_bytes(data)
    files, notes = fence.extract(arc)
    assert files == {"d/a.txt_ads": b"ads", "ok.txt": b"ok"}
    assert notes[0].startswith("2 entries skipped")


# ---- the paths that extract on the way to something else

def test_comic_pages_stay_inside(fence):
    import io as _io

    from PIL import Image
    page = _io.BytesIO()
    Image.new("RGB", (20, 30), "red").save(page, "PNG")
    arc = _zip(fence.arc("comic.cbz"), [("../../evil.png", page.getvalue()), ("01.png", page.getvalue())])
    pdf = convert(arc, "pdf", fence.out)
    import pymupdf as fitz
    assert fitz.open(pdf).page_count == 1
    fence.check(arc)


def test_repack_leaves_hostile_names_out(fence):
    arc = _zip(fence.arc("evil.zip"), [("../up.txt", b"no"), ("a/ok.txt", b"ok"), ("a/b.txt", b"b")])
    seven = convert(arc, "7z", fence.out)
    files, _ = Fence(fence.tmp / "second").extract(seven)
    assert files == {"ok.txt": b"ok", "b.txt": b"b"}


# ---- packport (rar, iso, cab, rpm, disk images... and the zip/7z fallback)

def _rar(path: Path, entries, password: bool = False) -> Path:
    """A stored RAR 4 with names exactly as given: (name, data) for files, (name, "link", target)
    for Unix symbolic links. password sets every file's "encrypted" flag."""
    from packport import rar as r
    out = bytearray(r.MARK + r._block(r.HEAD_MAIN, 0, bytes(6)))
    for entry in entries:
        name, data = entry[0], entry[1] if len(entry) == 2 else entry[2].encode()
        host, attr = (3, stat.S_IFLNK | 0o777) if len(entry) == 3 else (r.HOST_WIN32, r.ATTR_ARCHIVE)
        raw, unicode = r.encode_name(name)
        flags = r.LONG_BLOCK | (r.FHD_UNICODE if unicode else 0) | (0x04 if password else 0)
        body = r._FILE.pack(len(data), len(data), host, zlib.crc32(data), 0x21 << 16, 29, r.METHOD_STORE,
                            len(raw), attr)
        out += r._block(r.HEAD_FILE, flags, body + raw) + data
    path.write_bytes(bytes(out + r._block(r.HEAD_END, r.SKIP_IF_UNKNOWN, b"")))
    return path


def test_packport_link_then_write_through_it(fence):
    """The CVE-2025-11001 pattern: a link that points out, then a file written through it. The
    link is left out, so the file lands in a real folder of that name inside the extraction."""
    arc = _rar(fence.arc("cve.rar"), [("esc", "link", "../../outside"), ("esc/pwned.txt", b"pwned"),
                                      ("ok.txt", b"ok")])
    files, notes = fence.extract(arc)
    assert files == {"esc/pwned.txt": b"pwned", "ok.txt": b"ok"}
    assert notes[0].startswith("1 entry skipped")
    assert not (fence.tmp / "outside").exists()


def test_packport_hostile_names(fence):
    files, notes = fence.extract(_rar(fence.arc("names.rar"), HOSTILE_ZIP_NAMES))
    assert files == {f"{TAG}-abs.txt": b"abs", f"{TAG}-drive.txt": b"drive", "_CON.txt": b"con",
                     "_nul": b"nul", "dir/a.txt_ads": b"ads", "name_": b"dot", "ok.txt": b"fine"}
    assert notes == ["3 entries skipped (links, or names that would land outside the folder)"]


def test_packport_duplicates_are_renamed(fence):
    files, notes = fence.extract(_rar(fence.arc("dups.cbr"), [("a.txt", b"1"), ("a.txt", b"2"), ("d/f", b"3")]))
    assert files == {"a.txt": b"1", "a (1).txt": b"2", "d/f": b"3"}
    assert any("renamed" in n for n in notes)


def test_packport_password_is_refused(fence):
    fence.refused(_rar(fence.arc("locked.rar"), [("secret.txt", b"x" * 40)], password=True), "password-protected")


def test_packport_cut_short(fence):
    arc = _rar(fence.arc("cut.rar"), [("a.bin", os.urandom(50_000)), ("b.bin", os.urandom(50_000))])
    arc.write_bytes(arc.read_bytes()[:60_000])
    with pytest.raises(ConversionError, match="Couldn't extract cut.rar"):
        extract(arc, fence.out)
    fence.check(arc)


def test_packport_bigger_than_the_free_space_is_refused(fence, monkeypatch):
    arc = _rar(fence.arc("big.rar"), [("a.bin", bytes(3 << 20)), ("b.bin", bytes(3 << 20))])
    monkeypatch.setattr(safety.shutil, "disk_usage", lambda p: _usage(safety.MARGIN + (4 << 20)))
    fence.refused(arc, r"unpacks to 6\.0 MB, more than the")


def test_zip_with_deflate64_falls_back_to_packport(fence):
    import packport
    arc = fence.arc("d64.zip")
    _zip(arc, [("a.txt", b"hello " * 1000)])
    data = bytearray(arc.read_bytes())
    for sig, at in ((b"PK\x03\x04", 8), (b"PK\x01\x02", 10)):  # call it Deflate64: zipfile gives up
        i = data.index(sig)
        data[i + at:i + at + 2] = struct.pack("<H", 9)
    arc.write_bytes(bytes(data))
    with zipfile.ZipFile(arc) as z, pytest.raises(NotImplementedError):
        z.read("a.txt")
    with packport.open(arc) as a:
        assert a.members[0].name == "a.txt"  # stored, so Deflate64 reads it as raw Deflate would
    with pytest.raises(ConversionError):  # the data isn't Deflate64: packport says it's damaged
        extract(arc, fence.out)
    fence.check(arc)


def test_sweep_removes_links_and_junctions(tmp_path):
    root = tmp_path / "x"
    (root / "d").mkdir(parents=True)
    (root / "d" / "f.txt").write_text("f")
    target = tmp_path / "target"
    target.mkdir()
    (target / "precious.txt").write_text("keep")
    made = 0
    if os.name == "nt":
        made += subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "junction"), str(target)],
                               capture_output=True, check=False).returncode == 0
    try:
        os.symlink(target, root / "d" / "dirlink", target_is_directory=True)
        os.symlink(target / "precious.txt", root / "filelink")
        made += 2
    except OSError:
        pass  # Windows without developer mode can't make symbolic links
    if not made:
        pytest.skip("can't make links here")
    assert safety.sweep(root) == made
    assert not _links(root) and (root / "d" / "f.txt").exists()
    assert (target / "precious.txt").read_text() == "keep"  # removing a link never touches its target


def test_safe_name():
    assert safety.safe_name("a/b/c.txt") == "a/b/c.txt"
    assert safety.safe_name("/etc/passwd") == "etc/passwd"
    assert safety.safe_name("C:\\Windows\\x.dll") == "Windows/x.dll"
    assert safety.safe_name("a/../../x") is None and safety.safe_name("..") is None
    assert safety.safe_name("./") is None
    assert safety.safe_name("COM1.log") == "_COM1.log" and safety.safe_name("lpt¹") == "_lpt¹"
    assert safety.safe_name("con ") == "_con_" and safety.safe_name("x/aux.tar.gz") == "x/_aux.tar.gz"
    assert safety.safe_name('a<b>c"d|e?f*g\x01') == "a_b_c_d_e_f_g_"
    assert safety.safe_name("console.txt") == "console.txt"


def test_no_7zip_anywhere():
    """ConvertAll reads every archive itself: no converter, engine or extraction uses 7-Zip."""
    from convertall import registry
    registry._ensure_loaded()
    edges = [e for es in registry._edges.values() for e in es] if isinstance(registry._edges, dict) else list(registry._edges)
    assert not [e for e in edges if e.engine == "7zip"]
    assert "7zip" not in engines.ENGINES
    assert archives.EXTRACTABLE == archives.NATIVE_EXTRACT


def test_the_7zip_guard_stops_a_test_that_starts_it():
    with pytest.raises(AssertionError, match="started 7-Zip"):
        subprocess.Popen(["C:/Program Files/7-Zip/7z.exe", "i"])
    with pytest.raises(AssertionError, match="started 7-Zip"):
        subprocess.run('"C:/Program Files/7-Zip/7z.exe" i', check=False)


@pytest.mark.parametrize("cut", [12850, 14609])
def test_msi_cut_short(fence, cut):
    """A cut-short Windows Installer package fails as damaged, not with olefile's own error
    (it gave "bytes length not a multiple of item size")."""
    from .samples import Samples
    sample = Samples(fence.tmp / "arcs" / "samples")("msi")
    arc = fence.arc("cut.msi")
    arc.write_bytes(sample.read_bytes()[:cut])
    fence.refused(arc, "damaged")


def test_damaged_archive_message_names_convertall_not_its_library(fence):
    """packport's messages say "packport doesn't ..."; ConvertAll's users never heard of it."""
    arc = fence.arc("garbled.cab")
    arc.write_bytes(bytes(range(256)) * 8)
    with pytest.raises(ConversionError) as err:
        extract(arc, fence.out, src_fmt="cab")
    assert "packport" not in str(err.value)
    assert "may be damaged" in str(err.value)
    fence.check(arc)


CANCEL_PPMD = r'''
import gc, random, sys
from pathlib import Path
import py7zr
from convertall import cancel
from convertall.converters.archives import extract_to
work = Path(sys.argv[1])
rng = random.Random(7)
text = work / "big.txt"
text.write_text(" ".join(rng.choice(["alpha", "beta", "gamma", "delta"]) for _ in range(5_000_000)))
arc = work / "ppmd.7z"
with py7zr.SevenZipFile(arc, "w", filters=[{"id": py7zr.FILTER_PPMD, "order": 6, "mem": 24}]) as z:
    z.write(text, "big.txt")
import threading
token = cancel.Token()
threading.Timer(0.3, token.cancel).start()  # Cancel part-way through (it takes seconds to unpack)
try:
    with cancel.using(token):
        extract_to(arc, work / "out", "7z")
except cancel.Cancelled:
    print("stopped")
gc.collect()  # what the stopped extraction left: pyppmd crashed here, freeing its half-used decoder
print("ok")
'''


def test_cancelling_a_ppmd_7z_does_not_crash(tmp_path):
    """py7zr's PPMd decoder (pyppmd) crashes the program when it's freed part-way through a
    stream, which is what Cancel does: a PPMd 7z is read by packport instead."""
    import subprocess
    import sys
    p = subprocess.run([sys.executable, "-c", CANCEL_PPMD, str(tmp_path)], capture_output=True, text=True, check=False,
                       timeout=300)
    assert p.returncode == 0, (p.returncode, p.stdout, p.stderr[-500:])
    assert p.stdout.split() == ["stopped", "ok"]
