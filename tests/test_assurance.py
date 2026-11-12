"""What a route can't keep, the checks on every result, routes that keep more, mislabelled files,
settings and presets: through the real pipeline and the command line."""

from __future__ import annotations

import base64
import json
import struct

import pytest
from PIL import Image

from latheport import Report, assurance, losses, options, pipeline, registry
from latheport.cli import main
from latheport.detect import detect, mislabelled
from latheport.errors import ConversionError

from .conftest import needs
from .samples import Samples, picture


@pytest.fixture
def samples(tmp_path):
    return Samples(tmp_path / "samples")


def notes(src: str, dst: str, **opts) -> list[str]:
    return [n.text for n in registry.find_route(src, dst).explain(options=opts)]


def has(lines: list[str], *words: str) -> bool:
    return any(all(w in line for w in words) for line in lines)


# ---------------------------------------------------------------- notes on every kind of step

@pytest.mark.parametrize("src,dst,words", [
    ("png", "jpg", ("transparency", "white")), ("png", "jpg", ("lossy",)), ("gif", "jpg", ("first frame",)),
    ("png", "gif", ("256 colors",)), ("png", "xbm", ("black and white",)), ("psd", "png", ("Layers",)),
    ("exr", "png", ("brightness",)), ("heic", "bmp", ("photo details",)), ("png", "ico", ("square sizes",)),
    ("png", "svg", ("traced",)), ("svg", "png", ("pixels",)), ("pdf", "png", ("150 dpi",)),
    ("pdf", "png", ("own image", "folder")), ("pdf", "png", ("selected or searched",)), ("pdf", "eps", ("first page",)),
    ("docx", "txt", ("Plain text",)), ("eml", "html", ("Attachments",)), ("glb", "stl", ("parts are merged",)),
    ("glb", "stl", ("colors and textures",)), ("abc", "stl", ("no animation",)), ("step", "stl", ("curved surfaces",)),
    ("json", "csv", ("flat table",)), ("xlsx", "csv", ("first sheet",)), ("xlsx", "json", ("Formulas",)),
    ("ass", "srt", ("styling",)), ("wav", "mp3", ("lossy",)), ("mkv", "mp4", ("re-encoded",)),
    ("mp4", "gif", ("12 frames a second", "640 pixels")), ("mp3", "m4r", ("40 seconds",)), ("kml", "gpx", ("styles",)),
    ("dcm", "png", ("8 bits",)), ("dcm", "json", ("image itself",)), ("ttf", "otf", ("hinting",)),
    ("svg", "dxf", ("outlines",)), ("html", "md", ("Markdown keeps",)), ("srt", "sub", ("frames a second",)),
])
def test_route_says_what_it_cant_keep(src, dst, words):
    assert has(notes(src, dst), *words), notes(src, dst)


@pytest.mark.parametrize("src,dst", [
    ("png", "tiff"), ("gif", "png"), ("glb", "fbx"), ("glb", "gltf"), ("fbx", "glb"), ("flac", "wav"),
    ("srt", "vtt"), ("json", "yaml"), ("csv", "xlsx"), ("ttf", "woff2"), ("zip", "7z"),
])
def test_lossless_routes_say_nothing_lost(src, dst):
    assert notes(src, dst) == []


def test_a_feature_lost_once_is_said_once():
    # FBX -> GLB -> STL: the merge is said for the step that does it, never twice
    lines = notes("fbx", "stl")
    assert sum("merged" in n for n in lines) == 1


def test_settings_change_what_is_said():
    assert has(notes("pdf", "png", dpi=300), "300 dpi")
    assert has(notes("mp4", "gif", fps=8, video_size="480p"), "8 frames a second", "480p")
    assert has(notes("png", "jpg", max_size=800), "800 pixels")


# ---------------------------------------------------------------- choosing the route that keeps more

def test_loss_outweighs_a_step(monkeypatch):
    """A direct step that loses something loses to a two-step route that doesn't."""
    edges = registry._edges.copy()
    fake = registry.defaultdict(list)
    fake.update(edges)
    noop = lambda *a: None
    fake["aaa"] = [registry.Edge("aaa", "ccc", noop, notes=lambda s, d, o: [losses.loss("drops it", weight=losses.MINOR)]),
                   registry.Edge("aaa", "bbb", noop)]
    fake["bbb"] = [registry.Edge("bbb", "ccc", noop)]
    monkeypatch.setattr(registry, "_edges", fake)
    assert str(registry.find_route("aaa", "ccc")) == "aaa -> bbb -> ccc"
    # ...but an engine still to install costs more than any one loss
    fake["bbb"] = [registry.Edge("bbb", "ccc", noop, engine="libreoffice")]
    monkeypatch.setattr(registry.engines, "available", lambda key: False)
    assert str(registry.find_route("aaa", "ccc")) == "aaa -> ccc"


def test_gltf_to_glb_goes_through_meshport():
    assert registry.find_route("glb", "gltf").edges[0].func.__name__ == "meshport_convert"
    assert registry.find_route("gltf", "glb").edges[0].func.__name__ == "meshport_convert"


def _animated_gltf(path):
    """One triangle that slides along x over a second."""
    pos = struct.pack("<9f", 0, 0, 0, 1, 0, 0, 0, 1, 0)
    times = struct.pack("<2f", 0, 1)
    moves = struct.pack("<6f", 0, 0, 0, 2, 0, 0)
    buf = pos + times + moves
    doc = {
        "asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}], "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
        "buffers": [{"byteLength": len(buf), "uri": "data:application/octet-stream;base64," + base64.b64encode(buf).decode()}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 36}, {"buffer": 0, "byteOffset": 36, "byteLength": 8},
                        {"buffer": 0, "byteOffset": 44, "byteLength": 24}],
        "accessors": [{"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3", "min": [0, 0, 0], "max": [1, 1, 0]},
                      {"bufferView": 1, "componentType": 5126, "count": 2, "type": "SCALAR", "min": [0], "max": [1]},
                      {"bufferView": 2, "componentType": 5126, "count": 2, "type": "VEC3"}],
        "animations": [{"samplers": [{"input": 1, "output": 2}], "channels": [{"sampler": 0, "target": {"node": 0, "path": "translation"}}]}],
    }
    path.write_text(json.dumps(doc))
    return path


def test_animation_survives_gltf_to_glb_and_is_reported_lost_in_stl(tmp_path):
    src = _animated_gltf(tmp_path / "slide.gltf")
    r = Report()
    out = pipeline.convert(src, "glb", tmp_path / "out", report=r)
    assert assurance.inspect(out, "glb")["animation"] is True
    assert {c.name: c.state for c in r.checks}["Parts"] == "ok" and not r.warnings
    r = Report()
    pipeline.convert(src, "stl", tmp_path / "out", report=r)
    assert has(r.notes, "no animation")
    assert {c.name: c.state for c in r.checks}.get("Animation", "expected") == "expected"


# ---------------------------------------------------------------- measuring and checking

def test_inspect_measures_each_kind(tmp_path, samples):
    img = tmp_path / "a.png"
    picture().save(img)
    assert assurance.inspect(img, "png") == {"width": 160, "height": 120, "frames": 1, "photo_info": False,
                                              "transparency": True}
    import pymupdf
    doc = pymupdf.open()
    for _ in range(3):
        doc.new_page()
    doc.save(tmp_path / "three.pdf")
    assert assurance.inspect(tmp_path / "three.pdf", "pdf") == {"pages": 3}
    (tmp_path / "t.json").write_text(json.dumps({"a": [{"x": 1, "y": {"z": 2}}], "b": [{"x": 2}, {"x": 3}]}))
    assert assurance.inspect(tmp_path / "t.json", "json") == {"tables": 2, "records": 1, "nested": True}
    assert assurance.inspect(samples("srt"), "srt") == {"cues": 2}
    assert assurance.inspect(samples("ttf"), "ttf")["glyphs"] > 1
    assert assurance.inspect(samples("zip"), "zip")["files"] >= 1
    assert assurance.inspect(samples("gpx"), "gpx")["places"] >= 1
    assert assurance.inspect(tmp_path / "t.json", "xyz") is None  # not a kind measured here


def test_inspect_refuses_a_broken_result(tmp_path):
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"\x89PNG\r\n\x1a\n not really")
    with pytest.raises(assurance.Unreadable):
        assurance.inspect(bad, "png")
    assert assurance.measure(bad, "png") is None  # a source that won't open is the converter's business


def test_verify_states():
    lost = [losses.loss("STL holds one mesh", "parts", affects=("parts",))]
    checks = {c.name: c for c in assurance.verify({"parts": 4, "triangles": 10, "size": 2.0},
                                                   {"parts": 1, "triangles": 9, "size": 2.0005}, lost)}
    assert checks["Parts"].state == "expected" and checks["Parts"].why == "STL holds one mesh"
    assert checks["Triangles"].state == "warning"
    assert checks["Size"].state == "ok"  # within 1/1000
    assert assurance.verify({"duration": 10.0}, {"duration": 10.3}, [])[0].state == "ok"
    assert assurance.verify({"duration": 10.0}, {"duration": 7.0}, [])[0].state == "warning"


def test_a_result_that_doesnt_open_is_kept_and_reported(tmp_path, monkeypatch):
    from latheport.converters import images
    monkeypatch.setitem(images.OWN_WRITERS, "psd", lambda img, dst: dst.write_bytes(b"8BPS garbage"))
    src = tmp_path / "a.png"
    picture().save(src)
    r = Report()
    out = pipeline.convert(src, "psd", tmp_path / "out", report=r)
    assert out.exists()  # kept, never deleted
    assert r.checks[0].state == "error" and "doesn't open as PSD" in r.warnings[0]
    assert r.summary().startswith("Check:")


def test_report_lines_and_json(tmp_path):
    src = tmp_path / "a.png"
    picture().save(src)
    r = Report()
    pipeline.convert(src, "jpg", tmp_path / "out", report=r)
    d = json.loads(json.dumps(r.to_dict(), default=str))
    assert d["route"] == "PNG → JPG" and d["steps"][0]["tool"].startswith("Pillow ")
    assert {c["name"]: c["state"] for c in d["checks"]} == {"Frames": "ok", "Width": "ok", "Height": "ok",
                                                             "Transparency": "expected"}
    assert r.lines()[0].startswith("PNG → JPG via Pillow")
    assert "Checked, the same: frames 1, width 160, height 120" in r.lines()


def test_unhappened_may_notes_are_dropped(tmp_path):
    src = tmp_path / "small.png"
    picture().save(src)
    r = Report()
    pipeline.convert(src, "webp", tmp_path / "out", report=r, options={"max_size": 1000})
    assert not has(r.notes, "scaled down")  # 160 px wide: nothing was scaled


# ---------------------------------------------------------------- files named for the wrong format

def test_a_jpeg_named_png_is_converted_as_a_jpeg(tmp_path):
    fake = tmp_path / "photo.png"
    picture().convert("RGB").save(fake, "JPEG")
    assert detect(fake) == "jpg"
    assert "really a JPG" in mislabelled(fake)
    r = Report()
    out = pipeline.convert(fake, "webp", tmp_path / "out", report=r)
    assert r.detected == "content" and has(r.notes, "really a JPG")
    with Image.open(out) as img:
        assert img.format == "WEBP"
    with pytest.raises(ConversionError, match="a JPG already"):
        pipeline.convert(fake, "jpg", tmp_path / "out")


def test_honest_names_and_unknown_content_stay(tmp_path):
    real = tmp_path / "a.png"
    picture().save(real)
    odd = tmp_path / "b.png"
    odd.write_bytes(b"nothing we know")
    assert detect(real) == "png" and mislabelled(real) is None
    assert detect(odd) == "png" and mislabelled(odd) is None  # no other format's signature: the name stands


# ---------------------------------------------------------------- what Pillow used to drop or get wrong

def test_photo_details_and_color_profile_come_along(tmp_path):
    from PIL import ImageCms
    src = tmp_path / "photo.jpg"
    exif = Image.Exif()
    exif[0x010F], exif[0x0110], exif[0x0132], exif[0x0112] = "Camera Co", "Model 1", "2026:10:29 08:00:00", 6
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    picture().convert("RGB").save(src, "JPEG", exif=exif.tobytes(), icc_profile=icc)
    for fmt in ("png", "webp", "tiff", "heic", "avif"):
        out = pipeline.convert(src, fmt, tmp_path / fmt)
        if fmt == "heic":
            from latheport.converters.images import enable_heif
            enable_heif()
        with Image.open(out) as img:
            got = img.getexif()
            assert got.get(0x010F) == "Camera Co" and got.get(0x0132) == "2026:10:29 08:00:00", fmt
            assert got.get(0x0112, 1) == 1, fmt  # turned upright, so no longer "rotate 90"
            assert img.size == (120, 160), fmt
            assert img.info.get("icc_profile") == icc, fmt


def _layered_psd(path, size=(8, 6)):
    """A Photoshop file with two layers (red, blue) and a green flattened picture."""
    w, h = size

    def channels(rgb):
        return [bytes([c]) * (w * h) for c in rgb]
    layers = [("red", (255, 0, 0)), ("blue", (0, 0, 255))]
    records, data = b"", b""
    for name, rgb in layers:
        raw = name.encode()
        pname = bytes([len(raw)]) + raw
        pname += b"\0" * (-len(pname) % 4)
        extra = struct.pack(">II", 0, 0) + pname
        records += struct.pack(">iiiiH", 0, 0, h, w, 3)
        records += b"".join(struct.pack(">hI", i, 2 + w * h) for i in range(3))
        records += b"8BIMnorm" + bytes([255, 0, 0, 0]) + struct.pack(">I", len(extra)) + extra
        data += b"".join(struct.pack(">H", 0) + c for c in channels(rgb))
    info = struct.pack(">h", len(layers)) + records + data
    info += b"\0" * (len(info) % 2)
    section = struct.pack(">I", len(info)) + info + struct.pack(">I", 0)
    head = b"8BPS" + struct.pack(">H6xHIIHH", 1, 3, h, w, 8, 3) + struct.pack(">II", 0, 0)
    path.write_bytes(head + struct.pack(">I", len(section)) + section + struct.pack(">H", 0)
                     + b"".join(channels((0, 255, 0))))
    return path


def test_a_layered_photoshop_file_is_its_flattened_picture(tmp_path):
    src = _layered_psd(tmp_path / "layers.psd")
    with Image.open(src) as img:
        assert img.n_frames == 2  # Pillow sees the layers as frames
    for fmt in ("png", "gif", "webp"):
        out = pipeline.convert(src, fmt, tmp_path / fmt)
        with Image.open(out) as img:
            assert getattr(img, "n_frames", 1) == 1, fmt
            assert img.convert("RGB").getpixel((3, 3))[1] > 200, fmt  # green: the whole picture, not a layer


def test_several_sheets_to_csv_keep_the_first(tmp_path):
    from openpyxl import Workbook
    wb = Workbook()
    wb.active.title = "People"
    wb.active.append(["name", "age"])
    wb.active.append(["Ada", 36])
    other = wb.create_sheet("Places")
    other.append(["city"])
    other.append(["London"])
    src = tmp_path / "book.xlsx"
    wb.save(src)
    r = Report()
    out = pipeline.convert(src, "csv", tmp_path / "out", report=r)
    assert out.read_text(encoding="utf-8-sig").splitlines() == ["name,age", "Ada,36"]
    assert has(r.notes, "first sheet")
    assert {c.name: c.state for c in r.checks} == {"Tables": "expected", "Records": "ok"}


# ---------------------------------------------------------------- settings

def _size(path):
    return path.stat().st_size


def test_quality_and_max_size(tmp_path):
    src = tmp_path / "big.png"
    Image.effect_mandelbrot((1200, 900), (-2, -1.2, 1, 1.2), 80).convert("RGB").save(src)
    default = pipeline.convert(src, "jpg", tmp_path / "a")
    low = pipeline.convert(src, "jpg", tmp_path / "b", options={"quality": 40})
    assert _size(low) < 0.7 * _size(default)
    small = pipeline.convert(src, "webp", tmp_path / "c", options={"max_size": 600})
    with Image.open(small) as img:
        assert img.size == (600, 450)


# ---------------------------------------------------------------- deep fry

def _scene(path, size=(640, 480), mode="RGB"):
    """A soft picture: sky, grass, a face, the way memes start out."""
    from PIL import ImageDraw
    img = Image.new(mode, size, (110, 160, 230, 255) if mode == "RGBA" else (110, 160, 230))
    d = ImageDraw.Draw(img)
    w, h = size
    d.rectangle([0, h * 2 // 3, w, h], fill=(60, 150, 60))
    d.ellipse([w // 3, h // 5, w * 2 // 3, h * 3 // 4], fill=(240, 200, 160))
    if mode == "RGBA":
        d.rectangle([0, 0, w // 8, h // 8], fill=(0, 0, 0, 0))  # a clear corner
    img.save(path)
    return path


def _saturation(img):
    import numpy as np
    return float(np.asarray(img.convert("HSV"))[:, :, 1].mean())


def test_deep_fry_gets_hotter_up_the_slider(tmp_path):
    import numpy as np
    src = _scene(tmp_path / "meme.jpg")
    with Image.open(src) as img:
        plain = np.asarray(img.convert("RGB"), dtype=np.float32)
        before = _saturation(img)
    seen = []
    for heat in (1, 50, 100):
        r = Report()
        out = pipeline.convert(src, "jpg", tmp_path / str(heat), options={"deepfry": heat}, report=r)
        with Image.open(out) as img:
            assert img.size == (640, 480)
            seen.append((_saturation(img), float(np.abs(np.asarray(img.convert("RGB"), dtype=np.float32) - plain).mean())))
        assert has(r.notes, "Deep fried, on purpose")
        assert all(c.state == "ok" for c in r.checks), r.checks  # same size and frames, no warnings
    assert before < seen[0][0] < seen[1][0]  # more colour; at the top, blocks and grain mix it a little
    assert seen[2][0] > 1.5 * before
    assert seen[0][1] < seen[1][1] < seen[2][1]           # and further from the original
    assert seen[0][1] > 5 and seen[0][0] > 1.2 * before  # even 1% is plainly fried


def test_deep_fry_is_the_same_every_time_and_off_unless_asked(tmp_path):
    src = _scene(tmp_path / "meme.png")
    a = pipeline.convert(src, "png", tmp_path / "a", options={"deepfry": 50})
    b = pipeline.convert(src, "png", tmp_path / "b", options={"deepfry": 50})
    assert a.read_bytes() == b.read_bytes()  # the grain comes from a fixed seed
    plain = pipeline.convert(src, "png", tmp_path / "c")
    with Image.open(src) as x, Image.open(plain) as y:
        assert x.tobytes() == y.convert(x.mode).tobytes()  # a PNG to PNG with nothing set changes no pixel
    assert options.lasting({"deepfry": 100, "quality": 80}) == {"quality": 80}  # never carried to the next files
    st = options.SETTINGS["deepfry"]
    assert st.slider and st.check("75%") == 75 and st.check(" 1 ") == 1
    for bad in ("0", "101", "extra crispy"):
        with pytest.raises(ValueError, match="Deep fry is"):
            st.check(bad)


def test_deep_fry_keeps_transparency_and_animation(tmp_path):
    src = _scene(tmp_path / "logo.png", mode="RGBA")
    out = pipeline.convert(src, "webp", tmp_path / "a", options={"deepfry": 100})
    with Image.open(out) as img:
        assert img.mode == "RGBA" and img.getpixel((2, 2))[3] == 0 and img.getpixel((300, 400))[3] == 255
    frames = [Image.new("RGB", (120, 90), c) for c in ((200, 120, 90), (90, 120, 200), (120, 200, 90))]
    gif = tmp_path / "clip.gif"
    frames[0].save(gif, save_all=True, append_images=frames[1:], duration=80, loop=0)
    r = Report()
    out = pipeline.convert(gif, "gif", tmp_path / "b", options={"deepfry": 1}, report=r)
    with Image.open(out) as img:
        assert img.n_frames == 3
        first = img.convert("RGB").getpixel((60, 45))
    assert first != (200, 120, 90)
    assert {c.name: c.state for c in r.checks}["Frames"] == "ok"


def test_deep_fry_is_offered_where_pictures_are_saved():
    for src, dst in (("jpg", "jpg"), ("png", "png"), ("png", "jpg"), ("heic", "png"), ("gif", "gif"), ("bmp", "webp")):
        assert "deepfry" in registry.find_route(src, dst).settings, (src, dst)
    for src, dst in (("png", "ico"), ("png", "xbm"), ("mp4", "mp4"), ("png", "svg")):
        assert "deepfry" not in registry.find_route(src, dst).settings, (src, dst)
    assert has(notes("jpg", "jpg"), "Saving a JPG again")
    assert not has(notes("png", "png"), "again")  # PNG keeps every pixel


def test_deep_fry_from_the_command_line(tmp_path, capsys):
    src = _scene(tmp_path / "meme.jpg")
    assert main([str(src), "--to", "jpg", "--deepfry", "100", "-o", str(tmp_path / "out")]) == 0
    assert (tmp_path / "out" / "meme.jpg").exists()
    before = src.read_bytes()
    assert main([str(src), "--to", "jpg", "--deepfry", "30%"]) == 0  # next to it: under a new name
    assert src.read_bytes() == before and (tmp_path / "meme (1).jpg").stat().st_size > 0
    assert main([str(src), "--to", "jpg", "--deepfry", "200"]) == 2
    assert "Deep fry is from 1 to 100" in capsys.readouterr().err


# ---------------------------------------------------------------- effects: turn, crop, touch up, colour

def _corners(path, size=(640, 480)):
    """A PNG with a different colour in each corner, to see where each one goes."""
    img = Image.new("RGB", size, (128, 128, 128))
    w, h = size
    for (x, y), c in (((0, 0), (255, 0, 0)), ((w - 8, 0), (0, 255, 0)), ((0, h - 8), (0, 0, 255)),
                      ((w - 8, h - 8), (255, 255, 0))):
        img.paste(c, (x, y, x + 8, y + 8))
    img.save(path)
    return path


@pytest.mark.parametrize("turn, size, top_left", [
    ("right", (480, 640), (0, 0, 255)),     # a quarter clockwise: the bottom-left corner comes up top
    ("left", (480, 640), (0, 255, 0)),
    ("180", (640, 480), (255, 255, 0)),
    ("mirror", (640, 480), (0, 255, 0)),
])
def test_turn(tmp_path, turn, size, top_left):
    r = Report()
    out = pipeline.convert(_corners(tmp_path / "a.png"), "png", tmp_path / "out", options={"rotate": turn}, report=r)
    with Image.open(out) as img:
        assert img.size == size and img.convert("RGB").getpixel((2, 2)) == top_left
    assert all(c.state in ("ok", "expected") for c in r.checks), r.checks
    assert has(r.notes, "Turned" if turn != "mirror" else "Mirrored")


@pytest.mark.parametrize("shape, size, top_left", [
    ("square", (480, 480), (80, 0)), ("4:5", (384, 480), (128, 0)),
    ("16:9", (640, 360), (0, 60)), ("9:16", (270, 480), (185, 0)),
])
def test_crop_to_a_shape_from_the_middle(tmp_path, shape, size, top_left):
    src = _scene(tmp_path / "a.png")
    r = Report()
    out = pipeline.convert(src, "png", tmp_path / "out", options={"crop": shape}, report=r)
    with Image.open(src) as a, Image.open(out) as b:
        assert b.size == size
        assert b.convert("RGB").tobytes() == a.convert("RGB").crop((*top_left, top_left[0] + size[0],
                                                                    top_left[1] + size[1])).tobytes()
    checks = {c.name: c for c in r.checks}
    assert checks["Width"].state == "expected" and "Cropped to" in checks["Width"].why
    assert not r.warnings


def test_turn_crop_and_max_size_together_and_on_every_frame(tmp_path):
    out = pipeline.convert(_corners(tmp_path / "a.png"), "jpg", tmp_path / "a",
                           options={"rotate": "right", "crop": "16:9", "max_size": 240})
    with Image.open(out) as img:
        assert img.size == (240, 135)  # turned to 480 x 640, cropped to 480 x 270, then fitted in 240
    frames = [Image.new("RGB", (120, 90), c) for c in ((200, 120, 90), (90, 120, 200), (120, 200, 90))]
    gif = tmp_path / "clip.gif"
    frames[0].save(gif, save_all=True, append_images=frames[1:], duration=80, loop=0)
    r = Report()
    out = pipeline.convert(gif, "gif", tmp_path / "b", options={"rotate": "left", "crop": "square"}, report=r)
    with Image.open(out) as img:
        assert img.n_frames == 3 and img.size == (90, 90)
    states = {c.name: c.state for c in r.checks}
    assert states["Frames"] == "ok" and states["Width"] == states["Height"] == "expected" and not r.warnings


def _dull(path, src):
    """src washed out and tinted blue, as an old phone or a bad white balance leaves it."""
    import numpy as np
    with Image.open(src) as img:
        px = np.asarray(img.convert("RGB"), dtype=np.float32)
    Image.fromarray(np.clip(70 + px * 0.55 + [-10, 0, 25], 0, 255).astype(np.uint8)).save(path)
    return path


def test_auto_fix_brings_back_a_dull_photo_and_leaves_a_good_one(tmp_path):
    import numpy as np
    from PIL import ImageDraw
    good = _scene(tmp_path / "good.png")
    with Image.open(good) as img:  # a photo has some shadow and some light somewhere
        img = img.convert("RGB")
        d = ImageDraw.Draw(img)
        d.ellipse([270, 170, 300, 200], fill=(25, 25, 25))
        d.rectangle([180, 400, 460, 440], fill=(250, 250, 250), outline=(10, 10, 10), width=4)
        img.save(good)
    dull = _dull(tmp_path / "dull.png", good)
    with Image.open(good) as g, Image.open(dull) as d:
        clean = np.asarray(g.convert("RGB"), dtype=np.float32)
        off_before = np.abs(np.asarray(d.convert("RGB"), dtype=np.float32) - clean).mean()
    r = Report()
    fixed = pipeline.convert(dull, "png", tmp_path / "a", options={"enhance": "auto"}, report=r)
    with Image.open(fixed) as f:
        px = np.asarray(f.convert("RGB"), dtype=np.float32)
    lo, hi = np.percentile(px, (0.5, 99.5))
    assert lo < 20 and hi > 235                                   # the full range again
    assert np.abs(px - clean).mean() < off_before / 2             # much closer to how it should look
    assert has(r.notes, "Touched up") and not r.warnings
    again = pipeline.convert(good, "png", tmp_path / "b", options={"enhance": "auto"})
    with Image.open(again) as f:
        assert np.abs(np.asarray(f.convert("RGB"), dtype=np.float32) - clean).mean() < 5  # a good one barely changes (3.6 of 255)


def test_clean_up_a_page(tmp_path):
    import numpy as np
    from PIL import ImageDraw, ImageFilter
    w, h = 800, 600
    page = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(page)
    for i in range(12):
        d.rectangle([60, 60 + i * 40, 60 + 400 + (i * 37) % 250, 72 + i * 40], fill=20)  # lines of writing
    paper = np.asarray(page) > 128
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    light = 0.92 - 0.38 * np.clip(x / w + y / h - 0.8, 0, 1)      # a shadow over one corner
    px = np.asarray(page, dtype=np.float32) * light
    photo = tmp_path / "page.jpg"
    Image.fromarray(np.stack([px * 0.97, px * 0.95, px * 0.86], axis=2).astype(np.uint8)).save(photo, quality=90)
    with Image.open(photo) as img:
        before = np.asarray(img.convert("L"), dtype=np.float32)
    assert before[paper].std() > 20                                 # the shadow: the paper is far from even
    r = Report()
    out = pipeline.convert(photo, "png", tmp_path / "a", options={"enhance": "page"}, report=r)
    with Image.open(out) as img:
        assert img.mode == "L"
        after = np.asarray(img, dtype=np.float32)
    inner = np.asarray(Image.fromarray(paper).filter(ImageFilter.MinFilter(7)))  # paper away from the writing
    assert after[inner].mean() > 250 and after[inner].std() < 3     # evenly white
    assert after[~paper].mean() < 60                                 # and the writing dark
    assert has(r.notes, "Cleaned up as a page") and not r.warnings


def test_black_and_white_and_sepia(tmp_path):
    import numpy as np
    src = _scene(tmp_path / "logo.png", mode="RGBA")
    bw = pipeline.convert(src, "jpg", tmp_path / "a", options={"colour": "bw"})
    with Image.open(bw) as img:
        assert img.mode == "L"                            # a real grey JPG: one channel, smaller
    clear = pipeline.convert(src, "png", tmp_path / "b", options={"colour": "bw"})
    with Image.open(clear) as img:
        r, g, b, a = img.convert("RGBA").getpixel((300, 400))
        assert r == g == b and a == 255 and img.convert("RGBA").getpixel((2, 2))[3] == 0  # transparency kept
    sepia = pipeline.convert(src, "jpg", tmp_path / "c", options={"colour": "sepia"})
    with Image.open(sepia) as img:
        mean = np.asarray(img.convert("RGB"), dtype=np.float32).reshape(-1, 3).mean(axis=0)
    assert mean[0] > mean[1] > mean[2] + 20               # warm brown
    assert has(notes("png", "jpg", colour="sepia"), "sepia")


def test_effects_are_offered_where_they_make_sense_and_kept_for_these_files_only():
    keys = ("rotate", "crop", "enhance", "colour")
    for src, dst in (("jpg", "jpg"), ("png", "webp"), ("heic", "jpg"), ("gif", "gif"), ("png", "pdf")):
        assert set(keys) <= set(registry.find_route(src, dst).settings), (src, dst)
    assert {"rotate", "crop"} <= set(registry.find_route("png", "ico").settings)
    xbm = set(registry.find_route("png", "xbm").settings)
    assert {"rotate", "crop"} <= xbm and not {"enhance", "colour"} & xbm  # 1 bit: no colour to change
    for src, dst in (("png", "svg"), ("docx", "pdf")):
        assert not set(keys) & set(registry.find_route(src, dst).settings), (src, dst)
    video = set(registry.find_route("mp4", "mp4").settings)
    assert {"rotate", "crop", "colour"} <= video and "enhance" not in video  # turned and recoloured, not touched up
    assert options.lasting({k: options.SETTINGS[k].choices[0] for k in keys} | {"quality": 80}) == {"quality": 80}
    with pytest.raises(ValueError, match="Turn is one of"):
        options.SETTINGS["rotate"].check("sideways")
    assert options.SETTINGS["crop"].check(" 16:9 ") == "16:9"


def test_no_picture_goes_through_pillow_twice():
    """Every step of a route gets the same settings: a picture saved twice would be turned twice."""
    from latheport.converters import images
    once = 0
    for src in sorted(images.READ):
        for dst, route in registry.all_routes(src).items():
            through = sum(e.func is images.pillow_convert for e in route.edges)
            assert through <= 1, (src, dst, str(route))
            once += through
    assert once > 300, once  # it looked at the routes that do save a picture


def test_effects_from_the_command_line(tmp_path, capsys):
    src = _corners(tmp_path / "photo.png")
    assert main([str(src), "--to", "jpg", "--rotate", "right", "--crop", "square", "--colour", "bw",
                 "--enhance", "auto", "-o", str(tmp_path / "out")]) == 0
    with Image.open(tmp_path / "out" / "photo.jpg") as img:
        assert img.size == (480, 480) and img.mode == "L"
    assert main([str(src), "--to", "jpg", "--crop", "round"]) == 2
    assert "Crop to is one of: square, 4:5, 16:9, 9:16" in capsys.readouterr().err


# ---------------------------------------------------------------- crop by hand, text, watermark

def test_crop_by_hand_keeps_exactly_the_box_and_wins_over_a_shape(tmp_path):
    src = _scene(tmp_path / "a.png")  # 640 x 480
    r = Report()
    out = pipeline.convert(src, "png", tmp_path / "a", options={"area": "25,10,50,80", "crop": "square"}, report=r)
    with Image.open(src) as a, Image.open(out) as b:
        assert b.size == (320, 384)
        assert b.convert("RGB").tobytes() == a.convert("RGB").crop((160, 48, 480, 432)).tobytes()
    checks = {c.name: c for c in r.checks}
    assert checks["Width"].state == checks["Height"].state == "expected" and "Cropped by hand" in checks["Width"].why
    assert has(r.notes, "Cropped by hand") and not has(r.notes, "Cropped to a square")
    turned = pipeline.convert(src, "png", tmp_path / "b", options={"rotate": "right", "area": "0,0,50,100"})
    with Image.open(turned) as img:
        assert img.size == (240, 640)  # the box is of the picture as turned (480 x 640)
    st = options.SETTINGS["area"]
    assert st.check("10, 5, 80%, 60") == "10,5,80,60" and options.area_box("10,5,80,60", 640, 480) == (64, 24, 576, 312)
    for bad in ("90,0,20,10", "1,2,3", "a,b,c,d", "0,0,0,50"):
        with pytest.raises(ValueError, match="Crop by hand"):
            st.check(bad)


def _bright(img, box) -> float:
    import numpy as np
    return float(np.asarray(img.convert("L").crop(box), dtype=np.float32).mean())


def test_text_goes_where_it_is_placed_at_the_size_asked(tmp_path):
    src = tmp_path / "black.png"
    Image.new("RGB", (800, 600), "black").save(src)
    box = {"text": "HELLO", "x": 0.25, "y": 0.25, "size": 0.1, "font": "bold", "colour": "#ffffff", "edge": "none"}
    r = Report()
    out = pipeline.convert(src, "png", tmp_path / "a", options={"texts": [box]}, report=r)
    with Image.open(out) as img:
        assert _bright(img, (150, 120, 250, 180)) > 60       # white letters around (200, 150)
        assert _bright(img, (500, 380, 700, 520)) == 0         # nothing elsewhere
    assert has(r.notes, "Text added") and not r.warnings
    from latheport import overlay
    small, big = overlay.text_bounds((400, 300), [options.check_box(box)]), overlay.text_bounds((800, 600), [options.check_box(box)])
    assert abs((big[0][3] - big[0][1]) / (small[0][3] - small[0][1]) - 2) < 0.15  # letters scale with the picture
    stored = options.clean({"texts": [box]})["texts"]
    assert isinstance(stored, str) and options.boxes(stored)[0]["text"] == "HELLO"  # kept as text: routes cache on it
    assert options.clean({"texts": []}) == {}


def test_meme_text_is_upper_case_outlined_and_long_captions_stay_on_the_picture(tmp_path):
    from latheport import overlay
    src = tmp_path / "grey.png"
    Image.new("RGB", (800, 600), (120, 120, 120)).save(src)
    boxes = options.check_boxes([options.text_box("when you finally fix the bug and it was a missing comma all along",
                                                  "top", meme=True)])
    out = pipeline.convert(src, "png", tmp_path / "a", options={"texts": boxes})
    (x0, y0, x1, y1), = overlay.text_bounds((800, 600), options.boxes(boxes))
    assert y0 >= 0.02 * 600 - 1 and x0 >= 0 and x1 <= 800  # three lines, moved down to stay inside
    assert y1 - y0 > 2.5 * 0.11 * 600 * 0.8
    import numpy as np
    with Image.open(out) as img:
        px = np.asarray(img.convert("L").crop((int(x0), int(y0), int(x1), int(y1))))
    assert (px > 240).mean() > 0.1 and (px < 15).mean() > 0.05  # white letters, black outline
    assert overlay.text_bounds((800, 600), [options.text_box("abc", "top", meme=True)]) != \
        overlay.text_bounds((800, 600), [options.text_box("abc", "top")])


@pytest.mark.parametrize("corner, inside, outside", [
    ("bottom-right", (560, 540, 800, 600), (0, 0, 240, 60)),
    ("top-left", (0, 0, 240, 60), (560, 540, 800, 600)),
])
def test_watermark_in_its_corner_and_never_unless_typed(tmp_path, corner, inside, outside):
    src = tmp_path / "dark.png"
    Image.new("RGB", (800, 600), (30, 30, 30)).save(src)
    r = Report()
    out = pipeline.convert(src, "png", tmp_path / "a", options={"watermark": "my-photos.example", "watermark_at": corner},
                           report=r)
    with Image.open(out) as img:
        assert _bright(img, inside) > 35 and _bright(img, outside) == 30
    assert has(r.notes, "Watermarked: my-photos.example")
    plain = pipeline.convert(src, "png", tmp_path / "b")
    with Image.open(plain) as img:
        assert _bright(img, inside) == 30  # no watermark unless one is typed: none is built in
    assert "watermark" not in options.clean({}) and options.SETTINGS["watermark"].auto == "None"
    white = tmp_path / "white.png"
    Image.new("RGB", (800, 600), (250, 250, 250)).save(white)
    out = pipeline.convert(white, "png", tmp_path / "c", options={"watermark": "my-photos.example", "watermark_at": corner})
    with Image.open(out) as img:
        assert _bright(img, inside) < 248  # dark on a bright corner, so it shows


def test_watermark_and_pitch_are_remembered_the_rest_is_for_these_files():
    kept = options.lasting({"area": "1,1,50,50", "texts": "[]", "watermark": "me", "watermark_at": "top-left",
                            "speed": "2", "pitch": "change", "reverse": "yes"})
    assert kept == {"watermark": "me", "watermark_at": "top-left", "pitch": "change"}
    for bad in ("x" * 201, "a\x07b"):
        with pytest.raises(ValueError, match="Watermark"):
            options.SETTINGS["watermark"].check(bad)
    assert options.SETTINGS["speed"].check("2x") == "2"


# ---- the editor's deeper tools: Adjust, Straighten, Hide, Draw, a watermark picture

def _arr(path):
    import numpy as np
    with Image.open(path) as img:
        return np.asarray(img.convert("RGB"), dtype=np.float32)


def _convert(src, out, r=None, **opts):
    return pipeline.convert(src, "png", out, options=options.clean(opts), report=r)


def test_adjust_moves_each_measure_its_own_way(tmp_path):
    import numpy as np
    src = _scene(tmp_path / "scene.png")
    plain = _arr(_convert(src, tmp_path / "plain"))
    grey = lambda a: a @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    colourful = lambda a: (a.max(axis=2) - a.min(axis=2)).mean()
    edges = lambda a: (np.diff(grey(a), axis=1) ** 2).mean()
    measures = {"brightness": lambda a: grey(a).mean(), "contrast": lambda a: grey(a).std(), "saturation": colourful,
                "warmth": lambda a: (a[..., 0] - a[..., 2]).mean(), "sharpness": edges}
    for name, measure in measures.items():
        r = Report()
        up = _arr(_convert(src, tmp_path / f"{name}-up", r, adjust=f"{name}=60"))
        down = _arr(_convert(src, tmp_path / f"{name}-down", adjust=f"{name}=-60"))
        assert measure(down) < measure(plain) < measure(up), name
        assert has(r.notes, f"Adjusted: {name} +60")
    grey_out = _arr(_convert(src, tmp_path / "grey", adjust="saturation=-100"))
    assert colourful(grey_out) < 1  # all the way down: grey


def test_adjust_and_straighten_take_only_what_they_can(tmp_path):
    adjust, angle = options.SETTINGS["adjust"], options.SETTINGS["straighten"]
    assert adjust.check("Contrast=5, brightness=10,warmth=0") == "brightness=10,contrast=5"  # one form, zeros out
    assert adjust.check({"sharpness": 3}) == "sharpness=3" and options.clean({"adjust": "brightness=0"}) == {}
    assert options.adjustments("warmth=-20") == {"brightness": 0, "contrast": 0, "saturation": 0, "warmth": -20, "sharpness": 0}
    for bad, words in (("glow=5", "Adjust takes"), ("brightness=101", "from -100 to 100"), ("brightness", "like brightness=10")):
        with pytest.raises(ValueError, match=words):
            adjust.check(bad)
    assert angle.check("2.54") == "2.5" and angle.check("-1°") == "-1" and options.clean({"straighten": 0}) == {}
    with pytest.raises(ValueError, match="from -15 to 15"):
        angle.check(16)
    assert not options.lasting({"adjust": "brightness=5", "straighten": "1", "hides": "[]", "marks": "[]"})


def test_straighten_turns_it_level_keeping_its_size_with_no_empty_corner(tmp_path):
    import numpy as np
    src = tmp_path / "line.png"
    img = Image.new("RGB", (400, 300), "white")
    img.paste((0, 0, 0), (196, 0, 204, 300))  # a straight up-and-down line through the middle
    img.save(src)
    r = Report()
    a = _arr(_convert(src, tmp_path / "a", r, straighten="8"))
    assert a.shape[:2] == (300, 400)
    assert min(a[0, 0].min(), a[0, -1].min(), a[-1, 0].min(), a[-1, -1].min()) > 245  # every corner still white
    dark = lambda row: np.nonzero(a[row].mean(axis=1) < 100)[0].mean()
    lean = dark(20) - dark(279)  # turned clockwise: its top goes right
    assert 20 < lean < 60, lean
    assert has(r.notes, "Straightened 8°")
    back = _arr(_convert(src, tmp_path / "b", straighten="-8"))
    assert np.nonzero(back[20].mean(axis=1) < 100)[0].mean() < 200  # the other way: to the left


@pytest.mark.parametrize("how", ["pixelate", "blur", "cover"])
def test_hide_changes_its_part_and_nothing_else(tmp_path, how):
    import numpy as np
    src = tmp_path / "detail.png"  # fine detail everywhere, as in a face or a number plate
    Image.fromarray(np.random.default_rng(5).integers(0, 256, (480, 640, 3), dtype=np.uint8)).save(src)
    plain = _arr(_convert(src, tmp_path / "plain"))
    r = Report()
    a = _arr(_convert(src, tmp_path / "a", r, hides=[{"x": 0.25, "y": 0.25, "w": 0.5, "h": 0.25, "how": how}]))
    x0, y0, x1, y1 = 160, 120, 480, 240
    outside = np.ones(plain.shape[:2], bool)
    outside[y0:y1, x0:x1] = False
    assert np.array_equal(a[outside], plain[outside])  # not a pixel past the part changes
    part = a[y0:y1, x0:x1]
    assert np.abs(part - plain[y0:y1, x0:x1]).mean() > 5
    if how == "cover":
        assert part.max() == 0
    elif how == "pixelate":  # blocks: 12 down its shorter side at the usual strength, 32 across this one
        assert 300 < len(np.unique(part.reshape(-1, 3), axis=0)) <= 32 * 12
    words = {"pixelate": "pixelated", "blur": "blurred", "cover": "covered"}[how]
    assert has(r.notes, f"1 part hidden ({words})")


def test_hide_is_stronger_up_its_slider_and_said_plainly_when_wrong(tmp_path):
    import numpy as np
    src = _scene(tmp_path / "scene.png")
    blocks = []
    for strength in (10, 90):
        a = _arr(_convert(src, tmp_path / str(strength), hides=[{"x": 0.3, "y": 0.2, "w": 0.4, "h": 0.5, "strength": strength}]))
        blocks.append(len(np.unique(a[96:336, 192:448].reshape(-1, 3), axis=0)))
    assert blocks[0] > blocks[1]  # stronger: fewer, bigger blocks
    for bad, words in (({"x": 0.9, "y": 0, "w": 0.2, "h": 0.1}, "inside the picture"), ({"x": 0, "y": 0, "w": 0.1, "h": 0.1, "how": "smudge"}, "one of"),
                       ({"x": 0, "y": 0, "w": 0.1, "h": 0.1, "strength": 0}, "from 1 to 100")):
        with pytest.raises(ValueError, match=words):
            options.check_hides([bad])
    assert options.parse_hide("blur/40:10,10,20,20") == {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2, "how": "blur", "strength": 40}
    assert options.parse_hide("5,5,10,10")["how"] == "pixelate"
    with pytest.raises(ValueError, match="--hide is how:left,top,width,height"):
        options.parse_hide("pixelate:10,10")


def test_drawings_stay_on_what_they_point_at_when_the_crop_changes(tmp_path):
    src = tmp_path / "grey.png"
    Image.new("RGB", (800, 400), (128, 128, 128)).save(src)
    dot = [{"kind": "ellipse", "points": [[0.7, 0.4], [0.8, 0.6]], "colour": "#ff0000", "fill": True}]  # around 0.75, 0.5
    r = Report()
    whole = _arr(_convert(src, tmp_path / "a", r, marks=dot))
    assert tuple(whole[200, 600]) == (255, 0, 0) and tuple(whole[200, 400]) == (128, 128, 128)  # where it was put
    assert has(r.notes, "A drawing added")
    half = _arr(_convert(src, tmp_path / "b", marks=dot, area="50,0,50,100"))  # the right half only
    assert half.shape[:2] == (400, 400) and tuple(half[200, 200]) == (255, 0, 0)  # still on the same spot
    small = _arr(_convert(src, tmp_path / "c", marks=dot, max_size=200))
    assert tuple(small[50, 150]) == (255, 0, 0)  # scaled down with the picture


def test_every_kind_of_drawing_draws_and_the_highlighter_is_see_through(tmp_path):
    import numpy as np
    src = tmp_path / "grey.png"
    Image.new("RGB", (600, 400), (100, 100, 100)).save(src)
    kinds = [{"kind": "arrow", "points": [[0.1, 0.1], [0.4, 0.4]]}, {"kind": "line", "points": [[0.1, 0.9], [0.4, 0.9]]},
             {"kind": "box", "points": [[0.5, 0.1], [0.7, 0.3]]}, {"kind": "ellipse", "points": [[0.75, 0.1], [0.95, 0.3]], "fill": True},
             {"kind": "pen", "points": [[0.1, 0.6], [0.2, 0.7], [0.3, 0.6]]}]
    kinds += [{"kind": "sticker", "name": n, "points": [[0.15 + 0.15 * i, 0.5]], "n": 3} for i, n in enumerate(options.STICKERS)]
    for mark in kinds:
        a = _arr(_convert(src, tmp_path / f"{mark['kind']}{mark.get('name', '')}", marks=[mark]))
        assert (np.abs(a - 100).max(axis=2) > 60).sum() > 100, mark
    high = _arr(_convert(src, tmp_path / "high", marks=[{"kind": "highlight", "points": [[0.1, 0.5], [0.9, 0.5]],
                                                          "colour": "#ffff00", "width": 0.02}]))
    r, _, b = high[200, 300]
    assert 150 < r < 190 and b < 70  # yellow, about 40% over the grey: it shows through
    with pytest.raises(ValueError, match="An arrow has 2 points"):
        options.check_marks([{"kind": "arrow", "points": [[0.1, 0.1]]}])
    with pytest.raises(ValueError, match="A sticker is one of"):
        options.check_marks([{"kind": "sticker", "name": "skull", "points": [[0.1, 0.1]]}])


def test_watermark_picture_with_its_opacity_and_said_plainly_when_gone(tmp_path):
    import numpy as np
    src = tmp_path / "grey.png"
    Image.new("RGB", (800, 600), (100, 100, 100)).save(src)
    logo = tmp_path / "logo.png"
    Image.new("RGBA", (200, 100), (255, 0, 0, 255)).save(logo)
    r = Report()
    solid = _arr(_convert(src, tmp_path / "a", r, watermark_image=str(logo), watermark_opacity=100))
    faint = _arr(_convert(src, tmp_path / "b", watermark_image=str(logo), watermark_opacity=30))
    red = np.nonzero(solid[..., 1] < 10)
    assert len(red[0]) > 5000 and red[1].min() > 500 and red[0].min() > 400  # in the bottom-right corner
    y, x = int(red[0].mean()), int(red[1].mean())
    assert tuple(solid[y, x]) == (255, 0, 0)
    assert abs(faint[y, x, 0] - (0.3 * 255 + 0.7 * 100)) < 3 and abs(faint[y, x, 1] - 70) < 3  # 30%: see-through
    assert has(r.notes, "Watermarked: logo.png")
    both = _arr(_convert(src, tmp_path / "c", watermark_image=str(logo), watermark="me"))
    rows = np.nonzero(both[..., 0] - both[..., 1] > 30)[0]
    white = np.nonzero(both.min(axis=2) > 140)[0]
    assert rows.max() < white.min()  # the picture above the text, the text in the corner
    assert options.lasting({"watermark_image": "a.png", "watermark_opacity": 40}) == {"watermark_image": "a.png", "watermark_opacity": 40}
    logo.unlink()
    with pytest.raises(ConversionError, match="The watermark picture logo.png isn't there any more"):
        _convert(src, tmp_path / "d", watermark_image=str(logo))
    (tmp_path / "notes.png").write_text("not a picture")
    with pytest.raises(ConversionError, match="doesn't open as a picture"):
        _convert(src, tmp_path / "e", watermark_image=str(tmp_path / "notes.png"))


def test_the_deeper_tools_are_offered_where_they_make_sense():
    every = {"adjust", "straighten", "hides", "marks", "watermark_image", "watermark_opacity"}
    for src, dst in (("jpg", "jpg"), ("png", "webp"), ("gif", "gif"), ("mp4", "mp4"), ("mp4", "gif")):
        assert every <= set(registry.find_route(src, dst).settings), (src, dst)
    xbm = set(registry.find_route("png", "xbm").settings)
    assert {"straighten", "hides"} <= xbm and not {"adjust", "marks"} & xbm  # 1 bit: hidden, not recoloured
    assert not every & set(registry.find_route("png", "svg").settings)


def test_hide_and_adjust_from_the_command_line(tmp_path, capsys):
    import numpy as np
    src = _scene(tmp_path / "scene.png")
    assert main([str(src), "--to", "png", "--hide", "cover:0,0,25,25", "--hide", "blur/40:50,50,10,10",
                 "--adjust", "brightness=20", "--straighten", "1.5", "-o", str(tmp_path / "out"), "--json"]) == 0
    report = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert has(report["notes"], "2 parts hidden (blurred, covered)") and has(report["notes"], "Adjusted: brightness +20")
    assert _arr(tmp_path / "out" / "scene.png")[:100, :140].max() == 0  # the covered corner
    assert [h["how"] for h in options.items(report["settings"]["hides"])] == ["cover", "blur"]
    assert main([str(src), "--to", "png", "--hide", "smudge:1,1,5,5"]) == 2
    assert "one of: blur, pixelate, cover" in capsys.readouterr().err
    with pytest.raises(SystemExit) as done:  # the help prints (a % in it once stopped it)
        main(["--help"])
    assert done.value.code == 0 and "--hide HOW:L,T,W,H" in capsys.readouterr().out
    assert np.asarray(Image.open(tmp_path / "out" / "scene.png")).shape[:2] == (480, 640)


def test_text_crop_and_watermark_from_the_command_line(tmp_path, capsys):
    src = _scene(tmp_path / "meme.png")
    assert main([str(src), "--to", "jpg", "--text", "top words", "--text", "bottom words", "--text-style", "meme",
                 "--area", "0,0,50,100", "--watermark", "me", "-o", str(tmp_path / "out"), "--json"]) == 0
    report = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert "2 texts added" in report["notes"] and "Watermarked: me" in report["notes"]
    with Image.open(tmp_path / "out" / "meme.jpg") as img:
        assert img.size == (320, 480)
    boxes = options.boxes(report["settings"]["texts"])
    assert [b["y"] for b in boxes] == [0.09, 0.91] and all(b["meme"] for b in boxes)  # top, then bottom
    assert main([str(src), "--to", "jpg", "--text", "a", "--text-at", "top", "--text-at", "bottom"]) == 2
    assert "--text-at for each --text" in capsys.readouterr().err


@pytest.mark.parametrize("fmt", ["exr", "hdr"])
@pytest.mark.parametrize("out", ["png", "tiff"])
def test_colour_depth_of_a_picture_with_more_than_8_bits(samples, tmp_path, fmt, out):
    src = samples(fmt)
    default = pipeline.convert(src, out, tmp_path / "a")
    eight = pipeline.convert(src, out, tmp_path / "b", options={"bit_depth": "8"})
    if out == "png":  # the depth is the 25th byte of a PNG (under 8 when a palette will do)
        assert default.read_bytes()[24] == 16 and eight.read_bytes()[24] <= 8
    assert _size(eight) < _size(default)
    assert has(notes(fmt, out, bit_depth="8"), "8 bits a channel")
    assert not has(notes(fmt, out), "8 bits a channel")
    assert "bit_depth" in registry.find_route(fmt, out).settings


def test_colour_depth_is_a_choice_of_two():
    assert options.SETTINGS["bit_depth"].check("8") == "8" and options.SETTINGS["bit_depth"].check("16") == "16"
    with pytest.raises(ValueError):
        options.SETTINGS["bit_depth"].check("10")


def test_dpi(tmp_path):
    import pymupdf
    doc = pymupdf.open()
    doc.new_page(width=72 * 4, height=72 * 2)
    doc.save(tmp_path / "one.pdf")
    out = pipeline.convert(tmp_path / "one.pdf", "png", tmp_path / "o", options={"dpi": 300})
    with Image.open(out) as img:
        assert img.size == (1200, 600)


def test_cad_detail(samples, tmp_path):
    step = samples("step")
    counts = {}
    for level in ("coarse", "fine"):
        r = Report()
        out = pipeline.convert(step, "stl", tmp_path / level, report=r, options={"detail": level})
        counts[level] = assurance.inspect(out, "stl")["triangles"]
        assert r.settings == {"detail": level}
    assert counts["fine"] > 2 * counts["coarse"]


@needs("ffmpeg")
def test_media_settings(samples, tmp_path):
    mp4 = samples("mp4")
    gif = pipeline.convert(mp4, "gif", tmp_path / "g", options={"fps": 5})
    with Image.open(gif) as img:
        assert img.n_frames <= 11  # 2 s at 5 frames a second (12 by default: 24)
    small = pipeline.convert(mp4, "webm", tmp_path / "v", report=(r := Report()), options={"video_size": "360p"})
    assert assurance.inspect(small, "webm")["height"] <= 120  # 160x120 is smaller already: never enlarged
    assert r.settings == {"video_size": "360p"} and not r.warnings
    wav = samples("wav")
    rates = {b: _size(pipeline.convert(wav, "mp3", tmp_path / str(b), options={"bitrate": b})) for b in (64, 256)}
    assert rates[256] > 2.5 * rates[64]


def test_bad_settings_are_refused(tmp_path):
    src = tmp_path / "a.png"
    picture().save(src)
    with pytest.raises(ConversionError, match="from 1 to 100"):
        pipeline.convert(src, "jpg", tmp_path, options={"quality": 300})
    with pytest.raises(ConversionError, match="Unknown setting"):
        pipeline.convert(src, "jpg", tmp_path, options={"sharpness": 3})


def test_settings_a_route_doesnt_take_change_nothing(tmp_path):
    src = tmp_path / "a.png"
    picture().save(src)
    a = pipeline.convert(src, "bmp", tmp_path / "a")
    r = Report()
    b = pipeline.convert(src, "bmp", tmp_path / "b", report=r, options={"quality": 10, "dpi": 600, "bitrate": 64})
    assert a.read_bytes() == b.read_bytes() and r.settings == {}


def test_media_checks_start_once_ffmpeg_is_installed(tmp_path, monkeypatch):
    from latheport import engines
    monkeypatch.setattr(engines, "find", lambda key: None)
    assert assurance._ffprobe() is None
    (tmp_path / "ffmpeg.exe").write_bytes(b"")
    (tmp_path / "ffprobe.exe").write_bytes(b"")
    monkeypatch.setattr(engines, "find", lambda key: str(tmp_path / "ffmpeg.exe"))
    assert assurance._ffprobe() == str(tmp_path / "ffprobe.exe")  # no restart needed


# ---------------------------------------------------------------- presets

def test_builtin_presets_are_valid():
    for name, values in options.PRESETS.items():
        assert options.clean(values) == values, name


def test_save_load_delete_presets(tmp_path, monkeypatch):
    monkeypatch.setenv("LATHEPORT_PRESETS", str(tmp_path / "presets.json"))
    options.save_preset("Archive scans", {"dpi": 400, "quality": 90})
    assert options.preset("archive scans") == {"dpi": 400, "quality": 90}
    assert "Archive scans" in options.all_presets()
    with pytest.raises(ValueError, match="built-in"):
        options.save_preset("Best quality", {"quality": 50})
    with pytest.raises(ValueError):
        options.save_preset("Bad", {"quality": "loud"})
    options.delete_preset("Archive scans")
    assert "Archive scans" not in options.all_presets()
    (tmp_path / "presets.json").write_text("{not json")
    assert options.user_presets() == {}  # a damaged file never breaks the app


# ---------------------------------------------------------------- the command line

def test_cli_plan_converts_nothing(tmp_path, capsys):
    src = tmp_path / "a.png"
    picture().save(src)
    assert main([str(src), "--to", "jpg", "--plan", "--quality", "80"]) == 0
    out = capsys.readouterr().out
    assert "PNG → JPG via Pillow" in out and "transparency" in out and "quality=80" in out
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.png"]
    assert main([str(src), "--to", "jpg", "--plan", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["route"] == "PNG → JPG" and plan["takes"] == ["quality", "max_size", "upscale", "rotate", "straighten", "hides", "crop", "area", "adjust", "enhance", "colour", "background", "marks", "texts", "watermark", "watermark_at", "watermark_image", "watermark_opacity", "deepfry"] and plan["facts"]["width"] == 160


def test_cli_json_reports(tmp_path, capsys):
    src = tmp_path / "a.png"
    picture().save(src)
    (tmp_path / "bad.png").write_bytes(b"nothing")
    assert main([str(src), str(tmp_path / "bad.png"), "--to", "jpg", "--json", "--preset", "Smaller for email and web",
                 "-o", str(tmp_path / "out")]) == 1
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[0]["settings"] == {"quality": 75, "max_size": 1920} and lines[0]["warnings"] == []
    assert set(lines[0]) >= {"source", "output", "route", "steps", "notes", "checks", "seconds"}
    assert lines[1]["source"].endswith("bad.png") and lines[1]["error"]


def test_cli_text_shows_notes_and_checks(tmp_path, capsys):
    src = tmp_path / "a.png"
    picture().save(src)
    assert main([str(src), "--to", "jpg"]) == 0
    out = capsys.readouterr().out
    assert "JPG has no transparency" in out and "Checked, the same: frames 1" in out


def test_cli_settings_errors(tmp_path, capsys):
    src = tmp_path / "a.png"
    picture().save(src)
    assert main([str(src), "--to", "jpg", "--preset", "nope"]) == 2
    assert main([str(src), "--to", "jpg", "--video-quality", "huge"]) == 2
    assert main(["presets"]) == 0
    assert "Ready for 3D printing" in capsys.readouterr().out



# ---------------------------------------------------------------- found by checking every built-in route

def test_obj_objects_stay_separate_parts(samples, tmp_path):
    obj = samples("obj")
    assert assurance.inspect(obj, "obj")["parts"] == 2
    for fmt in ("glb", "3mf", "fbx"):
        r = Report()
        out = pipeline.convert(obj, fmt, tmp_path / fmt, report=r)
        assert assurance.inspect(out, fmt)["parts"] == 2, fmt  # trimesh alone made them one
        assert not r.warnings, r.lines()


def test_database_to_jsonl_and_plist(tmp_path):
    import plistlib
    import sqlite3
    db = tmp_path / "shop.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE zebra (n INTEGER)")
    con.executemany("INSERT INTO zebra VALUES (?)", [(1,), (2,)])
    con.execute("CREATE TABLE apple (n INTEGER)")
    con.execute("INSERT INTO apple VALUES (3)")
    con.commit()
    con.close()
    r = Report()
    out = pipeline.convert(db, "jsonl", tmp_path / "j", report=r)
    assert out.read_text(encoding="utf-8").splitlines() == ['{"n": 1}', '{"n": 2}']  # one record a line
    assert has(r.notes, "JSONL holds one table") and not r.warnings
    out = pipeline.convert(db, "plist", tmp_path / "p")
    assert list(plistlib.loads(out.read_bytes())) == ["zebra", "apple"]  # in the database's order


def test_sami_clearing_cues_arent_subtitles(samples, tmp_path):
    r = Report()
    pipeline.convert(samples("srt"), "smi", tmp_path, report=r)
    assert {c.name: (c.before, c.after) for c in r.checks}["Subtitles"] == (2, 2)


def test_big_counts_are_easy_to_read():
    from latheport.assurance import show
    assert (show(30682), show(1920), show(660000), show(6.63325), show(True)) == ("30,682", "1920", "660,000", "6.633", "yes")


@pytest.mark.parametrize("fmt", ["icns", "ico", "png", "webp", "gif", "tiff"])
def test_transparency_check_reads_every_picture_kind(fmt, tmp_path):
    """ICNS reports RGBA before decoding and RGB after; the check must not trip over that."""
    from PIL import Image

    from latheport import assurance
    for mode, see_through in (("RGB", False), ("RGBA", True)):
        im = Image.new(mode, (64, 64), (200, 30, 30) if mode == "RGB" else (200, 30, 30, 0))
        path = tmp_path / f"{mode}.{fmt}"
        im.save(path)
        facts = assurance.inspect(path, fmt)
        assert facts["width"] == (1024 if fmt == "icns" else 64)  # ICNS opens at its biggest size
        if fmt != "icns":  # ICNS keeps no transparency for this size in Pillow's writer
            assert facts["transparency"] == see_through, (fmt, mode)


@pytest.mark.parametrize("fmt", ["avif", "webp", "png"])
def test_an_animations_length_is_measured_in_every_format(tmp_path, fmt):
    """Pillow gives an AVIF frame its duration only once the frame is decoded: the check must
    still find the length (it read 0 s and warned on every animation made into AVIF)."""
    from PIL import Image
    frames = [Image.new("RGB", (40, 30), (40 * i, 0, 0)) for i in range(6)]
    src = tmp_path / "anim.gif"
    frames[0].save(src, save_all=True, append_images=frames[1:], duration=200, loop=0)
    report = Report()
    out = pipeline.convert(src, fmt, out_dir=tmp_path / "out", report=report)
    assert assurance.inspect(out, fmt)["duration"] == pytest.approx(1.2, abs=0.05)
    assert not report.warnings, report.lines()
