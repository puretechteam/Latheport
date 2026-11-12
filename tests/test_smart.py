"""The smart tools: background removal, upscaling, OCR (picture to text, Make searchable) and speech
recognition (subtitles and transcripts), measured on inputs made here with a known answer: pictures with an
exact subject mask, pages drawn from known text, speech spoken by Windows' own voice. And their downloads:
pinned, checked, whole or not at all."""

from __future__ import annotations

import dataclasses
import hashlib
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from PySide6.QtWidgets import QLabel

from latheport import Report, engines, options, pipeline, registry, smart
from latheport.errors import ConversionError, EngineMissingError

FONTS = Path(__file__).resolve().parent.parent / "latheport" / "assets" / "fonts"
FFMPEG = engines.find("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="FFmpeg isn't installed")


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def qapp():
    from latheport.qtdraw import app as qt_app
    return qt_app()


@pytest.fixture
def win(qapp, tmp_path, monkeypatch):
    """The window, as a new user's (its settings in tmp_path)."""
    import gc

    from PySide6.QtCore import QSettings

    from latheport import gui
    monkeypatch.setenv("LATHEPORT_PRESETS", str(tmp_path / "presets.json"))
    store = QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat)
    monkeypatch.setattr(gui, "QSettings", lambda *a: store)
    gc.collect()  # earlier tests' windows go now, not whenever the collector next runs mid-test
    w = gui.Window()
    yield w
    w.close()


# ---- inputs with a known answer


def page(lines: list[str], size: int = 28, font: str = "Lora-Variable.ttf", tilt: float = 0) -> Image.Image:
    f = ImageFont.truetype(str(FONTS / font), size)
    img = Image.new("RGB", (int(size * 30), int(size * 2.2 * len(lines) + size * 3)), "white")
    d = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        d.text((size * 1.5, size * 1.5 + i * size * 2.2), line, fill=(20, 20, 20), font=f)
    if tilt:
        img = img.rotate(tilt, resample=Image.Resampling.BICUBIC, expand=True, fillcolor="white")
    return img


TEXT = ["The quick brown fox jumps over the lazy dog.", "Invoice 2026-0471: total due $1,284.50 by 30 November.",
        "Pack my box with five dozen liquor jugs, please.", "Sphinx of black quartz, judge my vow (page 12 of 40).",
        "Email support@example.com or call +1 555 0100."]


def edits(a, b) -> int:
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def cer(got: str, want: str) -> float:
    flat = lambda t: " ".join(line.strip() for line in t.splitlines() if line.strip())
    return edits(flat(got), flat(want)) / len(flat(want))


def composite(kind: str, w: int = 720, h: int = 560, seed: int = 1) -> tuple[Image.Image, np.ndarray]:
    """A shaded object (its exact mask known) over a busy, blurred background."""
    rng = np.random.default_rng(seed)
    m = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(m)
    if kind == "ball":
        d.ellipse((w * 0.3, h * 0.25, w * 0.72, h * 0.81), fill=255)
    elif kind == "mug":
        d.rounded_rectangle((w * 0.3, h * 0.3, w * 0.62, h * 0.8), radius=w * 0.03, fill=255)
        d.ellipse((w * 0.56, h * 0.4, w * 0.74, h * 0.66), fill=255)
        d.ellipse((w * 0.62, h * 0.46, w * 0.69, h * 0.6), fill=0)
    else:  # a vase
        pts = [(w * (0.5 + 0.09 + 0.08 * np.sin(np.pi * t * 1.3)), h * (0.15 + 0.72 * t)) for t in np.linspace(0, 1, 60)]
        d.polygon(pts + [(w - x, y) for x, y in reversed(pts)], fill=255)
    mask = np.asarray(m, np.float32) / 255
    yy, xx = np.mgrid[0:h, 0:w]
    ys, xs = np.nonzero(mask)
    cx, cy, span = xs.mean(), ys.mean(), max(np.ptp(xs), np.ptp(ys))
    lit = 0.55 + 0.45 * np.clip(1 - np.hypot(xx - cx + 0.25 * span, yy - cy + 0.3 * span) / span, 0, 1)
    colour = {"ball": (220, 60, 50), "mug": (240, 240, 235), "vase": (60, 90, 200)}[kind]
    fg = np.array(colour, np.float32)[None, None] * lit[:, :, None]
    y = np.linspace(0, 1, h)[:, None, None]
    bg = rng.uniform(40, 220, 3) * (1 - y) + rng.uniform(20, 200, 3) * y + np.zeros((h, w, 3))
    noise = Image.fromarray(rng.uniform(0, 255, (h // 16, w // 16, 3)).astype(np.uint8)).resize((w, h), Image.Resampling.BICUBIC)
    back = Image.fromarray(np.clip(bg * 0.6 + np.asarray(noise, np.float32) * 0.4, 0, 255).astype(np.uint8))
    db = ImageDraw.Draw(back)
    for _ in range(25):
        x0, y0, r = rng.uniform(0, w), rng.uniform(0, h), rng.uniform(w * 0.02, w * 0.12)
        db.ellipse((x0 - r, y0 - r, x0 + r, y0 + r), fill=tuple(int(v) for v in rng.uniform(0, 255, 3)))
    bg = np.asarray(back.filter(ImageFilter.GaussianBlur(w / 120)), np.float32)
    soft = np.asarray(m.filter(ImageFilter.GaussianBlur(1)), np.float32)[:, :, None] / 255
    pic = Image.fromarray((fg * soft + bg * (1 - soft)).round().astype(np.uint8))
    return pic, mask > 0.5


SPEECH = ("The museum opens at nine in the morning and closes at six. Tickets cost twelve dollars for adults and five "
          "for children. Please keep your bags in the lockers near the entrance. The second floor has paintings from "
          "the eighteenth century, and the third floor shows modern sculpture. Photography is allowed without a flash. "
          "If you need help, ask anyone wearing a blue badge.")
NUMBERS = {"9": "nine", "6": "six", "12": "twelve", "5": "five", "18th": "eighteenth"}


def words(t: str) -> list[str]:
    t = re.sub(r"\$(\d+)", r"\1 dollars", t.lower())
    return [NUMBERS.get(w, w) for w in re.sub(r"[^a-z0-9' ]", " ", t).split()]


def wer(got: str, want: str) -> float:
    return edits(words(got), words(want)) / len(words(want))


def _say(d: Path, rate: int = 0) -> Path:
    """SPEECH said by Windows' own voice (System.Speech) at rate (-10 to 10, 0 its usual), as a WAV in d."""
    if sys.platform != "win32":
        pytest.skip("Windows' voice makes the speech")
    (d / "say.txt").write_text(SPEECH, encoding="utf-8")
    wav = d / f"speech{rate}.wav"
    script = d / "say.ps1"
    script.write_text("Add-Type -AssemblyName System.Speech\n"
                      f"$s = New-Object System.Speech.Synthesis.SpeechSynthesizer\n$s.Rate = {rate}\n"
                      f"$s.SetOutputToWaveFile('{wav}')\n"
                      f"$s.Speak((Get-Content -Raw -Encoding UTF8 '{d / 'say.txt'}'))\n$s.Dispose()\n", encoding="utf-8")
    shell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    r = subprocess.run([str(shell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                       capture_output=True, creationflags=engines.NO_WINDOW, check=False)
    if r.returncode or not wav.exists():
        pytest.skip(f"Windows' voice isn't available: {r.stderr[-200:]!r}")
    return wav


@pytest.fixture(scope="module")
def spoken(tmp_path_factory) -> Path:
    return _say(tmp_path_factory.mktemp("speech"))


# ---- downloads: pinned, checked, whole or not at all


def _fake_tool(tmp_path, monkeypatch, payloads: dict[str, bytes], zipped: bool = False):
    """A tool whose files come from file:// addresses in tmp_path (nothing from the network)."""
    downloads = []
    if zipped:
        z = tmp_path / "model.zip"
        with zipfile.ZipFile(z, "w") as f:
            for name, data in payloads.items():
                f.writestr(f"inside/{name}", data)
            f.writestr("inside/unwanted.txt", b"left out")
        data = z.read_bytes()
        downloads.append(smart.Download(z.as_uri(), hashlib.sha256(data).hexdigest(), len(data),
                                        keep=tuple((f"inside/{n}", n) for n in payloads)))
    else:
        for name, data in payloads.items():
            f = tmp_path / f"src-{name}"
            f.write_bytes(data)
            downloads.append(smart.Download(f.as_uri(), hashlib.sha256(data).hexdigest(), len(data), name=name))
    tool = smart.Tool("ocr", "Text reader (OCR)", tuple(downloads), "MIT", "here")
    monkeypatch.setitem(smart.TOOLS, "ocr", tool)
    monkeypatch.setattr(smart, "MODELS", str(tmp_path / "models"))
    engines.refresh()
    return tool


def test_a_tool_is_downloaded_checked_and_kept_whole(tmp_path, monkeypatch):
    _fake_tool(tmp_path, monkeypatch, {"detect.onnx": b"one" * 100, "read.onnx": b"two" * 50})
    try:
        assert not smart.found("ocr") and smart.download_size("ocr") >= 450
        assert registry.targets("png")["txt"].missing_engines == ["ocr"]
        smart.download("ocr")
        assert sorted(os.listdir(tmp_path / "models" / "ocr")) == ["detect.onnx", "read.onnx"]
        assert (tmp_path / "models" / "ocr" / "read.onnx").read_bytes() == b"two" * 50
        engines.refresh()
        assert engines.available("ocr") and smart.download_size("ocr") == 0  # the runtime is pip's here
    finally:
        engines.refresh()


def test_a_download_that_doesnt_match_its_checksum_leaves_nothing(tmp_path, monkeypatch):
    tool = _fake_tool(tmp_path, monkeypatch, {"detect.onnx": b"one" * 100, "read.onnx": b"two" * 50})
    bad = dataclasses.replace(tool.downloads[1], sha256="0" * 64)
    monkeypatch.setitem(smart.TOOLS, "ocr", smart.Tool("ocr", tool.name, (tool.downloads[0], bad), "MIT", "here"))
    with pytest.raises(ConversionError, match="checksum"):
        smart.download("ocr")
    assert not (tmp_path / "models" / "ocr").exists()  # the first file, though fine, isn't kept alone
    assert os.listdir(tmp_path / "models") == []        # no part left behind either


def test_a_zipped_model_keeps_only_its_files_under_their_names(tmp_path, monkeypatch):
    _fake_tool(tmp_path, monkeypatch, {"m.onnx": b"graph", "m.data": b"weights"}, zipped=True)
    smart.download("ocr")
    assert sorted(os.listdir(tmp_path / "models" / "ocr")) == ["m.data", "m.onnx"]


def test_the_runtime_is_downloaded_for_the_built_app_without_its_tools(tmp_path, monkeypatch):
    """The built app has no ONNX Runtime: its wheel (for this Python) is fetched, checked, and only what
    inference needs is unpacked."""
    wheel = tmp_path / "ort.whl"
    with zipfile.ZipFile(wheel, "w") as z:
        z.writestr("onnxruntime/__init__.py", "MARK = 'downloaded'\n")
        z.writestr("onnxruntime/capi/onnxruntime.dll", b"dll")
        z.writestr("onnxruntime/tools/convert.py", "not wanted")
        z.writestr("onnxruntime/transformers/big.py", "not wanted")
        z.writestr("onnxruntime-1.31.0.dist-info/METADATA", "x")
    data = wheel.read_bytes()
    tag = f"cp{sys.version_info[0]}{sys.version_info[1]}"
    monkeypatch.setitem(smart.RUNTIME_WHEELS, tag, (wheel.as_uri(), hashlib.sha256(data).hexdigest(), len(data)))
    monkeypatch.setattr(engines, "ENGINE_HOME", str(tmp_path / "engines"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert not smart.runtime_found() and smart.runtime_size() == len(data)
    smart.download_runtime()
    got = Path(smart.runtime_folder()) / "onnxruntime"
    assert smart.runtime_found()
    assert sorted(p.relative_to(got).as_posix() for p in got.rglob("*") if p.is_file()) == [
        "__init__.py", "capi/onnxruntime.dll"]
    monkeypatch.setitem(smart.RUNTIME_WHEELS, tag, (wheel.as_uri(), "0" * 64, len(data)))
    monkeypatch.setattr(engines, "ENGINE_HOME", str(tmp_path / "engines2"))
    with pytest.raises(ConversionError, match="checksum"):
        smart.download_runtime()
    assert not Path(smart.runtime_folder()).exists()


def test_every_tool_is_pinned_by_address_and_checksum():
    for tool in smart.TOOLS.values():
        assert tool.downloads and tool.licence and tool.source
        for d in tool.downloads:
            assert d.url.startswith("https://") and re.fullmatch(r"[0-9a-f]{64}", d.sha256) and d.size > 0
            assert "/resolve/main/" not in d.url  # a commit, never a branch that can move
    for url, sha, size in smart.RUNTIME_WHEELS.values():
        assert url.startswith("https://files.pythonhosted.org/") and len(sha) == 64 and size > 1_000_000


def test_a_setting_that_needs_a_missing_tool_asks_for_it(tmp_path, png, monkeypatch):
    """No tool installed (the tests' default): the conversion stops before any step runs (a long one first, a
    RAW photo's, would be wasted), naming it, so the window and the command line can offer to install it."""
    ran = []
    route = registry.find_route("png", "webp")
    spied = [dataclasses.replace(e, func=lambda *a, real=e.func: ran.append(a) or real(*a)) for e in route.edges]
    monkeypatch.setattr(pipeline, "find_route", lambda *a, **k: dataclasses.replace(route, edges=spied))
    for key, value, tool in (("background", "remove", "background"), ("upscale", "2", "upscale")):
        with pytest.raises(EngineMissingError) as got:
            pipeline.convert(png, "webp", out_dir=tmp_path, options={key: value})
        assert got.value.engines == [tool]
    assert ran == [] and list(tmp_path.glob("*.webp")) == []
    assert smart.wanted({"background": "white", "upscale": "4", "quality": 80}, {"background", "quality"}) == ["background"]
    assert smart.wanted({"searchable": "yes"}, {"searchable"}) == ["ocr"]
    assert smart.wanted({"subtitles_from": "speech", "speech_subs": "burn"}, {"subtitles_from", "speech_subs"}) == ["speech"]


def test_the_install_offer_counts_the_runtime_once(monkeypatch):
    monkeypatch.setattr(smart, "runtime_found", lambda: False)
    first = smart.download_size("upscale")
    monkeypatch.setattr(smart, "runtime_found", lambda: True)
    assert first - smart.download_size("upscale") > 10_000_000  # ONNX Runtime (15 MB) only while it's missing
    assert engines.size_text("speech").endswith(" MB")


def test_sound_and_text_formats_dont_say_lossy():
    """Plain text and subtitles hold words only: no "TXT is lossy" or see-through line for OCR and speech."""
    for src, dst in (("png", "txt"), ("wav", "srt"), ("flac", "vtt"), ("mp4", "txt")):
        lines = [n.text for n in registry.targets(src)[dst].explain()]
        assert not any("lossy" in t or "transparency" in t for t in lines), (src, dst, lines)
    assert any("lossy" in n.text for n in registry.targets("png")["jpg"].explain())  # pictures still say it


def test_sound_to_subtitles_is_written_down_not_a_track_from_mka():
    """An MKA made from an MP3 has no subtitle track: sound to subtitles goes through speech recognition."""
    for dst in ("srt", "vtt", "ass", "txt"):
        route = registry.find_route("mp3", dst)
        assert [e.dst for e in route.edges] == [dst] and route.edges[0].engine == "speech"
    assert "mka" not in [e.dst for e in registry.find_route("mp3", "sbv").edges]


# ---- OCR


def test_ocr_reads_pages_in_two_fonts_and_three_sizes(smart_models):
    from latheport.smart import ocr
    rates = {}
    for font in ("Lora-Variable.ttf", "Inter-SemiBold.ttf"):
        for size in (16, 28, 44):
            rates[(font, size)] = cer(ocr.text(page(TEXT, size, font)), "\n".join(TEXT))
    assert max(rates.values()) < 0.03, rates
    assert sum(rates.values()) / len(rates) < 0.015, rates


def test_ocr_levels_a_page_that_leans_and_boxes_words_where_they_are(smart_models):
    from latheport.smart import ocr
    img = page(TEXT, 28, tilt=2.0)
    assert cer(ocr.text(img), "\n".join(TEXT)) < 0.03
    lines = ocr.read(img)
    assert len(lines) == len(TEXT)
    for line in lines:  # every word's box inside its line's, in order, on the picture as it is
        xs = [w.box[0] for w in line.words]
        assert xs == sorted(xs)
        for w in line.words:
            assert line.box[0] - 2 <= w.box[0] < w.box[2] <= line.box[2] + 2
    # a straight page: the word "quartz" boxed where it was drawn
    flat = page(TEXT, 28)
    word = next(w for ln in ocr.read(flat) for w in ln.words if "quartz" in w.text)
    f = ImageFont.truetype(str(FONTS / "Lora-Variable.ttf"), 28)
    left = 28 * 1.5 + f.getlength("Sphinx of black ")
    top = 28 * 1.5 + 3 * 28 * 2.2
    assert abs(word.box[0] - left) < 8 and abs(word.box[2] - (left + f.getlength("quartz,"))) < 12
    assert word.box[1] - 6 < top + 10 < word.box[3]


def test_ocr_paragraphs_are_kept_apart():
    from latheport.smart import ocr
    rows = [[ocr.Line(t, (10, y, 300, y + 20), 1.0)] for t, y in (("one", 0), ("two", 30), ("three", 60), ("four", 130))]
    from unittest import mock
    with mock.patch.object(ocr, "_rows", return_value=(rows, None)):
        assert ocr.text(Image.new("RGB", (10, 10))) == "one\ntwo\nthree\n\nfour"


def test_a_picture_becomes_its_text(tmp_path, smart_models):
    src = tmp_path / "note.png"
    page(TEXT, 30).save(src)
    report = Report()
    out = pipeline.convert(src, "txt", out_dir=tmp_path, report=report)
    assert cer(out.read_text(encoding="utf-8"), "\n".join(TEXT)) < 0.03
    assert any("OCR" in n for n in report.notes)
    blank = tmp_path / "blank.png"
    Image.new("RGB", (400, 300), "white").save(blank)
    with pytest.raises(ConversionError, match="No text"):
        pipeline.convert(blank, "txt", out_dir=tmp_path / "b")


def test_a_scanned_tiffs_pages_are_all_read(tmp_path, smart_models):
    src = tmp_path / "scan.tiff"
    a, b = page(TEXT[:2], 30), page(TEXT[2:], 30)
    a.save(src, save_all=True, append_images=[b])
    report = Report()
    text = pipeline.convert(src, "txt", out_dir=tmp_path, report=report).read_text(encoding="utf-8")
    assert cer(text.replace("\f", ""), "\n".join(TEXT)) < 0.03 and "\f" in text
    assert any("2 pages read" in n for n in report.notes)


# ---- Make searchable


def _scan_pdf(path: Path, pictures: list[tuple[Image.Image, int]]) -> Path:
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument.new()
    for img, turn in pictures:
        w, h = img.width * 72 / 150, img.height * 72 / 150
        p = pdf.new_page(w, h)
        obj = pdfium.PdfImage.new(pdf)
        obj.set_bitmap(pdfium.PdfBitmap.from_pil(img.convert("RGB")))
        obj.set_matrix(pdfium.PdfMatrix().scale(w, h))
        p.insert_obj(obj)
        p.gen_content()
        if turn:
            p.set_rotation(turn)
    pdf.save(path)
    return path


def _found(path: Path, word: str) -> list:
    """Where word is on each page (PDFium's own text reading), as a box on the page as it shows."""
    import pypdfium2 as pdfium
    out = []
    doc = pdfium.PdfDocument(path)
    for i in range(len(doc)):
        pg = doc[i]
        tp = pg.get_textpage()
        hit = tp.search(word).get_next()
        if hit is None:
            out.append(None)
        else:
            boxes = [tp.get_charbox(hit[0] + j) for j in range(hit[1])]
            x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
            x1, y1 = max(b[2] for b in boxes), max(b[3] for b in boxes)
            w = pg.get_width() if pg.get_rotation() in (0, 180) else pg.get_height()
            if pg.get_rotation() == 90:  # the page's own space to the page as it shows
                x0, y0, x1, y1 = y0, w - x1, y1, w - x0
            out.append((x0, y0, x1, y1))
        tp.close()
    doc.close()
    return out


def test_a_scan_is_made_searchable_and_its_words_lie_on_the_picture(tmp_path, smart_models):
    """Two scanned pages, one stored sideways with /Rotate 90: both read; a word found in each is in the same
    place on the page as it shows (within 3 points), and where the picture has it."""
    img = page(TEXT, 40)
    src = _scan_pdf(tmp_path / "scan.pdf", [(img, 0), (img.rotate(90, expand=True), 90)])
    assert _found(src, "quartz") == [None, None]
    report = Report()
    out = pipeline.convert(src, "pdf", out_dir=tmp_path / "out", options={"searchable": "yes"}, report=report)
    flat, turned = _found(out, "quartz")
    assert flat and turned
    assert max(abs(a - b) for a, b in zip(flat, turned)) < 3
    f = ImageFont.truetype(str(FONTS / "Lora-Variable.ttf"), 40)
    left = (40 * 1.5 + f.getlength("Sphinx of black ")) * 72 / 150
    assert abs(flat[0] - left) < 6
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(out)
    text = doc[0].get_textpage().get_text_range()
    assert cer(text.replace("\r\n", "\n"), "\n".join(TEXT)) < 0.03
    assert any("2 scanned pages read" in n for n in report.notes)
    assert {c.name: c.state for c in report.checks}.get("Pages") == "ok"


def test_pages_with_text_stay_as_they_are(tmp_path, smart_models):
    from tests.test_pdf_tools import _pdf
    src = _pdf(tmp_path / "typed.pdf", 3)
    report = Report()
    out = pipeline.convert(src, "pdf", out_dir=tmp_path / "out", options={"searchable": "yes"}, report=report)
    assert any("nothing to read" in n for n in report.notes)
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(out)
    assert [doc[i].get_textpage().get_text_range().strip() for i in range(3)] == ["Page 1", "Page 2", "Page 3"]


# ---- background


@pytest.mark.parametrize(("kind", "seed"), [("ball", 1), ("ball", 3), ("mug", 3), ("mug", 4), ("vase", 2), ("vase", 4)])
def test_the_background_is_found_around_the_subject(kind, seed, smart_models):
    """Backgrounds with things touching the subject (IS-Net took them in on all six: IoU 0.75 to 0.87)."""
    from latheport.smart import background
    pic, truth = composite(kind, seed=seed)
    found = background.subject(pic) > 0.5
    iou = (found & truth).sum() / (found | truth).sum()
    assert iou > 0.98, iou


def test_the_subjects_edges_are_fitted_to_the_picture(smart_models):
    """The model's 512-pixel map enlarged 3x: fitted to the picture's own edges, it's closer to the true soft
    edge than enlarged alone (on the 12 pictures it was swept on: 28% less error in a band around the edge)."""
    from latheport.smart import background
    plain, fitted = [], []
    for kind in ("ball", "vase", "mug"):
        pic, truth = composite(kind, 1600, 1250, seed=5)
        soft = np.asarray(Image.fromarray((truth * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(1)), np.float32) / 255
        t8 = Image.fromarray((truth * 255).astype(np.uint8))
        band = np.asarray(t8.filter(ImageFilter.MaxFilter(15)), bool) & ~np.asarray(t8.filter(ImageFilter.MinFilter(15)), bool)
        raw = background.found(pic.resize((background.SIDE, background.SIDE), Image.Resampling.BILINEAR))
        big = np.asarray(Image.fromarray(raw, "F").resize(pic.size, Image.Resampling.BILINEAR), np.float32)
        plain.append(np.abs(big - soft)[band].mean())
        fitted.append(np.abs(background.fitted(pic, raw) - soft)[band].mean())
    assert sum(fitted) < 0.85 * sum(plain), (plain, fitted)


def test_a_cut_out_edge_keeps_no_fringe_of_the_old_background(smart_models):
    """A red disc on green with a soft edge, put on white: where the edge is part disc, part white, its green is
    taken out with the old background (green and blue alike, as white and this red give them)."""
    from latheport.smart import background
    w, h = 640, 480
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).ellipse((180, 100, 460, 380), fill=255)
    soft = np.asarray(m.filter(ImageFilter.GaussianBlur(3)), np.float32)[:, :, None] / 255
    pic = Image.fromarray((np.array([220, 40, 40]) * soft + np.array([40, 200, 40]) * (1 - soft)).astype(np.uint8))
    out = np.asarray(background.changed(pic, "white"), np.float32)
    alpha = background.subject(pic)
    band = (alpha > 0.15) & (alpha < 0.85)
    assert band.sum() > 200
    fringe = (out[:, :, 1] - out[:, :, 2])[band].mean()  # green above blue: the old background showing
    before = (np.asarray(pic, np.float32)[:, :, 1] - np.asarray(pic, np.float32)[:, :, 2])[band].mean()
    assert fringe < 10 and before > 60, (fringe, before)


def test_background_settings_on_a_conversion(tmp_path, smart_models):
    pic, truth = composite("ball")
    src = tmp_path / "ball.jpg"
    pic.save(src, quality=95)
    report = Report()
    out = pipeline.convert(src, "png", out_dir=tmp_path / "a", options={"background": "remove"}, report=report)
    with Image.open(out) as img:
        a = np.asarray(img.getchannel("A"))
    assert img.mode == "RGBA" and a[5, 5] == 0 and a[truth].mean() > 245 and a[~truth].mean() < 10
    assert any("Background taken out" in n for n in report.notes)
    out = pipeline.convert(src, "jpg", out_dir=tmp_path / "w", options={"background": "white"})
    with Image.open(out) as img:
        px = np.asarray(img.convert("RGB"), np.float32)
    assert px[~truth].mean() > 245 and abs(px[truth].mean() - np.asarray(pic, np.float32)[truth].mean()) < 8
    out = pipeline.convert(src, "png", out_dir=tmp_path / "b", options={"background": "blur"})
    with Image.open(out) as img:
        px = np.asarray(img.convert("L"), np.float32)
    orig = np.asarray(pic.convert("L"), np.float32)
    edge = lambda p, m: np.abs(np.diff(p, axis=1))[m[:, 1:]].mean()
    assert edge(px, ~truth) < 0.6 * edge(orig, ~truth)       # the background softer...
    assert abs(edge(px, truth) - edge(orig, truth)) < 0.5     # ...the subject as it was
    note = [n.text for n in registry.find_route("png", "jpg").explain(options={"background": "remove"})]
    assert any("can't be see-through, so it's white" in n for n in note)


# ---- upscale


def _psnr(a: Image.Image, b: Image.Image) -> float:
    x, y = (np.asarray(i.convert("L"), np.float64) for i in (a, b))
    return float(10 * np.log10(255 ** 2 / max(((x - y) ** 2).mean(), 1e-9)))


def test_upscaled_text_comes_closer_to_the_original_than_resizing(smart_models):
    """Drawn text made 4x and 2x smaller, then big again: the upscaler beats bicubic resizing by PSNR."""
    from latheport.smart import upscale
    t = Image.new("RGB", (800, 520), "white")
    d = ImageDraw.Draw(t)
    for i, size in enumerate((18, 28, 44)):
        d.text((20, 20 + i * 150), "Sharp letters, 0123456789", fill=(10, 10, 10),
               font=ImageFont.truetype(str(FONTS / "Lora-Variable.ttf"), size))
    for factor, margin in ((4, 0.5), (2, 3.0)):
        small = t.resize((t.width // factor, t.height // factor), Image.Resampling.BICUBIC)
        big = upscale.bigger(small, factor)
        assert big.size == (small.width * factor, small.height * factor)
        ref = t.crop((0, 0, *big.size))
        assert _psnr(big, ref) > _psnr(small.resize(big.size, Image.Resampling.BICUBIC), ref) + margin


def test_tiles_leave_no_seams_and_see_through_stays(smart_models):
    """A smooth gradient through many tiles: no step where tiles meet; a see-through edge stays see-through."""
    from latheport.smart import upscale
    x = np.linspace(0, 255, 300)
    grad = np.dstack([np.tile(x, (240, 1)), np.tile(x[::-1], (240, 1)), np.full((240, 300), 128.0)]).astype(np.uint8)
    img = Image.fromarray(grad).convert("RGBA")
    img.putalpha(Image.fromarray(np.where(np.arange(300)[None, :].repeat(240, 0) < 40, 0, 255).astype(np.uint8)))
    big = upscale.bigger(img, 4)
    assert big.mode == "RGBA" and big.getpixel((10, 10))[3] == 0 and big.getpixel((600, 400))[3] == 255
    g = np.asarray(big.convert("RGB"), np.float32)[8:-8]
    steps = np.abs(np.diff(g, axis=1)).max(axis=2).mean(axis=0)  # each column's step to the next, on average
    seams = [(x + upscale.TILE - upscale.OVERLAP) * 4 - 1 for x in upscale.tiles(300)[:-1]]  # where tiles meet
    inside = np.median(steps[200:-40])
    assert len(seams) >= 2 and max(steps[s] for s in seams) < inside + 1.5, ([steps[s] for s in seams], inside)
    with pytest.raises(ConversionError, match="Too big to upscale"):
        upscale.check((6000, 5000), 2)


def test_upscaling_2x_needs_memory_for_2x_only(smart_models):
    """2x used to fill a 4x picture of float numbers first: 16 bytes a pixel of the 4x picture, about 60 of the
    result (a 20 megapixel photo at 2x, inside the limit, asked for 3.8 GB). Now 3 bytes a pixel of the result."""
    import tracemalloc

    from latheport.smart import upscale
    x = np.linspace(0, 255, 400)
    img = Image.fromarray(np.dstack([np.tile(x, (300, 1)), np.tile(x[::-1], (300, 1)),
                                     np.full((300, 400), 90.0)]).astype(np.uint8))
    upscale.bigger(Image.new("RGB", (32, 32)), 2)  # the model loaded before measuring
    for factor in (2, 4):
        tracemalloc.start()
        try:
            big = upscale.bigger(img, factor)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        result = big.width * big.height * 3
        assert big.size == (400 * factor, 300 * factor) and peak < 4 * result + 8_000_000, (factor, peak / result)


def test_upscale_on_a_conversion_says_the_new_size(tmp_path, smart_models):
    src = tmp_path / "small.png"
    composite("mug", 160, 120)[0].save(src)
    report = Report()
    out = pipeline.convert(src, "png", out_dir=tmp_path, options={"upscale": "2"}, report=report)
    with Image.open(out) as img:
        assert img.size == (320, 240)
    sizes = {c.name: c.state for c in report.checks}
    assert sizes["Width"] == sizes["Height"] == "expected"  # changed, as the notes say


# ---- speech


def test_speech_is_written_down_with_timings(spoken, smart_models):
    from latheport.smart import speech
    samples = speech.sound(spoken)
    lang, said = speech.transcribe(samples)
    assert lang == "en"
    assert wer(" ".join(s.text for s in said), SPEECH) < 0.05
    starts = [s.start for s in said]
    assert starts == sorted(starts) and said[-1].end <= len(samples) / speech.RATE + 0.5
    assert all(s.end >= s.start for s in said) and len(said) >= 5
    lang, _ = speech.transcribe(samples[:16000 * 8], language="en")
    assert lang == "en"


def test_fast_speech_is_cut_into_sentences_for_subtitles(tmp_path, smart_models):
    """Whisper's rules for its timestamps (in pairs, never going back) cut fast speech into its sentences: 9,
    against one 22-second block without them, no use as subtitles."""
    from latheport.smart import speech
    _, said = speech.transcribe(speech.sound(_say(tmp_path, rate=3)), "en")
    assert wer(" ".join(s.text for s in said), SPEECH) < 0.05
    assert len(said) >= 6 and max(s.end - s.start for s in said) < 8, [(s.start, s.end) for s in said]


def test_silence_says_nothing_was_heard(tmp_path, smart_models):
    from latheport.smart import speech
    _, said = speech.transcribe(np.zeros(16000 * 5, np.float32))
    assert said == []


@needs_ffmpeg
def test_sound_becomes_subtitles_and_a_transcript(tmp_path, spoken, smart_models):
    import pysubs2
    report = Report()
    srt = pipeline.convert(spoken, "srt", out_dir=tmp_path, report=report)
    subs = pysubs2.load(str(srt))
    assert wer(" ".join(e.plaintext.replace("\n", " ") for e in subs), SPEECH) < 0.05
    for e in subs:  # at most two lines of 42 characters on screen
        assert all(len(line) <= 42 for line in e.plaintext.split("\n")) and e.plaintext.count("\n") <= 1
        assert e.end > e.start
    assert any("Heard as English" in n for n in report.notes)
    txt = pipeline.convert(spoken, "txt", out_dir=tmp_path, options={"language": "en"}).read_text(encoding="utf-8")
    assert wer(txt, SPEECH) < 0.05


@needs_ffmpeg
def test_a_videos_speech_as_subtitles_burned_in_or_as_a_track(tmp_path, spoken, smart_models):
    from tests.test_av_tools import make
    video = make(tmp_path / "talk.mp4", "-f", "lavfi", "-i", "color=c=0x224466:s=640x360:d=40", "-i", str(spoken),
                 "-shortest", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac")
    import pysubs2
    srt = pipeline.convert(video, "srt", out_dir=tmp_path / "s", options={"subtitles_from": "speech"})
    assert wer(" ".join(e.plaintext.replace("\n", " ") for e in pysubs2.load(str(srt))), SPEECH) < 0.05
    with pytest.raises(ConversionError, match="no subtitle track.*subtitles-from speech"):
        pipeline.convert(video, "srt", out_dir=tmp_path / "n")
    mkv = pipeline.convert(video, "mkv", out_dir=tmp_path / "t", options={"speech_subs": "track"})
    from latheport.converters import media
    probe = subprocess.run([FFMPEG, "-hide_banner", "-i", str(mkv)], capture_output=True, text=True,
                           creationflags=engines.NO_WINDOW, check=False).stderr  # no output file: FFmpeg exits 1
    assert "Subtitle: subrip" in probe
    with pytest.raises(ConversionError, match="can't hold a subtitle track"):
        pipeline.convert(video, "avi", out_dir=tmp_path / "x", options={"speech_subs": "track"})
    plain = pipeline.convert(video, "mp4", out_dir=tmp_path / "p", options={"start": "3", "end": "20"})
    burned = pipeline.convert(video, "mp4", out_dir=tmp_path / "b",
                              options={"speech_subs": "burn", "start": "3", "end": "20"})

    def lower_third(path: Path, at: float) -> np.ndarray:
        raw = subprocess.run([FFMPEG, "-v", "error", "-ss", str(at), "-i", str(path), "-frames:v", "1", "-f", "rawvideo",
                              "-pix_fmt", "gray", "-"], capture_output=True, creationflags=engines.NO_WINDOW,
                             check=True).stdout
        return np.frombuffer(raw, np.uint8).reshape(360, 640)[260:, :].astype(np.float32)
    # 4 s into the cut (7 s in): a sentence is shown, white on the blue; the top of the picture is untouched
    assert (lower_third(burned, 4) > 200).sum() > 500 and (lower_third(plain, 4) > 200).sum() == 0
    assert media.probe(burned)["duration"] == pytest.approx(17, abs=0.2)


# ---- the window and the editor


def test_the_window_asks_to_install_a_smart_tool_a_setting_needs(monkeypatch, win, png):
    win.set_files([png])
    win.picker.set_current("webp")
    asked = []
    monkeypatch.setattr(win, "confirm_install", lambda keys: asked.append(keys) or False)
    monkeypatch.setattr(win.options_panel, "chosen", lambda: {"background": "remove", "quality": 80})
    win.start_convert()
    assert asked == [["background"]] and win.job is None  # declined: nothing runs
    box = win.install_box(["background"])
    assert "Background remover (" in " ".join(lab.text() for lab in box.findChildren(QLabel))


def test_the_editor_takes_the_background_out_and_copies_the_text(monkeypatch, tmp_path, qapp, smart_models):
    """Background in the editor's Adjust panel: shown on the stage (see-through where the background was) and
    kept as the file's setting; Copy the text in it puts the picture's text on the clipboard."""
    from PySide6.QtWidgets import QApplication

    from latheport.editor import EditorWindow
    from tests.test_gui import _pump
    photo = tmp_path / "ball.png"
    composite("ball", 640, 480, seed=2)[0].save(photo)
    note = tmp_path / "note.png"
    page(TEXT[:2], 30).save(note)
    e = EditorWindow([(photo, "png"), (note, "png")])
    try:
        e.choose_tool("adjust")
        assert e.smart_box.isVisibleTo(e)
        e.background.setCurrentIndex(e.background.findData("remove"))
        _pump(qapp, lambda: not e.jobs and e.stage.picture is not None and e.stage.picture.hasAlphaChannel()
              and e.stage.picture.pixelColor(5, 5).alpha() == 0, 30)
        assert e.stage.picture.pixelColor(5, 5).alpha() == 0  # the corner: background, see-through on the stage
        mid = e.stage.picture.pixelColor(e.stage.picture.width() // 2, e.stage.picture.height() // 2)
        assert mid.alpha() == 255
        assert e.edits.settings({photo: (640, 480)})[photo]["background"] == "remove"
        e.go(1)
        e.copy_text.click()
        _pump(qapp, lambda: not e.jobs and "Copied" in e.smart_note.text(), 30)
        assert cer(QApplication.clipboard().text(), "\n".join(TEXT[:2])) < 0.03
        assert e.smart_note.text().startswith("Copied 2 lines")
    finally:
        e.close()


def test_the_editor_asks_before_downloading_a_smart_tool(monkeypatch, qapp, tmp_path):
    """No tool installed: choosing a background asks first; declined, nothing is set or downloaded."""
    from latheport.editor import EditorWindow
    photo = tmp_path / "ball.png"
    composite("ball", 320, 240)[0].save(photo)
    e = EditorWindow([(photo, "png")])
    asked = []
    monkeypatch.setattr(e, "_ask", lambda title, text, yes: asked.append(text) or False)
    try:
        e.background.setCurrentIndex(e.background.findData("white"))
        assert len(asked) == 1 and "background remover (" in asked[0] and " MB)" in asked[0]
        assert "background" not in e.edit().look and e.background.currentData() is None  # back to As it is
    finally:
        e.close()


def test_closing_the_editor_while_a_smart_tool_downloads_doesnt_crash(qapp, tmp_path):
    """The editor waits 3 s for its work when it closes; a download goes on longer. Its thread is kept until it
    ends (a running thread's object let go ends the program), and what it says afterwards goes nowhere."""
    import gc
    import time

    from latheport.editor import window as editor
    photo = tmp_path / "ball.png"
    composite("ball", 320, 240)[0].save(photo)
    e = editor.EditorWindow([(photo, "png")])
    heard = []
    job = editor.SmartJob(lambda: time.sleep(4.5) or "installed")
    job.done.connect(lambda got, err: heard.append(got))
    e._keep(job)
    job.start()
    e.close()
    del e
    gc.collect()
    from latheport.editor import jobs
    assert job in jobs.RUNNING and job.isRunning()
    from tests.test_gui import _pump
    assert _pump(qapp, lambda: job not in jobs.RUNNING, 10)
    assert heard == []  # the closed window wasn't called


def test_edit_summary_names_the_background():
    from latheport.gui import edit_summary
    assert edit_summary({Path("a.png"): {"background": "blur", "colour": "bw"}}, 1) == "Black and white, background blurred"


def test_settings_for_smart_tools_are_known():
    for key in ("background", "upscale", "searchable", "subtitles_from", "speech_subs", "language"):
        assert key in options.SETTINGS
    assert options.SETTINGS["language"].choices[0] == "af"  # by name: Afrikaans first
    assert "speech_subs" in registry.find_route("mp4", "mkv").settings
    assert {"background", "upscale"} <= set(registry.find_route("jpg", "png").settings)
    assert "searchable" in registry.find_route("pdf", "pdf").settings
    assert "subtitles_from" in registry.find_route("mkv", "srt").settings


def test_inference_keeps_to_three_quarters_of_the_cores(monkeypatch):
    monkeypatch.setattr(os, "cpu_count", lambda: 16)
    assert smart.threads() == 12
    monkeypatch.setattr(os, "cpu_count", lambda: 1)
    assert smart.threads() == 1


def test_byte_table_decodes_whispers_tokens():
    from latheport.smart import speech
    table = speech._byte_table()
    assert len(table) == 256 and sorted(table.values()) == list(range(256))
    assert table["Ġ"] == 32 and table["a"] == ord("a")  # Ġ stands for a space
