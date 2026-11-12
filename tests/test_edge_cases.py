"""Files and moments that used to go wrong, each found by reading the code through: data that expands or
loses parts, names that come out wrong, a result too big to check, paths with quotes in them, Cancel inside
a long step, a stopped copy, a slow engine, text with no BOM, two programs writing into one folder."""

from __future__ import annotations

import gzip
import io
import json
import subprocess

import numpy as np
import pytest
from PIL import Image, ImageSequence

from latheport import assurance, cancel, engines, pipeline, registry
from latheport.converters import data, email, media, pdf
from latheport.errors import ConversionError
from latheport.textfile import decode

# ---- data

def test_a_yaml_alias_bomb_is_refused_and_ordinary_aliases_still_work(tmp_path):
    """281 bytes whose aliases nest six deep (each naming the one before ten times) load as shared objects but
    write out as 13 MB of JSON; three more levels would be 13 GB, written in-process where Cancel can't stop it."""
    lines, prev = ['a: &a ["x","x","x","x","x","x","x","x","x","x"]'], "a"
    for i in range(1, 7):
        name = chr(ord("a") + i)
        lines.append(f"{name}: &{name} [" + ",".join(f"*{prev}" for _ in range(10)) + "]")
        prev = name
    bomb = tmp_path / "bomb.yaml"
    bomb.write_text("\n".join(lines), encoding="utf-8")
    with pytest.raises(ConversionError, match="aliases expand to 12,345,685 values"):
        pipeline.convert(bomb, "json", out_dir=tmp_path / "out")
    assert not (tmp_path / "out" / "bomb.json").exists()
    ok = tmp_path / "ok.yaml"
    ok.write_text("base: &b {x: 1, y: 2}\none: {<<: *b, z: 3}\nlist: [*b, *b]\n", encoding="utf-8")
    assert data.load(ok, "yaml") == {"base": {"x": 1, "y": 2}, "one": {"x": 1, "y": 2, "z": 3},
                                     "list": [{"x": 1, "y": 2}, {"x": 1, "y": 2}]}
    loop = tmp_path / "loop.yaml"
    loop.write_text("&a [*a]\n", encoding="utf-8")
    with pytest.raises(ConversionError, match="refers to itself"):
        data.load(loop, "yaml")


def test_columns_with_the_same_name_are_all_kept(tmp_path):
    src = tmp_path / "t.csv"
    src.write_text("name,value,value\nx,1,2\n", encoding="utf-8")
    assert data.load(src, "csv") == [{"name": "x", "value": "1", "value 2": "2"}]  # was {"value": "2"} only
    from openpyxl import Workbook
    wb = Workbook()
    wb.active.append(["name", "value", "value"])
    wb.active.append(["x", 1, 2])
    wb.save(tmp_path / "t.xlsx")
    assert data.load(tmp_path / "t.xlsx", "xlsx") == [{"name": "x", "value": 1, "value 2": 2}]


def test_xml_keeps_the_text_after_a_child_and_toml_says_nulls_go(tmp_path):
    src = tmp_path / "p.xml"
    src.write_text("<p>Hello <b>world</b> again</p>", encoding="utf-8")
    assert data.load(src, "xml") == {"p": {"b": "world", "#text": "Hello again"}}  # " again" was lost
    notes = [n.text for n in registry.find_route("json", "toml").explain()]
    assert "TOML has no empty (null) values: they are left out" in notes
    assert not any("null" in n.text for n in registry.find_route("ini", "toml").explain())


# ---- pictures

def test_each_frame_of_an_animation_keeps_its_own_time(tmp_path):
    """A GIF of 100, 2000 and 100 ms frames became a WebP of three 100 ms frames (2.2 s became 0.3 s)."""
    frames = [Image.new("RGB", (32, 32), c) for c in ("red", "green", "blue")]
    frames[0].save(tmp_path / "a.gif", save_all=True, append_images=frames[1:], duration=[100, 2000, 100], loop=0)
    for fmt in ("webp", "png", "gif"):
        out = pipeline.convert(tmp_path / "a.gif", fmt, out_dir=tmp_path / fmt)
        with Image.open(out) as img:
            times = []
            for f in ImageSequence.Iterator(img):
                f.load()  # WebP says a frame's time once it's decoded
                times.append(int(f.info["duration"]))
        assert times == [100, 2000, 100], fmt


# ---- names and places

def test_one_compressor_to_another_keeps_the_name(tmp_path):
    src = tmp_path / "notes.txt.gz"
    src.write_bytes(gzip.compress(b"hello"))
    out = pipeline.convert(src, "xz", out_dir=tmp_path / "out")
    assert out.name == "notes.txt.xz"  # was notes.txt.gz.xz, which extracted to a text file named notes.txt.gz
    assert pipeline.extract(out, tmp_path / "x", "xz").name == "notes.txt"


def test_two_programs_writing_one_name_at_once_dont_overwrite(tmp_path, monkeypatch):
    """The window and `latheport watch` both finishing a.png into one folder: the name looked up as free is
    taken before the move. The one that came second gets 'a (1).png'; nothing is overwritten."""
    item = tmp_path / "work" / "a.png"
    item.parent.mkdir()
    item.write_bytes(b"ours")
    out = tmp_path / "out"
    out.mkdir()
    real, calls = pipeline.unique_path, []

    def racy(path):
        got = real(path)
        if not calls:
            got.write_bytes(b"theirs")  # the other program writes it now
        calls.append(got)
        return got
    monkeypatch.setattr(pipeline, "unique_path", racy)
    final = pipeline.place(item, out / "a.png")
    assert (out / "a.png").read_bytes() == b"theirs" and final.name == "a (1).png" and final.read_bytes() == b"ours"


def test_full_paths_are_cut_to_names_one_by_one():
    assert pipeline.names_only(r"Couldn't rename C:\a\b.txt to C:\c\d.txt") == "Couldn't rename b.txt to d.txt"
    assert pipeline.names_only(r"No such file: 'C:\Users\Bob Smith\a b.png'") == "No such file: 'a b.png'"


# ---- checks

def test_a_result_too_big_to_check_again_isnt_called_broken(tmp_path, monkeypatch):
    """A CSV, JSON or XLSX result between 50 and 200 MB was reported as "doesn't open"."""
    src = tmp_path / "t.json"
    src.write_text(json.dumps([{"a": i} for i in range(50)]), encoding="utf-8")
    monkeypatch.setattr(assurance, "DATA_LIMIT", 100)
    assert assurance.inspect(src, "json") is None
    report = assurance.Report()
    pipeline.convert(src, "csv", out_dir=tmp_path / "out", report=report)
    assert not [c for c in report.checks if c.state == "error"], report.checks


def test_a_note_explains_only_the_count_it_changes():
    before, after = {"pages": 5, "files": 3}, {"pages": 4, "files": 3}
    joined = registry.Note("3 files joined")
    checks = {c.name: c for c in assurance.verify(before, after, [], [joined])}
    assert checks["Pages"].state == "warning"  # "joined" says nothing about pages
    stopped = registry.Note("The program stopped early", ("pages",))
    checks = {c.name: c for c in assurance.verify(before, after, [], [joined, stopped])}
    assert checks["Pages"].state == "expected" and checks["Pages"].why == "The program stopped early"


# ---- FFmpeg filters

def test_paths_with_quotes_brackets_and_commas_reach_ffmpegs_filters(tmp_path):
    """The overlay picture and burned-in subtitles live in the temp folder, which holds the user's name: a path
    with an apostrophe ended FFmpeg's quoted value early ("No such file or directory")."""
    ff = engines.find("ffmpeg")
    if not ff:
        pytest.skip("needs FFmpeg")
    for name in ("Bob's files", "a,b;c [x]"):
        d = tmp_path / name
        d.mkdir()
        Image.new("RGBA", (16, 16), (255, 0, 0, 255)).save(d / "o.png")
        (d / "s.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n", encoding="utf-8")
        graph = (f"movie={media._filter_path(str(d / 'o.png'))}[ov];[0:v][ov]overlay=0:0,"
                 f"subtitles={media._filter_path(str(d / 's.srt'))}:fontsdir={media._filter_path(str(d))}")
        r = subprocess.run([ff, "-hide_banner", "-nostdin", "-y", "-f", "lavfi", "-i", "color=c=blue:s=64x64:d=0.2",
                            "-filter_complex", graph, str(d / "out.mp4")], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120, check=False)
        assert r.returncode == 0, (name, r.stderr[-400:])
        with Image.open(d / "o.png"):
            pass


# ---- Cancel inside a long step

def _cancel_after(n: int, token: cancel.Token):
    calls = []

    def tick(*a, **k):
        calls.append(1)
        if len(calls) == n:
            token.cancel()
    return calls, tick


def test_cancel_stops_a_pdf_between_pages(tmp_path, monkeypatch):
    pages = [Image.new("RGB", (60, 80), c) for c in ("red", "green", "blue", "white")]
    pages[0].save(tmp_path / "four.pdf", save_all=True, append_images=pages[1:])
    token = cancel.Token()
    calls, tick = _cancel_after(1, token)
    real = pdf._save_page
    monkeypatch.setattr(pdf, "_save_page", lambda *a: (real(*a), tick()))
    with cancel.using(token), pytest.raises(cancel.Cancelled):
        pipeline.convert(tmp_path / "four.pdf", "png", out_dir=tmp_path / "out")
    assert len(calls) == 1  # all four pages were drawn before the stop was seen


def test_cancel_stops_the_upscaler_between_tiles(monkeypatch):
    from latheport.smart import upscale

    class Model:
        def get_inputs(self):
            return [type("I", (), {"name": "x"})()]

        def run(self, _, feed):
            tick()
            return [np.zeros((1, 3, upscale.TILE * 4, upscale.TILE * 4), np.float32)]
    token = cancel.Token()
    calls, tick = _cancel_after(1, token)
    monkeypatch.setattr(upscale, "session", lambda *a: Model())
    with cancel.using(token), pytest.raises(cancel.Cancelled):
        upscale.bigger(Image.new("RGB", (600, 400)), 2)
    assert len(calls) == 1  # 24 tiles


def test_upscaling_a_palette_picture_with_a_see_through_colour(monkeypatch):
    from latheport.smart import upscale

    class Model:
        def get_inputs(self):
            return [type("I", (), {"name": "x"})()]

        def run(self, _, feed):
            return [np.zeros((1, 3, upscale.TILE * 4, upscale.TILE * 4), np.float32)]
    monkeypatch.setattr(upscale, "session", lambda *a: Model())
    img = Image.new("P", (40, 30), 0)
    img.info["transparency"] = 0
    big = upscale.bigger(img, 2)
    assert big.size == (80, 60) and big.mode == "RGBA" and big.getchannel("A").getextrema() == (0, 0)


def test_a_copy_that_stops_part_way_leaves_no_half_file(tmp_path):
    from latheport.safety import Budget, Refused, copy_capped

    class Broken(io.RawIOBase):
        def __init__(self):
            self.sent = 0

        def readable(self):
            return True

        def read(self, n=-1):
            if self.sent:
                raise OSError("Bad CRC-32 for file 'a.bin'")  # zipfile's check at the end of an entry
            self.sent = 1
            return b"x" * (1 << 20)
    target = tmp_path / "a.bin"
    with pytest.raises(OSError, match="CRC"):
        copy_capped(Broken(), target, None, Budget(tmp_path, "a.zip"), "a.bin")
    assert not target.exists()
    with pytest.raises(Refused):
        copy_capped(io.BytesIO(b"y" * 3_000_000), target, 1_000_000, Budget(tmp_path, "a.zip"), "a.bin")
    assert not target.exists()


# ---- engines, text, mail

def test_an_engine_that_takes_too_long_says_so_in_words():
    import sys
    with pytest.raises(ConversionError) as got:
        engines.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)
    assert str(got.value) == "python took longer than 1 second, so it was stopped"  # was the whole command line


def test_utf16_without_a_bom_is_read_as_utf16():
    text = "name,city\nJos\u00e9,Z\u00fcrich\n"
    for encoding in ("utf-16-le", "utf-16-be", "utf-16", "utf-8", "utf-8-sig", "cp1252"):
        assert decode(text.encode(encoding)) == text, encoding


def test_a_mails_page_runs_nothing_and_fetches_nothing():
    _, body = email._body_html('<html><body><img src="https://t.example/p.gif" alt="logo"><img src="cid:abc">'
                               '<a href="javascript:alert(1)">x</a><a href="https://ok.example">ok</a>'
                               '<p onclick="y()" style="color:red">t</p>'
                               '<meta http-equiv="refresh" content="0;url=http://x"><link rel=stylesheet '
                               'href="https://t.example/s.css"></body></html>')
    assert "t.example" not in body and "javascript" not in body and "onclick" not in body and "refresh" not in body
    assert 'alt="logo"' in body and 'src="cid:abc"' in body and 'href="https://ok.example"' in body


def test_a_mails_page_fetches_nothing_through_css_and_runs_nothing_through_a_split_scheme():
    styles, body = email._body_html('<html><head><style>@import url(https://t.example/a.css);'
                                    'p{background:url("https://t.example/b.gif")} q{color:red}</style></head><body>'
                                    '<p style="background-image: url( http://t.example/c.gif )">t</p>'
                                    '<a href="jav&#x09;ascript:alert(1)">x</a><a href=" &#10;javascript:alert(2)">y</a></body></html>')
    assert "t.example" not in styles + body and "javascript" not in body and "java" not in body
    assert "color:red" in styles


def test_a_web_archive_swaps_whole_references_and_its_stylesheets_pictures(tmp_path):
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["Subject"] = "page"
    msg.make_mixed()
    page = ('<html><head><link rel="stylesheet" href="http://x/s.css"></head><body><img src="http://x/a.png">'
            '<a href="http://x/a.png.bak">old</a></body></html>')
    msg.add_attachment(page, subtype="html")
    msg.get_payload()[0]["Content-Location"] = "http://x/"
    for loc, body, maintype, sub in (("http://x/a.png", b"\x89PNG one", "image", "png"),
                                     ("http://x/bg.png", b"\x89PNG two", "image", "png"),
                                     ("http://x/s.css", b"body { background: url(http://x/bg.png) }", "text", "css")):
        msg.add_attachment(body, maintype=maintype, subtype=sub)
        msg.get_payload()[-1]["Content-Location"] = loc
    src = tmp_path / "p.mht"
    src.write_bytes(msg.as_bytes())
    out = pipeline.convert(src, "html", out_dir=tmp_path / "out").read_text(encoding="utf-8")
    assert 'href="http://x/a.png.bak"' in out  # was rewritten as a.png's data with ".bak" after it
    assert 'src="data:image/png;base64,' in out
    import base64
    import re
    css = base64.b64decode(re.search(r'href="data:text/css;base64,([^"]+)"', out).group(1)).decode()
    assert "url(data:image/png;base64," in css  # the stylesheet's picture came along too


# ---- plan and convert agree

def test_plan_shows_the_route_the_conversion_takes(png, monkeypatch, without_engine):
    """When the route that keeps most of this file needs an engine that isn't installed, the conversion takes the
    usual one; --plan showed the other, with "Needs FFmpeg"."""
    from dataclasses import replace
    usual = registry.find_route("png", "jpg")
    needy = replace(usual, edges=[replace(usual.edges[0], engine="ffmpeg")])
    without_engine("ffmpeg")
    monkeypatch.setattr(pipeline, "find_route", lambda s, d, held=None, options=None: usual if held is None else needy)
    assert pipeline.plan(png, "jpg")["needs"] == []
    with pytest.raises(ConversionError, match="Quality is a whole number"):  # a bad setting is a ConversionError, not a ValueError
        pipeline.plan(png, "jpg", {"quality": "lots"})


def test_a_linear_picture_made_jpeg_doesnt_keep_its_linear_profile(tmp_path, monkeypatch):
    """A linear-light XCF (or EXR) with a profile: the JPEG got the sRGB curve but kept the file's profile (a
    linear one), so viewers showed it far too bright. A picture with the sRGB curve keeps its profile."""
    import pixport
    from PIL import Image as PILImage
    from PIL import ImageCms

    from latheport.converters import pictures
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    px = np.full((8, 8, 3), 0.2, np.float32)
    for transfer, kept in (("linear", False), ("srgb", True)):
        monkeypatch.setattr(pictures, "read", lambda src, fmt, t=transfer: pixport.Image(px, t, {"icc": icc}, "xcf"))
        out = tmp_path / f"{transfer}.jpg"
        pictures.pixport_to_picture(tmp_path / "a.xcf", out, "xcf", "jpg")
        with PILImage.open(out) as img:
            assert bool(img.info.get("icc_profile")) == kept, transfer


def test_a_picture_over_the_pixel_budget_is_refused_in_latheports_words(tmp_path):
    """pixport refuses a header claiming over 2^28 pixels, and says "pixport decodes" and "pass max_pixels=",
    which is nothing a Latheport user can do."""
    (tmp_path / "huge.hdr").write_bytes(b"#?RADIANCE\nFORMAT=32-bit_rle_rgbe\n\n-Y 20000 +X 20000\n" + bytes(64))
    with pytest.raises(ConversionError, match=r"huge\.hdr can't be read: .*20,000 x 20,000 pixels") as err:
        pipeline.convert(tmp_path / "huge.hdr", "png", tmp_path / "out")
    assert "pixport" not in str(err.value) and "max_pixels" not in str(err.value)
