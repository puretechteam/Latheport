"""A folder turned into a comic holds its pages and ComicInfo.xml, and nothing else in the folder."""

import tarfile
import zipfile
from pathlib import Path

import py7zr
import pytest
from PIL import Image

from latheport import cancel, registry
from latheport.converters import comics

PAGES = ["01.png", "02.png", "chapter 2/03.png"]
INFO = "ComicInfo.xml"
JUNK = ["Thumbs.db", "desktop.ini", ".DS_Store", "notes.txt", "__MACOSX/._01.png"]


@pytest.fixture
def folder(tmp_path) -> Path:
    """3 PNG pages (one in a subfolder), ComicInfo.xml, and the files Windows, macOS and people leave beside them."""
    root = tmp_path / "comic"
    for i, name in enumerate(PAGES):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (40, 60), (60 * i, 90, 200)).save(root / name)
    (root / INFO).write_text("<?xml version='1.0'?><ComicInfo><Title>Test</Title></ComicInfo>", encoding="utf-8")
    for name in JUNK:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(b"not a page")
    return root


def _entries(path: Path, fmt: str, tmp: Path) -> dict[str, bytes]:
    """Every file in the comic, by its path inside it."""
    if fmt == "cbz":
        with zipfile.ZipFile(path) as z:
            return {i.filename: z.read(i) for i in z.infolist() if not i.is_dir()}
    if fmt == "cbt":
        with tarfile.open(path) as t:
            return {m.name: t.extractfile(m).read() for m in t.getmembers() if m.isfile()}
    out = tmp / "unpacked"
    with py7zr.SevenZipFile(path) as z:
        z.extractall(out)
    return {p.relative_to(out).as_posix(): p.read_bytes() for p in out.rglob("*") if p.is_file()}


@pytest.mark.parametrize("fmt", ["cbz", "cbt", "cb7"])
def test_only_the_pages_and_comicinfo_go_in(folder, tmp_path, fmt):
    dst = tmp_path / f"out.{fmt}"
    found: list = []
    tokens = registry.running({}, found, [])
    try:
        comics.folder_to_comic(folder, dst, "folder", fmt)
    finally:
        registry.reset(tokens)
    entries = _entries(dst, fmt, tmp_path)
    assert sorted(entries) == sorted(PAGES + [INFO])
    for name in PAGES + [INFO]:
        assert entries[name] == (folder / name).read_bytes(), name
    assert found == ["5 files that aren't pages were left out"]


def test_nothing_left_out_says_nothing(tmp_path):
    root = tmp_path / "comic"
    root.mkdir()
    Image.new("RGB", (40, 60)).save(root / "01.png")
    found: list = []
    tokens = registry.running({}, found, [])
    try:
        comics.folder_to_comic(root, tmp_path / "out.cbz", "folder", "cbz")
    finally:
        registry.reset(tokens)
    assert found == []
    with zipfile.ZipFile(tmp_path / "out.cbz") as z:
        assert z.namelist() == ["01.png"]


def test_cancel_stops_copying_the_pages(folder, tmp_path):
    token = cancel.Token()
    token.cancel()
    with cancel.using(token), pytest.raises(cancel.Cancelled):
        comics.folder_to_comic(folder, tmp_path / "out.cbz", "folder", "cbz")
    assert not (tmp_path / "out.cbz").exists()
