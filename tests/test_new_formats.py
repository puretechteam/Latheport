import json
import plistlib
import zipfile
from pathlib import Path

import pymupdf
import pytest
from PIL import Image

from latheport import registry
from latheport.detect import detect
from latheport.errors import ConversionError
from latheport.formats import COMMON_TARGETS, FORMATS, suggest
from latheport.pipeline import convert, extract

from .conftest import needs
from .samples import Samples, picture, write_rar


@pytest.fixture
def samples(tmp_path):
    return Samples(tmp_path / "samples")


# ---- comics

def _page_colors(pdf):
    """The red square's shade on every page: the sample pages are told apart by it."""
    with pymupdf.open(pdf) as doc:
        return [doc.extract_image(page.get_images()[0][0])["width"] for page in doc], doc.page_count


@pytest.mark.parametrize("fmt", ["cbz", "cb7", "cbt", "cbr"])
def test_comic_to_pdf(samples, tmp_path, fmt):
    pdf = convert(samples(fmt), "pdf", out_dir=tmp_path)
    with pymupdf.open(pdf) as doc:
        # 4 pages (ComicInfo.xml and __MACOSX junk skipped), page1 is the portrait one
        assert doc.page_count == 4
        assert doc[0].rect.height > doc[0].rect.width
        assert doc[1].rect.width > doc[1].rect.height


def test_comic_pages_in_reading_order(tmp_path):
    src = tmp_path / "order.cbz"
    with zipfile.ZipFile(src, "w") as z:
        for n in (10, 2, 1, 3):  # stored out of order; 10 must come last, not after 1
            img = picture((100 + n, 150))
            out = tmp_path / f"p{n}.png"
            img.save(out)
            z.write(out, f"Page {n}.png")
    with pymupdf.open(convert(src, "pdf")) as doc:
        assert [round(p.rect.width * 150 / 72) for p in doc] == [101, 102, 103, 110]


def test_jpeg_pages_are_not_recompressed(samples, tmp_path):
    cbz = samples("cbz")
    with zipfile.ZipFile(cbz) as z:
        original = z.read("Issue 1/page1.jpg")
    with pymupdf.open(convert(cbz, "pdf", out_dir=tmp_path)) as doc:
        assert doc.extract_image(doc[0].get_images()[0][0])["image"] == original


def test_pdf_to_cbz_and_back_is_lossless(samples, tmp_path):
    src = samples("cbz")
    pdf = convert(src, "pdf", out_dir=tmp_path / "a")
    cbz = convert(pdf, "cbz", out_dir=tmp_path / "b")
    with zipfile.ZipFile(src) as a, zipfile.ZipFile(cbz) as b:
        assert b.namelist() == ["001.jpg", "002.jpg", "003.jpg", "004.jpg"]
        # JPEG pages come back byte for byte (the PNG page has transparency, so it's flattened)
        assert b.read("001.jpg") == a.read("Issue 1/page1.jpg")
        assert b.read("002.jpg") == a.read("Issue 1/page2.jpg")
    with pymupdf.open(pdf) as a, pymupdf.open(convert(cbz, "pdf", out_dir=tmp_path / "c")) as b:
        assert [p.rect for p in a] == [p.rect for p in b]


def test_text_pdf_to_cbz_renders_pages(samples, tmp_path):
    cbz = convert(samples("pdf"), "cbz", out_dir=tmp_path)
    with zipfile.ZipFile(cbz) as z:
        assert z.namelist() == ["001.jpg", "002.jpg"]


def test_renamed_comics_detected_by_content(samples, tmp_path):
    fake_cbr = tmp_path / "really a zip.cbr"
    fake_cbr.write_bytes(samples("cbz").read_bytes())
    assert detect(fake_cbr) == "cbz"
    assert not registry.find_route("cbz", "pdf").missing_engines  # no 7-Zip needed
    with pymupdf.open(convert(fake_cbr, "pdf")) as doc:
        assert doc.page_count == 4
    fake_cbz = tmp_path / "really a rar.cbz"
    write_rar(fake_cbz, {"1.png": b""})
    assert detect(fake_cbz) == "cbr"


def test_cbr_to_cbz(samples, tmp_path):
    cbz = convert(samples("cbr"), "cbz", out_dir=tmp_path)
    with zipfile.ZipFile(cbz) as z:
        assert "Issue 1/page10.png" in z.namelist()


def test_cbr_is_created_as_stored_rar(samples, tmp_path, without_engine):
    import packport
    without_engine("winrar")  # packport, not WinRAR
    out = convert(samples("cbz"), "cbr", out_dir=tmp_path)
    assert out.read_bytes().startswith(b"Rar!\x1a\x07\x00")  # RAR 4, which every comic reader opens
    names = {m.name for m in packport.read_rar(out)}
    assert {"Issue 1/page1.jpg", "Issue 1/page10.png", "ComicInfo.xml"} <= names
    with pymupdf.open(convert(out, "pdf", out_dir=tmp_path / "pdf")) as doc:
        assert doc.page_count == 4


def test_folder_of_images_to_pdf(samples, tmp_path):
    with pymupdf.open(convert(samples("folder"), "pdf", out_dir=tmp_path)) as doc:
        assert doc.page_count == 2  # notes.txt is not a page


def test_comic_without_pages(tmp_path):
    src = tmp_path / "empty.cbz"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("readme.txt", "no pictures here")
    with pytest.raises(ConversionError, match="No page images"):
        convert(src, "pdf")


# ---- most common target

def test_every_common_target_is_reachable():
    for src, dst in COMMON_TARGETS.items():
        assert src in FORMATS and dst in registry.targets(src), f"{src} -> {dst}"


def test_suggest_falls_back_by_category():
    assert suggest("cbt", registry.targets("cbt")) == "pdf"
    assert suggest("tar.zst", registry.targets("tar.zst")) == "zip"
    assert suggest("exe", registry.targets("exe")) == "zip"
    assert suggest("zip", {"tar": None}) is None


# ---- subtitles

def test_srt_vtt_roundtrip(samples, tmp_path):
    vtt = convert(samples("srt"), "vtt", out_dir=tmp_path).read_text(encoding="utf-8")
    assert vtt.startswith("WEBVTT") and "00:00:01.000 --> 00:00:02.000" in vtt and "<i>there</i>" in vtt
    srt = convert(samples("vtt"), "srt", out_dir=tmp_path).read_text(encoding="utf-8")
    assert srt.splitlines()[:3] == ["1", "00:00:00,000 --> 00:00:01,000", "Hello"]  # voice tag stripped
    assert "2\n00:00:01,000 --> 00:00:02,500\nSecond line" in srt  # 'intro' cue id dropped, renumbered


@needs("ffmpeg")
def test_subtitles_out_of_a_video(samples, tmp_path):
    srt = convert(samples("mkv"), "srt", out_dir=tmp_path).read_text(encoding="utf-8")
    assert "Second line" in srt
    with pytest.raises(ConversionError, match="no subtitle track"):
        convert(samples("avi"), "srt", out_dir=tmp_path)


# ---- data

def test_jsonl_plist_sqlite(samples, tmp_path):
    rows = json.loads(convert(samples("jsonl"), "json", out_dir=tmp_path).read_text(encoding="utf-8"))
    assert rows[1]["name"] == "Linus"
    with convert(samples("json"), "plist", out_dir=tmp_path).open("rb") as f:
        assert plistlib.load(f)["people"][0]["age"] == 36
    db = json.loads(convert(samples("sqlite"), "json", out_dir=tmp_path / "db").read_text(encoding="utf-8"))
    assert db["people"][0] == {"name": "Ada", "age": 36, "photo": "<2 bytes>"} and db["cities"] == [{"name": "London"}]


def test_sqlite_detected_without_extension(samples, tmp_path):
    bare = tmp_path / "app.db"
    bare.write_bytes(samples("sqlite").read_bytes())
    assert detect(bare) == "sqlite"


def test_ini_keeps_case_and_default_section(tmp_path):
    ini = tmp_path / "app.ini"
    ini.write_text("[DEFAULT]\nTimeout = 5\n\n[Server]\nHostName = a\n", encoding="utf-8")
    assert json.loads(convert(ini, "json").read_text(encoding="utf-8")) == {
        "DEFAULT": {"Timeout": "5"}, "Server": {"HostName": "a"}}  # defaults not copied into every section
    loose = tmp_path / "loose.cfg"
    loose.write_text("key = value\n", encoding="utf-8")
    assert json.loads(convert(loose, "json").read_text(encoding="utf-8")) == {"DEFAULT": {"key": "value"}}


def test_csv_numbers_become_numbers_in_excel(tmp_path):
    from openpyxl import load_workbook
    src = tmp_path / "t.csv"
    src.write_text("id,zip,price\n1,02134,9.5\n", encoding="utf-8")
    ws = load_workbook(convert(src, "xlsx")).active
    assert [c.value for c in ws[2]] == [1, "02134", 9.5]


# ---- archives & images

@pytest.mark.parametrize("fmt", ["tar.zst", "zst"])
def test_zstd(samples, tmp_path, fmt):
    src = samples("folder") if fmt == "tar.zst" else samples("txt")
    out = extract(convert(src, fmt, out_dir=tmp_path), tmp_path / "x")
    assert (out / "notes.txt").exists() if fmt == "tar.zst" else out.read_text() == src.read_text()


@pytest.mark.parametrize("fmt", ["psd", "cur", "xpm", "sgi", "xbm"])
def test_more_images(samples, tmp_path, fmt):
    with Image.open(convert(samples(fmt), "png", out_dir=tmp_path)) as img:
        assert img.width > 0


def test_illustrator_and_pdf_to_svg(samples, tmp_path):
    assert convert(samples("ai"), "pdf", out_dir=tmp_path).read_bytes().startswith(b"%PDF")
    old_ai = tmp_path / "old.ai"
    old_ai.write_bytes(b"%!PS-Adobe-3.0\n%%Creator: Adobe Illustrator\n")
    with pytest.raises(ConversionError, match="PDF compatibility"):
        convert(old_ai, "pdf")
    svgs = convert(samples("pdf"), "svg", out_dir=tmp_path / "svg")
    assert svgs.is_dir() and all("<svg" in p.read_text() for p in svgs.iterdir())


def test_files_dated_before_1980_go_into_a_zip(tmp_path):
    # ZIP's DOS dates start in 1980; a file from an old RPM or tar dated 1970 used to stop the zip
    import os

    from latheport.converters.archives import compress
    folder = tmp_path / "old"
    folder.mkdir()
    (folder / "a.txt").write_text("from 1970")
    os.utime(folder / "a.txt", (0, 0))
    compress(folder, tmp_path / "out.zip", "zip")
    with zipfile.ZipFile(tmp_path / "out.zip") as z:
        assert z.read("old/a.txt") == b"from 1970"
        assert z.getinfo("old/a.txt").date_time[0] == 1980


@pytest.mark.parametrize("fmt", ["rar", "cab", "wim", "rpm", "squashfs", "lzh", "arj", "iso", "udf", "dmg", "vhd", "vhdx",
                                 "vmdk", "qcow2", "vdi", "deb", "xar", "pkg", "msi", "apfs", "hfs", "ar"])
def test_archives_without_an_extension_are_recognised(samples, tmp_path, fmt):
    import shutil
    src = samples(fmt)
    if src is None:
        pytest.skip(f"can't make a {fmt} sample here")
    bare = tmp_path / "download"
    shutil.copyfile(src, bare)
    assert detect(bare) == fmt


def test_a_lone_first_piece_says_the_others_are_missing(samples, tmp_path):
    # Only the .001 of a four-piece split: the message should say what's wrong, not just "damaged"
    import shutil
    lone = tmp_path / "lone" / "sample.001"
    lone.parent.mkdir()
    shutil.copyfile(samples("001"), lone)
    with pytest.raises(ConversionError, match=r"other pieces \(\.002, \.003"):
        extract(lone, tmp_path / "x")
    assert extract(samples("001"), tmp_path / "all").is_dir()  # all four pieces: it works


def _convert_on_windows_platform(src: Path, fmt: str, out_dir: Path) -> Path:
    """convert() in a child process on Qt's real platform: test_gui.py puts the session on Qt's
    offscreen one, which has no fonts on Windows (the app never uses it), and XPS text needs them."""
    import os
    import subprocess
    import sys
    env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"}
    code = (f"from latheport.pipeline import convert; print(convert(r'{src}', '{fmt}', out_dir=r'{out_dir}'))")
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=300, check=False)
    assert p.returncode == 0, p.stderr[-800:]
    return Path(p.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("fmt", ["xps", "oxps"])
def test_xps_becomes_a_pdf_with_its_shapes_and_searchable_text(samples, tmp_path, fmt):
    """XPS pages are drawn with Qt: the shapes where they were, and the text (glyphs from an
    embedded font) still there to search and copy."""
    from .samples import TEXT
    pdf = _convert_on_windows_platform(samples(fmt), "pdf", tmp_path)
    with pymupdf.open(pdf) as doc:
        assert doc.page_count == 2
        for i, page in enumerate(doc, 1):
            assert f"{TEXT} {i}." in page.get_text()
            pix = page.get_pixmap()
            r, g, b = pix.pixel(int(300 * 0.75), int(320 * 0.75))  # inside the page's blue rectangle
            assert (r, g, b) == (0x33, 0x66, 0xCC)


def test_xps_printed_by_windows_keeps_its_text(tmp_path):
    """A real XPS from Windows' XPS printer (libarchive's test file), its fonts obfuscated as the
    format has them: the text comes out the same as the printer wrote it."""
    src = Path(__file__).parent / "data" / "archives" / "test_compat_zip_7.xps"
    with pymupdf.open(_convert_on_windows_platform(src, "pdf", tmp_path)) as doc:
        assert "test XPS file created with Win XP XPS printer" in doc[0].get_text()


def test_xps_pages_are_read_once_and_a_broken_one_says_which(samples, tmp_path, monkeypatch):
    """Each page's XML was parsed twice (the first page three times); and a page that wasn't XML came out as a raw
    ParseError when it was any page but the first."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from latheport import qtdraw
    reads = []
    real = qtdraw.Xps._xml
    monkeypatch.setattr(qtdraw.Xps, "_xml", lambda self, name, base="/": (reads.append(name), real(self, name, base))[1])
    assert qtdraw.xps_to_pdf(samples("xps"), tmp_path / "a.pdf") == 2
    assert sorted(r for r in reads if r.endswith(".fpage")) == ["/Documents/1/Pages/1.fpage", "/Documents/1/Pages/2.fpage"]
    broken = tmp_path / "broken.xps"
    with zipfile.ZipFile(samples("xps")) as zin, zipfile.ZipFile(broken, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item)
            zout.writestr(item, b"<FixedPage" if item.filename.endswith("2.fpage") else data)
    with pytest.raises(ConversionError, match="page 2"):
        qtdraw.xps_to_pdf(broken, tmp_path / "b.pdf")
