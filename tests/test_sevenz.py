"""The 7z writer (converters/sevenz.py): archives in several blocks, packed on several threads, that py7zr and
Windows' own tar (libarchive) open with every file, folder, name, date and checksum as written."""

import contextlib
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import py7zr
import pytest

from latheport import cancel
from latheport.converters import sevenz

BSDTAR = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "tar.exe"


def _tree(root: Path, wide: bool = True) -> Path:
    """A folder of everything: nested and empty folders, an empty file, big and small files, names beyond ASCII
    (wide: Japanese too, which Windows' tar can't write to disk even from py7zr's own archives)."""
    src = root / "src"
    (src / "deep" / "deeper").mkdir(parents=True)
    (src / "empty folder").mkdir()
    (src / "empty file.txt").write_bytes(b"")
    (src / ("naïve café ラ.txt" if wide else "naïve café.txt")).write_text("unicode name")
    (src / "deep" / "deeper" / "random.bin").write_bytes(os.urandom(300_000))  # incompressible
    (src / "deep" / "text.txt").write_text("the same line\n" * 50_000)  # very compressible
    (src / "big.bin").write_bytes(os.urandom(2_500_000) * 2)  # bigger than a chunk
    for i in range(40):
        (src / "deep" / f"small {i:02}.txt").write_text(f"file {i}\n" * (i + 1))
    (src / "mid").mkdir()
    for i in range(12):
        (src / "mid" / f"part {i:02}.bin").write_bytes(os.urandom(20_000) * 4)
    return src


def _items(src: Path) -> list[tuple[str, Path]]:
    return [(src.name, src)] + [(f"{src.name}/{p.relative_to(src).as_posix()}", p) for p in sorted(src.rglob("*"))]


@contextlib.contextmanager
def _open(arc: Path):
    """py7zr reading the archive through one open file: given a name, it reads several blocks on threads that
    each open the file and never close it (closed as each thread ends, but the suite treats the warning as an
    error). py7zr leaves a file it was handed open, so this closes both."""
    with arc.open("rb") as f, py7zr.SevenZipFile(f) as z:
        yield z


def _same(a: Path, b: Path) -> None:
    listing = lambda root: sorted(p.relative_to(root).as_posix() + "/" * p.is_dir() for p in root.rglob("*"))
    assert listing(a) == listing(b)
    for p in a.rglob("*"):
        if p.is_file():
            assert (b / p.relative_to(a)).read_bytes() == p.read_bytes(), p


@pytest.fixture
def small_blocks(monkeypatch):
    """Blocks of 64 KB, so a small test folder makes many blocks on several threads."""
    monkeypatch.setattr(sevenz, "BLOCK_MIN", 64 << 10)
    monkeypatch.setattr(sevenz, "BLOCK_MAX", 64 << 10)
    monkeypatch.setattr(sevenz, "threads", lambda: 4)


def test_many_blocks_open_in_py7zr_with_every_file_as_written(tmp_path, small_blocks):
    src = _tree(tmp_path)
    arc = tmp_path / "out.7z"
    sevenz.write(arc, _items(src))
    with _open(arc) as z:
        folders = len(z.header.main_streams.unpackinfo.folders)
        z.extractall(tmp_path / "x")  # py7zr checks each file's checksum as it unpacks it
    assert folders >= 12  # really several blocks
    _same(src, tmp_path / "x" / "src")
    with _open(arc) as z:
        info = {i.filename: i for i in z.list()}
    assert info["src/empty folder"].is_directory and info["src/deep"].is_directory
    assert not info["src/empty file.txt"].is_directory and info["src/empty file.txt"].uncompressed == 0
    when = info["src/deep/text.txt"].creationtime.timestamp()  # py7zr shows the mtime as creationtime
    assert abs(when - (src / "deep" / "text.txt").stat().st_mtime) < 0.01


@pytest.mark.skipif(not BSDTAR.exists(), reason="Windows' own tar (libarchive)")
def test_windows_own_tar_opens_it_too(tmp_path, small_blocks):
    """libarchive shares no code with py7zr and reads the coders only in py7zr's order: LZMA2, then x86."""
    src = _tree(tmp_path, wide=False)
    arc = tmp_path / "out.7z"
    sevenz.write(arc, _items(src))
    out = tmp_path / "x"
    out.mkdir()
    subprocess.run([str(BSDTAR), "-xf", str(arc), "-C", str(out)], check=True, capture_output=True)
    _same(src, out / "src")


def test_a_damaged_block_is_caught_by_its_checksum(tmp_path, small_blocks):
    src = _tree(tmp_path)
    arc = tmp_path / "out.7z"
    sevenz.write(arc, _items(src))
    data = bytearray(arc.read_bytes())
    data[40_000] ^= 0xFF  # inside the packed data, past the signature header
    arc.write_bytes(bytes(data))
    with _open(arc) as z:
        try:
            z.extractall(tmp_path / "x")  # a wrong checksum, or a block that doesn't unpack: py7zr's errors vary
            bad = None
        except Exception as exc:  # noqa: BLE001
            bad = type(exc).__name__
    assert bad is not None


def test_folders_and_empty_files_only(tmp_path):
    src = tmp_path / "src"
    (src / "a" / "b").mkdir(parents=True)
    (src / "a" / "nothing.txt").write_bytes(b"")
    arc = tmp_path / "out.7z"
    sevenz.write(arc, _items(src))
    with _open(arc) as z:
        z.extractall(tmp_path / "x")
    _same(src, tmp_path / "x" / "src")


def test_one_file(tmp_path):
    f = tmp_path / "photo.png"
    f.write_bytes(os.urandom(5000))
    arc = tmp_path / "out.7z"
    sevenz.write(arc, [(f.name, f)])
    with _open(arc) as z:
        z.extractall(tmp_path / "x")
    assert (tmp_path / "x" / "photo.png").read_bytes() == f.read_bytes()


def test_it_packs_on_several_threads_at_once(tmp_path, small_blocks, monkeypatch):
    src = _tree(tmp_path)
    busy, most = [0], [0]
    lock = threading.Lock()
    real = sevenz._pack

    def counted(*a):
        with lock:
            busy[0] += 1
            most[0] = max(most[0], busy[0])
        try:
            time.sleep(0.05)  # long enough for the others to start
            return real(*a)
        finally:
            with lock:
                busy[0] -= 1
    monkeypatch.setattr(sevenz, "_pack", counted)
    sevenz.write(tmp_path / "out.7z", _items(src))
    assert most[0] == 4


def test_packing_says_how_far_it_has_got_and_cancel_stops_it(tmp_path, small_blocks):
    src = _tree(tmp_path)
    total = sum(p.stat().st_size for p in src.rglob("*") if p.is_file())
    token = cancel.Token()
    heard = []
    token.listener = lambda done, of: heard.append((done, of, token.doing))
    with cancel.using(token):
        sevenz.write(tmp_path / "out.7z", _items(src))
    assert heard[-1] == (total, total, "packing")

    token = cancel.Token()
    token.listener = lambda done, of: token.cancel()  # Cancel pressed as soon as packing starts
    started = time.monotonic()
    with cancel.using(token), pytest.raises(cancel.Cancelled):
        sevenz.write(tmp_path / "stopped.7z", _items(src))
    assert time.monotonic() - started < 5


def test_dictionary_and_property_byte():
    assert sevenz._dict(10) == 1 << 16 and sevenz._dict(5 << 20) == 8 << 20 and sevenz._dict(1 << 30) == 8 << 20
    assert sevenz._dict_property(8 << 20) == 22 and sevenz._dict_property(1 << 16) == 8  # 7-Zip's own values
    assert sevenz._number(0x7F) == b"\x7f" and sevenz._number(0x80) == b"\x80\x80"
    assert sevenz._number(0x4000) == b"\xc0\x00\x40" and len(sevenz._number(1 << 60)) == 9


def test_threads_use_three_quarters_of_the_cores_at_most_eight(monkeypatch):
    for cores, want in ((1, 1), (2, 1), (4, 3), (16, 8), (64, 8)):
        monkeypatch.setattr(os, "cpu_count", lambda c=cores: c)
        assert sevenz.threads() == want


def test_shutil_free(tmp_path):
    """No temporary files are left beside the archive."""
    src = _tree(tmp_path)
    out = tmp_path / "o"
    out.mkdir()
    sevenz.write(out / "a.7z", _items(src))
    assert [p.name for p in out.iterdir()] == ["a.7z"]
    shutil.rmtree(out)
