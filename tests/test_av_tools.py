"""Cutting, fitting in a size, evening out loudness and joining sound and video (FFmpeg)."""

import subprocess
from pathlib import Path

import pytest

from latheport import engines, options, pipeline, registry
from latheport.assurance import Report
from latheport.converters import media
from latheport.errors import ConversionError

FFMPEG = engines.find("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="FFmpeg isn't installed")


def make(path: Path, *args: str) -> Path:
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args, str(path)], check=True,
                   creationflags=engines.NO_WINDOW)
    return path


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    if not FFMPEG:
        pytest.skip("FFmpeg isn't installed")
    d = tmp_path_factory.mktemp("av")
    return {
        "video": make(d / "clip.mp4", "-f", "lavfi", "-i", "testsrc2=size=640x480:rate=25", "-f", "lavfi",
                      "-i", "sine=frequency=440:sample_rate=44100", "-t", "12", "-c:v", "libx264", "-crf", "16",
                      "-c:a", "aac", "-b:a", "192k", "-pix_fmt", "yuv420p"),
        "silent_video": make(d / "silent.mp4", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30", "-t", "3",
                             "-c:v", "libx264", "-pix_fmt", "yuv420p"),
        "quiet": make(d / "quiet.wav", "-f", "lavfi", "-i", "sine=frequency=300:sample_rate=48000", "-af",
                      "volume=0.05", "-t", "8"),
        "song": make(d / "song.flac", "-f", "lavfi", "-i", "sine=frequency=500:sample_rate=44100", "-t", "30"),
        "grain": make(d / "grain.mp4", "-f", "lavfi", "-i", "testsrc2=size=640x480:rate=25,noise=alls=40:allf=t+u",
                      "-t", "3", "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p"),  # detail everywhere
    }


def checks(report: Report) -> dict:
    return {c.name: c for c in report.checks}


# ---- settings

@needs_ffmpeg
@pytest.mark.parametrize(("rate", "kind", "fmt", "expected"), [
    (96000, "wav", "wma", 48000), (352800, "wav", "wma", 48000), (352800, "wav", "ogg", 192000),
    (96000, "mkv", "wmv", 48000), (96000, "mkv", "asf", 48000), (352800, "mkv", "ogv", 192000),
    (44100, "wav", "wma", 44100), (96000, "wav", "ogg", 96000), (96000, "wav", "flac", 96000),
])
def test_sound_too_fine_for_the_encoder_is_brought_to_what_it_takes(tmp_path, rate, kind, fmt, expected):
    """WMA stops at 48 kHz and Vorbis at about 200: FFmpeg doesn't choose for them. DSD comes out of FFmpeg at 352.8 kHz."""
    src = tmp_path / f"fine.{kind}"
    picture = ["-f", "lavfi", "-i", "testsrc=size=64x64:rate=5:duration=1", "-c:v", "libx264", "-c:a", "flac"] if kind == "mkv" else []
    make(src, "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate={rate}:duration=1", *picture)
    report = Report()
    out = pipeline.convert(src, fmt, out_dir=tmp_path, report=report)
    assert media.probe(out)["rate"] == expected
    assert any("kHz" in n for n in report.notes) == (expected != rate), report.lines()


def test_times_are_read_in_every_usual_way():
    st = options.SETTINGS["start"]
    assert [st.check(v) for v in ("90", "90s", "1:30", 90, "0:01:30")] == ["1:30"] * 5
    assert st.check("1:02:03.5") == "1:02:03.5"
    assert st.check("0:05,25") == "0:05.25"
    for bad in ("1:75", "abc", "-3", "1:2:3:4", "200:00:00"):
        with pytest.raises(ValueError):
            st.check(bad)


def test_end_must_come_after_start():
    with pytest.raises(ValueError, match="after"):
        options.clean({"start": "1:00", "end": "0:30"})


def test_a_trim_is_not_kept_for_next_time_or_in_presets(tmp_path, monkeypatch):
    monkeypatch.setenv("LATHEPORT_PRESETS", str(tmp_path / "presets.json"))
    values = options.clean({"start": "5", "end": "9", "target_size": "10", "loudness": "streaming"})
    assert options.lasting(values) == {"target_size": 10, "loudness": "streaming"}
    options.save_preset("Discord", values)
    assert options.preset("Discord") == {"target_size": 10, "loudness": "streaming"}


def test_sound_and_video_can_be_converted_to_their_own_format():
    for fmt in ("mp4", "mkv", "webm", "mp3", "wav", "flac"):
        route = registry.find_route(fmt, fmt)
        assert len(route.edges) == 1
        assert {"start", "end", "loudness"} <= set(route.settings)
    assert {"max_size", "deepfry"} <= set(registry.find_route("png", "png").settings)  # pictures too: shrink or fry
    with pytest.raises(Exception, match="Can't convert"):
        registry.find_route("docx", "docx")  # but not every kind (PDF to PDF changes pages: test_pdf_tools)
    assert "target_size" in registry.find_route("mov", "mp4").settings
    assert "target_size" not in registry.find_route("mp4", "wav").settings  # no size to choose for PCM


def test_mp3_bitrates_round_down_to_one_the_encoder_has(monkeypatch):
    monkeypatch.setattr(registry, "option", lambda key, default=None: 1 if key == "target_size" else default)
    monkeypatch.setattr(media, "option", lambda key, default=None: 1 if key == "target_size" else default)
    args = media._fit_audio(media.AUDIO_ARGS["mp3"], 55.0, "mp3")
    assert args[args.index("-b:a") + 1] == "128k"  # 146 kbit/s would fit; LAME has 128 and 160
    with pytest.raises(ConversionError, match="can't fit"):
        media._fit_audio(media.AUDIO_ARGS["mp3"], 3600.0, "mp3")


# ---- with FFmpeg

@needs_ffmpeg
def test_cut_keeps_exactly_the_span(clips, tmp_path):
    report = Report()
    out = pipeline.convert(clips["video"], "mp4", tmp_path, report=report, options={"start": "2.5", "end": "0:07"})
    length = checks(report)["Length"]
    assert length.state == "expected" and abs(length.after - 4.5) <= 0.06
    assert media.probe(out)["duration"] == pytest.approx(4.5, abs=0.06)


@needs_ffmpeg
def test_cut_from_a_start_only_and_into_another_format(clips, tmp_path):
    report = Report()
    pipeline.convert(clips["video"], "mp3", tmp_path, report=report, options={"start": "10"})
    assert abs(checks(report)["Length"].after - 2.0) <= 0.06
    with pytest.raises(ConversionError, match="past the end"):
        pipeline.convert(clips["video"], "mp3", tmp_path, options={"start": "20"})


@needs_ffmpeg
@pytest.mark.parametrize("fmt", ["mp4", "webm"])
def test_fit_in_a_size(clips, tmp_path, fmt):
    report = Report()
    out = pipeline.convert(clips["video"], fmt, tmp_path, report=report, options={"target_size": 1})
    mb = out.stat().st_size / 2**20
    assert 0.85 <= mb <= 1.0, mb
    assert checks(report)["File size"].state == "expected"


@needs_ffmpeg
def test_fit_in_a_size_never_pads_a_small_video(clips, tmp_path):
    report = Report()
    out = pipeline.convert(clips["silent_video"], "mp4", tmp_path, report=report, options={"target_size": 10})
    assert out.stat().st_size < 2**20  # 3 s that already look right at well under 10 MB stay small
    assert any("full quality" in n for n in report.notes)


@needs_ffmpeg
def test_fit_in_a_size_says_so_when_it_cannot(tmp_path):
    long_clip = make(tmp_path / "long.mp4", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10", "-t", "300",
                     "-c:v", "libx264", "-pix_fmt", "yuv420p")
    with pytest.raises(ConversionError, match="can't fit"):
        pipeline.convert(long_clip, "mp4", tmp_path, options={"target_size": 1})


@needs_ffmpeg
@pytest.mark.parametrize("level,lufs", [("streaming", -14.0), ("broadcast", -23.0)])
def test_loudness_is_evened_out(clips, tmp_path, level, lufs):
    report = Report()
    out = pipeline.convert(clips["quiet"], "mp3", tmp_path, report=report, options={"loudness": level})
    check = checks(report)["Loudness"]
    assert check.state == "expected" and abs(check.after - lufs) <= 1.0
    from latheport import assurance
    assert abs(assurance.loudness(out) - lufs) <= 1.0


@needs_ffmpeg
def test_join_videos_with_and_without_sound(clips, tmp_path):
    report = Report()
    out = pipeline.join([clips["video"], clips["silent_video"]], "mp4", tmp_path, report=report)
    assert out.name == "clip (joined).mp4"
    info = media.probe(out)
    assert info["duration"] == pytest.approx(15.0, abs=0.25)
    assert (info["width"], info["height"]) == (640, 480)  # the first video's size, the other's fitted in
    assert info["audio"]
    assert checks(report)["Length"].state == "expected"


@needs_ffmpeg
def test_join_sound_with_even_loudness(clips, tmp_path):
    report = Report()
    out = pipeline.join([clips["quiet"], clips["song"], clips["video"]], "m4a", tmp_path, report=report,
                        options={"loudness": "streaming"})
    assert media.probe(out)["duration"] == pytest.approx(50.0, abs=0.3)
    assert abs(checks(report)["Loudness"].after + 14.0) <= 1.0


@needs_ffmpeg
def test_join_refuses_what_it_cannot_join(clips, tmp_path):
    with pytest.raises(ConversionError, match="two files"):
        pipeline.join([clips["video"]], "mp4", tmp_path)
    with pytest.raises(ConversionError, match="sound only"):
        pipeline.join([clips["video"], clips["song"]], "mp4", tmp_path)
    picture = tmp_path / "a.png"
    picture.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 64)
    with pytest.raises(ConversionError, match="isn't a sound or video"):
        pipeline.join([clips["video"], picture], "mp4", tmp_path)
    with pytest.raises(ConversionError, match="not PDF"):
        pipeline.join([clips["video"], clips["silent_video"]], "pdf", tmp_path)


# ---- effects on video and sound: turned, cropped, recoloured, text, speed, backwards

def frame_at(path: Path, t: float, tmp: Path):
    from PIL import Image
    png = make(tmp / f"{path.stem}-{t}.png", "-ss", str(t), "-i", str(path), "-frames:v", "1")
    return Image.open(png).convert("RGB")


def pitch(path: Path) -> float:
    """The loudest frequency of a second of sound from half a second in, in Hz."""
    import numpy as np
    raw = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn", "-ac", "1", "-ar",
                          "48000", "-f", "f32le", "-"], capture_output=True, check=True,
                         creationflags=engines.NO_WINDOW).stdout
    x = np.frombuffer(raw, dtype=np.float32)[24000:72000]
    return float(np.fft.rfftfreq(len(x), 1 / 48000)[np.abs(np.fft.rfft(x * np.hanning(len(x)))).argmax()])


@pytest.fixture(scope="module")
def ramp(tmp_path_factory):
    """6 s that brighten steadily, with a tone that rises: backwards, both fall."""
    if not FFMPEG:
        pytest.skip("FFmpeg isn't installed")
    d = tmp_path_factory.mktemp("ramp")
    return make(d / "ramp.mp4", "-f", "lavfi", "-i", "color=c=black:s=320x240:r=30:d=6,geq=lum='min(255,T*40)':cb=128:cr=128",
                "-f", "lavfi", "-i", "aevalsrc='0.5*sin(2*PI*(200+60*t)*t)':d=6", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-shortest")


def brightness(path: Path):
    import numpy as np
    raw = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(path), "-vf", "scale=16:12,format=gray",
                          "-f", "rawvideo", "-"], capture_output=True, check=True, creationflags=engines.NO_WINDOW).stdout
    return np.frombuffer(raw, dtype=np.uint8).reshape(-1, 16 * 12).mean(axis=1).astype(float)


def rising(path: Path) -> bool:
    """Whether the sound's pitch goes up from start to end."""
    import numpy as np
    raw = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn", "-ac", "1", "-ar",
                          "8000", "-f", "f32le", "-"], capture_output=True, check=True,
                         creationflags=engines.NO_WINDOW).stdout
    x = np.frombuffer(raw, dtype=np.float32)
    peak = [np.fft.rfftfreq(2048, 1 / 8000)[np.abs(np.fft.rfft(x[i:i + 2048])).argmax()]
            for i in range(0, len(x) - 2048, 4000)]
    return peak[-1] > peak[0]


@needs_ffmpeg
def test_video_turned_and_cropped_also_when_the_phone_tagged_it_turned(clips, tmp_path):
    r = Report()
    out = pipeline.convert(clips["video"], "mp4", tmp_path / "a", options={"rotate": "right"}, report=r)
    info = media.probe(out)
    assert (info["width"], info["height"]) == (480, 640)
    assert checks(r)["Width"].state == "expected" and not r.warnings
    phone = make(tmp_path / "phone.mp4", "-display_rotation", "90", "-i", str(clips["silent_video"]), "-c", "copy")
    assert (media.probe(phone)["width"], media.probe(phone)["height"]) == (360, 640)  # as it's shown
    out = pipeline.convert(phone, "mp4", tmp_path / "b", options={"crop": "square"})
    assert (media.probe(out)["width"], media.probe(out)["height"]) == (360, 360)
    out = pipeline.convert(clips["video"], "webm", tmp_path / "c", options={"area": "10,10,50,50"})
    assert (media.probe(out)["width"], media.probe(out)["height"]) == (320, 240)


@needs_ffmpeg
def test_video_black_and_white_and_text_on_it(clips, tmp_path):
    import numpy as np
    r = Report()
    texts = options.check_boxes([{"text": "HI", "x": 0.5, "y": 0.5, "size": 0.3, "font": "bold", "colour": "#ff0000",
                                  "edge": "none"}])
    out = pipeline.convert(clips["video"], "mp4", tmp_path / "a", options={"colour": "bw", "texts": texts,
                                                                          "watermark": "me"}, report=r)
    hsv = np.asarray(frame_at(out, 2, tmp_path).convert("HSV"))
    assert hsv[:100, :100, 1].mean() < 8                      # the picture is grey...
    assert (hsv[200:280, 280:360, 1] > 150).mean() > 0.2      # ...the red text on it isn't
    assert {"Text added", "Watermarked: me", "Made black and white"} <= set(r.notes)


def _as_picture(frame, opts: dict):
    """frame with the same effects done on it as a picture (what the editor shows)."""
    from latheport.converters import images
    tokens = registry.running(options.clean(opts), [], [])
    try:
        return images.effects([frame.copy()], "png")[0].convert("RGB")
    finally:
        registry.reset(tokens)


@needs_ffmpeg
@pytest.mark.parametrize("opts", [
    {"adjust": "brightness=50"}, {"adjust": "contrast=50,saturation=-60"}, {"adjust": "warmth=80,sharpness=60"},
    {"straighten": "4"},
], ids=["brightness", "contrast-saturation", "warmth-sharpness", "straighten"])
def test_video_effects_come_out_as_the_editor_shows_them(clips, tmp_path, opts):
    """Each effect done by FFmpeg on the video matches the same effect done on a frame as a picture
    (what the editor shows), and changes the frame by much more than that difference."""
    import numpy as np
    plain = frame_at(pipeline.convert(clips["video"], "mp4", tmp_path / "a"), 2, tmp_path / "a")
    r = Report()
    done = frame_at(pipeline.convert(clips["video"], "mp4", tmp_path / "b", options=options.clean(opts), report=r), 2, tmp_path / "b")
    a, b, pic = (np.asarray(x, dtype=np.float32) for x in (plain, done, _as_picture(plain, opts)))
    assert done.size == plain.size
    apart, changed = np.abs(b - pic).mean(), np.abs(b - a).mean()
    assert apart < 5 and changed > 2 * apart, (apart, changed)
    assert r.notes


@needs_ffmpeg
@pytest.mark.parametrize("how", ["pixelate", "blur"])
def test_video_hides_its_part_on_every_frame_and_nothing_else(clips, tmp_path, how):
    import numpy as np
    plain = pipeline.convert(clips["grain"], "mp4", tmp_path / "a")
    done = pipeline.convert(clips["grain"], "mp4", tmp_path / "b", options=options.clean(
        {"hides": [{"x": 0.25, "y": 0.25, "w": 0.4, "h": 0.3, "how": how}]}))
    part = (slice(120, 264), slice(160, 416))  # 256 x 144 from 160, 120
    for t in (1, 2.5):
        a = np.asarray(frame_at(plain, t, tmp_path / "a"), dtype=np.float32)
        b = np.asarray(frame_at(done, t, tmp_path / "b"), dtype=np.float32)
        outside = np.ones(a.shape[:2], bool)
        outside[116:268, 156:420] = False
        assert np.abs(b - a)[outside].mean() < 3  # only the encoder's own noise past the part (grain is hard on it)
        if how == "blur":
            detail = lambda x: (np.diff(x.mean(axis=2), axis=1) ** 2).mean()
            assert detail(b[part]) < 0.35 * detail(a[part]), t  # the detail in it is gone
        else:  # flat blocks: side by side, nearly every pair of pixels in it the same
            flat = lambda x: (np.abs(np.diff(x.mean(axis=2), axis=1)) < 4).mean()
            assert flat(b[part]) > 0.85 and flat(b[part]) > flat(a[part]) + 0.1, t
        if how == "pixelate":  # and as the editor shows it
            from PIL import Image
            pic = np.asarray(_as_picture(Image.fromarray(a.astype(np.uint8)), {"hides": [
                {"x": 0.25, "y": 0.25, "w": 0.4, "h": 0.3, "how": how}]}), dtype=np.float32)
            # FFmpeg averages each block in its own colour space: a few levels from the picture's average
            assert np.median(np.abs(b[part] - pic[part]).mean(axis=2)) < 10


@needs_ffmpeg
def test_video_hides_covers_draws_and_watermarks_with_a_picture(clips, tmp_path):
    import numpy as np
    from PIL import Image
    logo = tmp_path / "logo.png"
    Image.new("RGBA", (120, 60), (0, 0, 255, 255)).save(logo)
    r = Report()
    opts = options.clean({"hides": [{"x": 0, "y": 0, "w": 0.25, "h": 0.25, "how": "cover"}],
                          "marks": [{"kind": "ellipse", "points": [[0.45, 0.45], [0.55, 0.55]], "colour": "#ff0000", "fill": True}],
                          "watermark_image": str(logo), "watermark_opacity": 100, "crop": "square"})
    out = pipeline.convert(clips["video"], "mp4", tmp_path / "a", options=opts, report=r)
    f = np.asarray(frame_at(out, 2, tmp_path), dtype=np.float32)
    assert f.shape[:2] == (480, 480)
    assert f[10:100, 10:30].max() < 30  # covered (the square is cut from the middle: x 80 onwards of 640)
    r_, g, b = f[240, 240]
    assert r_ > 200 and g < 60 and b < 60  # the red drawing in the middle, still there after the crop
    corner = f[440:465, 400:465]  # the logo, 77 x 38 at 16% of the width, a 10-pixel margin from the corner
    assert ((corner[..., 2] > 180) & (corner[..., 0] < 60)).mean() > 0.9
    assert {"1 part hidden (covered)", "A drawing added", "Watermarked: logo.png"} <= set(r.notes)


def test_a_part_to_hide_is_split_off_changed_and_laid_back():
    hides = media.hide_filters([{"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2, "how": "pixelate", "strength": 70},
                                {"x": 0.5, "y": 0.5, "w": 0.2, "h": 0.2, "how": "cover", "strength": 70}], 640, 360)
    graph = media._graph(["hflip", *hides, "crop=320:180:0:0"], "", "scale=320:-2")
    assert graph == ("hflip[hd1];[hd1]split[hd1a][hd1b];[hd1b]crop=128:72:64:36,scale=21:12:flags=area,"
                     "scale=128:72:flags=neighbor[hd1c];[hd1a][hd1c]overlay=64:36[hd1d];"
                     "[hd1d]drawbox=x=320:y=180:w=128:h=72:color=black:t=fill,crop=320:180:0:0,scale=320:-2")
    assert media._graph(["hflip", "hue=s=0"], "", "scale=320:-2") == "hflip,hue=s=0,scale=320:-2"  # as before
    # the path escaped for the option's value (C\:) and then for the graph (C\\:), as FFmpeg reads it
    assert media._graph(["hflip"], "C:/t/o.png", "") == r"movie=C\\:/t/o.png[ov];hflip[base];[base][ov]overlay=0:0"


@needs_ffmpeg
@pytest.mark.parametrize("speed, pitch_kept", [("2", True), ("2", False), ("0.5", True), ("4", True)])
def test_speed_with_the_pitch_kept_or_not(clips, tmp_path, speed, pitch_kept):
    opts = {"speed": speed} if pitch_kept else {"speed": speed, "pitch": "change"}
    r = Report()
    out = pipeline.convert(clips["video"], "mp4", tmp_path / "a", options={**opts, "end": "8"}, report=r)
    assert abs(media.probe(out)["duration"] - 8 / float(speed)) < 0.15
    assert checks(r)["Length"].state == "expected" and not r.warnings
    hz = pitch(out)
    assert abs(hz - (440 if pitch_kept else 440 * float(speed))) < 15, hz


@needs_ffmpeg
def test_speed_of_sound_only(clips, tmp_path):
    sound = pipeline.convert(clips["song"], "mp3", tmp_path / "b", options={"speed": "1.5"})
    assert abs(media.probe(sound)["duration"] - 20) < 0.15 and abs(pitch(sound) - 500) < 15


@needs_ffmpeg
def test_backwards_plays_the_cut_in_reverse_frame_for_frame(ramp, tmp_path):
    import numpy as np
    src = brightness(ramp)
    r = Report()
    out = pipeline.convert(ramp, "mp4", tmp_path / "a", options={"reverse": "yes", "start": "1", "end": "5"}, report=r)
    got = brightness(out)
    assert len(got) == 120                                    # 4 s at 30 frames a second
    assert np.abs(got - src[30:150][::-1]).mean() < 3        # the same frames, last first
    assert "Played backwards" in r.notes and not r.warnings
    assert rising(ramp) and not rising(out)                  # the sound turned round too
    wav = pipeline.convert(ramp, "wav", tmp_path / "b", options={"reverse": "yes"})
    assert not rising(wav) and abs(media.probe(wav)["duration"] - 6) < 0.1


@needs_ffmpeg
def test_backwards_takes_a_long_video_in_pieces_and_says_no_past_the_limit(tmp_path, monkeypatch):
    import numpy as np
    long = make(tmp_path / "long.mp4", "-f", "lavfi", "-i",
                "color=c=black:s=640x360:r=30:d=12,geq=lum='min(255,T*20)':cb=128:cr=128",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p")
    out = pipeline.convert(long, "mp4", tmp_path / "a", options={"reverse": "yes"})
    got, src = brightness(out), brightness(long)
    assert len(got) == len(src) == 360 and np.abs(got - src[::-1]).mean() < 3  # 10 s pieces met without a seam
    monkeypatch.setattr(media, "REVERSE_MOST", 5)
    with pytest.raises(ConversionError, match="Backwards takes at most"):
        pipeline.convert(long, "mp4", tmp_path / "b", options={"reverse": "yes"})


@needs_ffmpeg
def test_gif_from_video_with_effects(clips, tmp_path):
    r = Report()
    out = pipeline.convert(clips["silent_video"], "gif", tmp_path / "a", options={"rotate": "left", "colour": "sepia",
                                                                                  "speed": "2"}, report=r)
    info = media.probe(out)
    assert info["width"] < info["height"] and abs(info["duration"] - 1.5) < 0.2
    assert "Played at 2x" in r.notes  # a GIF has no sound: no word about voices


def test_effects_are_offered_for_video_and_sound_where_they_apply():
    video = set(registry.find_route("mp4", "mp4").settings)
    assert {"rotate", "crop", "area", "colour", "texts", "watermark", "speed", "pitch", "reverse"} <= video
    sound = set(registry.find_route("mp3", "wav").settings)
    assert {"speed", "pitch", "reverse"} <= sound and not {"rotate", "area", "texts"} & sound
    assert {"rotate", "area", "speed"} <= set(registry.find_route("mp4", "gif").settings)
