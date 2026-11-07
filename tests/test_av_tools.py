"""Cutting, fitting in a size, evening out loudness and joining sound and video (FFmpeg)."""

import subprocess
from pathlib import Path

import pytest

from convertall import engines, options, pipeline, registry
from convertall.assurance import Report
from convertall.converters import media
from convertall.errors import ConversionError

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
    monkeypatch.setenv("CONVERTALL_PRESETS", str(tmp_path / "presets.json"))
    values = options.clean({"start": "5", "end": "9", "target_size": "10", "loudness": "streaming"})
    assert options.lasting(values) == {"target_size": 10, "loudness": "streaming"}
    options.save_preset("Discord", values)
    assert options.preset("Discord") == {"target_size": 10, "loudness": "streaming"}


def test_sound_and_video_can_be_converted_to_their_own_format():
    for fmt in ("mp4", "mkv", "webm", "mp3", "wav", "flac"):
        route = registry.find_route(fmt, fmt)
        assert len(route.edges) == 1
        assert {"start", "end", "loudness"} <= set(route.settings)
    with pytest.raises(Exception, match="Can't convert"):
        registry.find_route("png", "png")  # pictures aren't encoded again for nothing
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
    from convertall import assurance
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
