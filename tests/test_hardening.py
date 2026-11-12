"""Hostile and broken files, built here: what a downloaded file must not be able to do (read other
files, fill memory, run a program from the Downloads folder), and messages instead of tracebacks."""

import gzip
import http.server
import io
import json
import shutil
import subprocess
import sys
import threading
import zipfile

import pytest
from bookport.limits import safe_path
from PIL import Image, PngImagePlugin

from latheport import assurance, engines, instance, pipeline, safety
from latheport.converters import maps, media
from latheport.errors import ConversionError

SECRET = "SECRET-TOKEN-4711"


def test_a_document_links_only_to_its_folder_or_one_up(tmp_path):
    (tmp_path / "secret.txt").write_text(SECRET)
    book = tmp_path / "book"
    (book / "Text").mkdir(parents=True)
    (book / "Images").mkdir()
    (book / "Images" / "a.png").write_bytes(b"png")
    (book / "Text" / "b.png").write_bytes(b"png")
    base = book / "Text"
    assert safe_path(base, "b.png") == (base / "b.png").resolve()
    assert safe_path(base, "../Images/a.png") == (book / "Images" / "a.png").resolve()
    assert safe_path(base, "b.png?x=1#top") == (base / "b.png").resolve()
    for bad in ("../../secret.txt", "..\\..\\secret.txt", "%2e%2e/%2e%2e/secret.txt", str(tmp_path / "secret.txt"),
                "C:/Windows/win.ini", "C:Windows/win.ini", "/Windows/win.ini", "file:///C:/Windows/win.ini",
                "//server/share/a.png", "\\\\server\\share\\a.png", "\\\\?\\C:\\Windows\\win.ini",
                "http://example.com/a.png", "b.png:stream", "b.png\0.txt"):
        assert safe_path(base, bad) is None, bad


# REL: two folders up, past the one allowed. ABS: the same folder by its full path. NET: a server here that counts requests.
HOSTILE_DOCS = [
    ("tex", r"\input{REL/secret.tex}"), ("tex", r"\input{ABS/secret.tex}"), ("tex", r"\include{REL/secret}"),
    ("tex", r"\includegraphics{REL/secret.png}"), ("tex", r"\includegraphics{ABS/secret.png}"),
    ("tex", r"\bibliography{REL/secret}\cite{k}"), ("tex", r"\includegraphics{NET/x.png}"),
    ("rst", ".. include:: REL/secret.txt"), ("rst", ".. include:: ABS/secret.txt"), ("rst", ".. literalinclude:: ABS/secret.txt"),
    ("rst", ".. image:: REL/secret.png"), ("rst", ".. raw:: html\n   :file: ABS/secret.txt"), ("rst", ".. csv-table:: x\n   :url: NET/x"),
    ("org", '#+INCLUDE: "REL/secret.txt"'), ("org", '#+INCLUDE: "ABS/secret.txt"'), ("org", "[[file:REL/secret.png]]"), ("org", "[[NET/x.png]]"),
    ("adoc", "include::REL/secret.txt[]"), ("adoc", "include::ABS/secret.txt[]"), ("adoc", "image::ABS/secret.png[]"), ("adoc", ":allow-uri-read:\ninclude::NET/x[]"),
    ("wiki", "[[File:REL/secret.png]]"), ("wiki", "[[File:ABS/secret.png]]"),
    ("typ", '#include "REL/secret.txt"'), ("typ", '#include "ABS/secret.txt"'), ("typ", '#image("REL/secret.png")'), ("typ", '#raw(read("ABS/secret.txt"))'),
    ("dbk", '<article xmlns:xi="http://www.w3.org/2001/XInclude"><xi:include href="REL/secret.txt" parse="text"/></article>'),
    ("dbk", '<article xmlns:xi="http://www.w3.org/2001/XInclude"><xi:include href="NET/x" parse="text"/></article>'),
    ("dbk", '<!DOCTYPE article [<!ENTITY x SYSTEM "file:///ABS/secret.txt">]><article><para>&x;</para></article>'),
    ("dbk", '<!DOCTYPE article [<!ENTITY x SYSTEM "NET/x">]><article><para>&x;</para></article>'),
    ("dbk", '<article><para><inlinemediaobject><imageobject><imagedata fileref="ABS/secret.png"/></imageobject></inlinemediaobject></para></article>'),
    ("man", ".so REL/secret.txt"), ("man", ".so ABS/secret.txt"),
    ("textile", "!REL/secret.png!"), ("textile", "!NET/x.png!"),
    ("md", "![x](REL/secret.png)"), ("md", "![x](ABS/secret.png)"), ("md", "![x](file:///ABS/secret.png)"), ("md", "![x](NET/x.png)"),
    ("html", '<img src="REL/secret.png">'), ("html", '<img src="ABS/secret.png">'), ("html", '<img src="NET/x.png">'),
    ("html", '<link rel="stylesheet" href="REL/secret.css">'), ("html", '<style>@import "REL/secret.css";</style>'),
]
# The same constructs at a file they may read: they must come through, or the test above proves nothing.
ALLOWED_DOCS = [(f, d.replace("REL", ".")) for f, d in HOSTILE_DOCS if "REL" in d]
DOC_AROUND = {"tex": "\\documentclass{article}\n\\begin{document}\nStart\n\nDOC\n\nEnd\n\\end{document}\n", "dbk": "DOC",
              "html": "<html><head></head><body><p>Start</p>DOC<p>End</p></body></html>", "man": ".TH A 1\n.SH NAME\nstart\nDOC\n"}


@pytest.fixture(scope="module")
def counting_server():
    requests = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(SECRET.encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", requests
    server.shutdown()
    server.server_close()


def _secret_files(folder):
    (folder / "secret.txt").write_text(SECRET)
    (folder / "secret.tex").write_text(SECRET)
    (folder / "secret.css").write_text(f"/*{SECRET}*/ p{{color:red}}")
    (folder / "secret.bib").write_text("@book{k, title={" + SECRET + "}, author={A}, year={2000}}")
    info = PngImagePlugin.PngInfo()
    info.add_text("m", SECRET)
    Image.new("RGB", (4, 4), "red").save(folder / "secret.png", pnginfo=info)


def _book_holds_secret(epub) -> bool:
    with zipfile.ZipFile(epub) as z:
        return any(SECRET.encode() in z.read(n) for n in z.namelist())


def _document_to_book(home, fmt, doc):
    src = home / f"doc.{'1' if fmt == 'man' else fmt}"
    src.write_text(DOC_AROUND.get(fmt, "Start\n\nDOC\n\nEnd\n").replace("DOC", doc), encoding="utf-8")
    out = home / "out"
    out.mkdir()
    return pipeline.convert(src, "epub", out_dir=out)


@pytest.mark.parametrize(("fmt", "doc"), HOSTILE_DOCS)
def test_a_document_cant_reach_other_files_or_a_server(tmp_path, counting_server, fmt, doc):
    url, requests = counting_server
    outside = tmp_path / "outside"
    outside.mkdir()
    _secret_files(outside)
    home = tmp_path / "a" / "b"
    home.mkdir(parents=True)
    requests.clear()
    doc = doc.replace("REL", "../../outside").replace("ABS", outside.as_posix()).replace("NET", url)
    assert not _book_holds_secret(_document_to_book(home, fmt, doc))
    assert not requests


@pytest.mark.parametrize(("fmt", "doc"), ALLOWED_DOCS)
def test_a_document_can_reach_what_is_beside_it(tmp_path, fmt, doc):
    _secret_files(tmp_path)
    assert _book_holds_secret(_document_to_book(tmp_path, fmt, doc))


def test_read_capped_stops_at_the_limit():
    assert safety.read_capped(io.BytesIO(b"x" * 10), "f", limit=10) == b"x" * 10
    with pytest.raises(safety.Refused, match="unpacks to more than"):
        safety.read_capped(io.BytesIO(b"x" * 11), "f", limit=10)


@pytest.mark.parametrize("src", [
    ("doc.rst", "Title\n=====\n\n.. include:: ../../secret.txt\n\nText.\n"),
    ("doc.adoc", "= Title\n\ninclude::../../secret.txt[]\n\nText.\n"),
    ("doc.tex", "\\documentclass{article}\\begin{document}\\input{../../secret.txt}Text.\\end{document}\n"),
    ("doc.org", "#+TITLE: Title\n#+INCLUDE: \"../../secret.txt\"\n\nText.\n"),
    ("doc.typ", "= Title\n#include \"../../secret.txt\"\n\nText.\n"),
])
def test_include_lines_cant_read_other_files(tmp_path, src):
    """An include (rST, AsciiDoc, LaTeX, Org, Typst) may read only from the document's folder or one
    up: a downloaded file can't copy another of your files into what it becomes, and the report
    says what was left out."""
    from latheport.assurance import Report
    (tmp_path / "secret.txt").write_text(SECRET)
    name, text = src
    doc = tmp_path / "a" / "b" / name
    doc.parent.mkdir(parents=True)
    doc.write_text(text, encoding="utf-8")
    for fmt in ("md", "html"):
        report = Report()
        out = pipeline.convert(doc, fmt, tmp_path / fmt, report=report)
        assert SECRET not in out.read_text(encoding="utf-8")
        assert "Text." in out.read_text(encoding="utf-8")
        assert any("secret.txt" in n for n in report.notes), report.notes


@pytest.mark.parametrize("fmt", ["docx", "odt", "epub", "html"])
def test_pictures_come_only_from_the_folder(tmp_path, fmt):
    folder = tmp_path / "doc"
    (folder / "img").mkdir(parents=True)
    Image.new("RGB", (8, 8), "red").save(folder / "img" / "pic.png")
    Image.new("RGB", (8, 8), "blue").save(tmp_path / "outside.png")
    src = folder / "doc.rst"
    src.write_text("Title\n=====\n\n.. image:: img/pic.png\n\n.. image:: ../../outside.png\n", encoding="utf-8")
    out = pipeline.convert(src, fmt, tmp_path / "out")
    if fmt == "html":
        text = out.read_text(encoding="utf-8")
        assert text.count("<img") == 1
    else:
        with zipfile.ZipFile(out) as z:
            assert sum(n.lower().endswith(".png") for n in z.namelist()) == 1


def test_text_markup_keeps_its_pictures_beside_it(tmp_path):
    folder = tmp_path / "doc"
    folder.mkdir()
    Image.new("RGB", (8, 8), "red").save(folder / "pic.png")
    src = folder / "doc.md"
    src.write_text("# Title\n\n![a red square](pic.png)\n\nText.\n", encoding="utf-8")
    out = pipeline.convert(src, "tex", tmp_path / "out")
    assert out.is_dir() and (out / "doc.tex").is_file()
    assert [p.name for p in (out / "images").iterdir()] == ["pic.png"]
    assert "images/pic.png" in (out / "doc.tex").read_text(encoding="utf-8")


def test_svgz_bomb_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(safety, "INNER_LIMIT", 1 << 20)
    bomb = tmp_path / "bomb.svgz"
    bomb.write_bytes(gzip.compress(b"<svg xmlns='http://www.w3.org/2000/svg'>" + b" " * (3 << 20) + b"</svg>"))
    with pytest.raises(ConversionError, match="unpacks to more than"):
        pipeline.convert(bomb, "pdf", tmp_path / "out")


def test_damaged_svgz_says_so(tmp_path):
    bad = tmp_path / "bad.svgz"
    bad.write_bytes(gzip.compress(b"<svg/>" * 100)[:30])
    with pytest.raises(ConversionError, match="damaged"):
        pipeline.convert(bad, "pdf", tmp_path / "out")


def test_kmz_bomb_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(safety, "INNER_LIMIT", 1 << 20)
    bomb = tmp_path / "bomb.kmz"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("doc.kml", b"<kml>" + b" " * (3 << 20) + b"</kml>")
    with pytest.raises(ConversionError, match="unpacks to more than"):
        maps.read_kmz(bomb)


@pytest.mark.parametrize("data", [b"[1, 2]", b'{"type": "Feature", "geometry": {"type": "Point", "coordinates": ["a", "b"]}}',
                                  b'{"type": "FeatureCollection", "features": [{"type": "Feature", "properties": 5}]}',
                                  b'{"type": "Feature", "geometry": {"type": "LineString", "coordinates": 7}}'])
def test_odd_geojson_gets_a_message(data):
    with pytest.raises(ConversionError, match="GeoJSON"):
        maps.read_geojson(data)


def test_programs_in_the_current_folder_are_never_used(tmp_path, monkeypatch):
    """Windows runs a program from the current folder before PATH; a Downloads folder holding a
    fake "ffmpeg.exe" must not be picked up."""
    shutil.copy(sys.executable, tmp_path / "ca-test-engine.exe")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)  # as if someone took it away
    monkeypatch.setenv("PATH", r"C:\Windows")
    monkeypatch.setattr(engines, "_registry_path", lambda: "")
    monkeypatch.setitem(engines.ENGINES, "catest", engines.Engine("catest", "Test", "ca-test-engine", "", ""))
    engines.refresh()
    try:
        assert engines.find("catest") is None
    finally:
        engines.refresh()


def test_ffmpeg_reads_local_files_only(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(engines, "require", lambda key: key)
    monkeypatch.setattr(engines, "run", lambda args, **kw: calls.append(args))
    media.ffmpeg(tmp_path / "a.m3u8", tmp_path / "a.mp4", [])
    args = calls[0]
    assert args[args.index("-protocol_whitelist") + 1] == "file"
    assert args.index("-protocol_whitelist") < args.index("-i")


def test_speech_recognition_reads_local_files_only(tmp_path, monkeypatch):
    """An .mp3 that is really a playlist of web addresses: FFmpeg would fetch them while reading its sound."""
    from latheport.smart import speech
    seen = []

    class Stop(Exception):
        pass

    def fake_popen(args, **kw):
        seen.append(args)
        raise Stop
    monkeypatch.setattr(engines, "require", lambda key: key)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    with pytest.raises(Stop):
        speech.sound(tmp_path / "talk.mp3")
    args = seen[0]
    assert args[args.index("-protocol_whitelist") + 1] == "file"
    assert args.index("-protocol_whitelist") < args.index("-i")


def test_checking_audio_opens_no_console_window(tmp_path, monkeypatch):
    seen = {}

    def fake_run(args, **kw):
        seen.update(kw, args=args)
        return subprocess.CompletedProcess(args, 0, json.dumps(
            {"format": {"duration": "1.0"}, "streams": [{"codec_type": "audio"}]}), "")
    monkeypatch.setattr(assurance, "_ffprobe", lambda: "ffprobe")
    monkeypatch.setattr(assurance.subprocess, "run", fake_run)
    assert assurance._media(tmp_path / "a.mp3", "mp3")["audio_tracks"] == 1
    assert seen["creationflags"] == engines.NO_WINDOW
    assert seen["encoding"] == "utf-8"
    assert "-protocol_whitelist" in seen["args"]


@pytest.mark.parametrize("name, head", [
    ("bad.exr", b"\x76\x2f\x31\x01\x02\x00\x00\x00"),
    ("bad.hdr", b"#?RADIANCE\nFORMAT=32-bit_rle_rgbe\n\n-Y 99999999 +X 99999999\n"),
    ("bad.jxl", b"\xff\x0a\xfa\x7f"),
    ("bad.xcf", b"gimp xcf v011\x00\x7f\xff\xff\xff\x7f\xff\xff\xff"),
    ("bad.dng", b"II*\x00\x08\x00\x00\x00\xff\xff"),
])
def test_damaged_pictures_are_refused_plainly(tmp_path, name, head):
    """RAW, EXR, HDR, JPEG XL and GIMP files that are cut short or claim impossible sizes stop with
    a message (no crash, no hang, no huge allocation)."""
    src = tmp_path / name
    src.write_bytes(head + bytes(64))
    with pytest.raises(ConversionError):
        pipeline.convert(src, "png", tmp_path / "out")


def test_a_launch_never_opens_a_second_window(monkeypatch, tmp_path):
    """The open window holds the lock but doesn't answer: say so instead of opening another."""
    from PySide6.QtCore import QLockFile
    monkeypatch.setattr(instance, "SERVER_NAME", "Latheport-test-busy")
    monkeypatch.setattr(instance.tempfile, "gettempdir", lambda: str(tmp_path))
    held = QLockFile(str(tmp_path / "Latheport-test-busy.lock"))
    assert held.tryLock(0)
    told = []
    monkeypatch.setattr(instance, "WAIT_S", 0.3)
    monkeypatch.setattr(instance, "_busy", lambda: told.append(True))
    monkeypatch.setattr(instance, "forward", lambda *a, **k: False)
    from latheport import gui
    monkeypatch.setattr(gui, "run", lambda *a: pytest.fail("opened a second window"))
    assert instance.main([]) == 1
    assert told == [True]
    held.unlock()


def _red(img: Image.Image) -> int:
    return sum(1 for r, g, b in img.convert("RGB").get_flattened_data() if r > 200 and g < 60 and b < 60)


@pytest.mark.parametrize("how", ["absolute", "file-uri", "relative", "entity", "css-import"])
@pytest.mark.parametrize("dst", ["png", "pdf"])
def test_an_svg_cannot_draw_a_file_from_elsewhere_on_the_computer(tmp_path, how, dst, monkeypatch):
    """An SVG's pictures come from inside it (data: URIs). Qt's SVG renderer would load an
    <image> by absolute path, so a downloaded SVG could copy any picture on the computer into
    its result."""
    from latheport import pdfium
    secret = tmp_path / "secret.png"
    Image.new("RGB", (100, 100), (255, 0, 0)).save(secret)
    docs = tmp_path / "a" / "b" / "docs"
    docs.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)  # a relative name mustn't be found in the working folder either
    ref = {"absolute": str(secret), "file-uri": secret.as_uri(), "relative": "secret.png",
           "entity": "&p;", "css-import": "#none"}[how]
    head = f'<!DOCTYPE svg [<!ENTITY p "{secret}">]>' if how == "entity" else ""
    style = f'<style>@import url("{secret.as_uri()}"); @import "{secret}";</style>' if how == "css-import" else ""
    svg = docs / "picture.svg"
    svg.write_text(f'<?xml version="1.0"?>{head}<svg xmlns="http://www.w3.org/2000/svg" '
                   f'xmlns:xlink="http://www.w3.org/1999/xlink" width="100" height="100">{style}'
                   f'<image width="100" height="100" xlink:href="{ref}" href="{ref}"/></svg>', encoding="utf-8")
    out = pipeline.convert(svg, dst, out_dir=tmp_path / "out")
    if dst == "png":
        assert _red(Image.open(out)) == 0
    else:
        with pdfium.opened(out) as doc:
            assert _red(pdfium.render(doc[0], 1)) == 0


def test_an_svg_keeps_its_own_pictures(tmp_path):
    """A picture inside the SVG (a data: URI) is still drawn."""
    import base64
    buf = io.BytesIO()
    Image.new("RGB", (100, 100), (255, 0, 0)).save(buf, "PNG")
    uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    svg = tmp_path / "own.svg"
    svg.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
                   f'width="100" height="100"><image width="100" height="100" xlink:href="{uri}"/></svg>',
                   encoding="utf-8")
    assert _red(Image.open(pipeline.convert(svg, "png", out_dir=tmp_path / "out"))) > 1000


@pytest.mark.parametrize("dst", ["png", "cbz"])
def test_a_huge_pdf_page_is_drawn_at_a_size_that_fits(tmp_path, dst, monkeypatch):
    """A PDF page can be 200 inches square: at 150 dpi that's 30,000 pixels a side, 3.6 GB to
    draw, from a file of a few hundred bytes. Pages are drawn no bigger than PAGE_PIXELS."""
    import pypdfium2

    from latheport.pdfium import PAGE_PIXELS
    doc = pypdfium2.PdfDocument.new()
    doc.new_page(14400, 14400)
    src = tmp_path / "huge.pdf"
    doc.save(str(src))
    doc.close()
    out = pipeline.convert(src, dst, out_dir=tmp_path / "out")
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", None)  # the picture is big on purpose, to measure
    if dst == "png":
        with Image.open(out) as img:
            assert img.width * img.height <= PAGE_PIXELS
    else:
        with zipfile.ZipFile(out) as z, Image.open(io.BytesIO(z.read(z.namelist()[0]))) as img:
            assert img.width * img.height <= PAGE_PIXELS
