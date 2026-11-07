"""Audio and video via FFmpeg: converting, and cutting (Start at / End at), fitting in a size,
evening out loudness and joining several files into one."""

import json
import math
import re
import subprocess
from pathlib import Path

from .. import engines
from ..errors import ConversionError
from ..losses import loss
from ..options import parse_time, show_time
from ..registry import converter, expect, note, option

EVEN = ["-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2"]  # H.264/yuv420p needs even dimensions
# FFmpeg can't read back Theora under 18 pixels a side: smaller pictures are enlarged, shape kept
THEORA_MIN = 18
_GROW = f"max(1,{THEORA_MIN}/min(iw,ih))"
EVEN_THEORA = ["-vf", f"scale='trunc(iw*{_GROW}/2+0.5)*2':'trunc(ih*{_GROW}/2+0.5)*2'"]
H264 = ["-c:v", "libx264", "-preset", "medium", "-crf", "23", "-pix_fmt", "yuv420p", *EVEN]
AAC = ["-c:a", "aac", "-b:a", "160k"]

AUDIO_ARGS = {
    "mp3": ["-c:a", "libmp3lame", "-q:a", "2"],
    "wav": ["-c:a", "pcm_s16le"],
    "flac": ["-c:a", "flac"],
    "ogg": ["-c:a", "libvorbis", "-q:a", "5"],
    "opus": ["-c:a", "libopus", "-b:a", "128k"],
    "m4a": ["-c:a", "aac", "-b:a", "192k"],
    "aac": ["-c:a", "aac", "-b:a", "192k"],
    "wma": ["-c:a", "wmav2", "-b:a", "192k"],
    "aiff": ["-c:a", "pcm_s16be"],
    "ac3": ["-c:a", "ac3", "-b:a", "448k"],
    "amr": ["-c:a", "libopencore_amrnb", "-ar", "8000", "-ac", "1", "-b:a", "12.2k"],
    "mka": ["-c:a", "libopus", "-b:a", "160k"],
    "m4b": ["-c:a", "aac", "-b:a", "96k"],  # audiobooks: speech needs little
    "wv": ["-c:a", "wavpack", "-frame_size", "4096"],  # MP3's short first frame upsets it otherwise
    "caf": ["-c:a", "alac"],
    "mp2": ["-c:a", "mp2", "-b:a", "256k", "-ar", "44100"],  # MP2 has no 8 kHz mode (phone audio)
    "au": ["-c:a", "pcm_s16be"],
    "m4r": ["-c:a", "aac", "-b:a", "192k", "-t", "40", "-f", "ipod"],  # iPhone ringtones stop at 40 s
}

VIDEO_ARGS = {
    "mp4": [*H264, *AAC, "-movflags", "+faststart"],
    "m4v": [*H264, *AAC, "-movflags", "+faststart"],
    "mov": [*H264, *AAC],
    "mkv": [*H264, *AAC],
    "ts": [*H264, *AAC],
    "flv": [*H264, *AAC, "-ar", "44100"],
    "3gp": [*H264, *AAC, "-ar", "44100"],
    "webm": ["-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0", "-row-mt", "1", "-pix_fmt", "yuv420p",
             "-c:a", "libopus", "-b:a", "128k"],
    "avi": ["-c:v", "mpeg4", "-q:v", "3", "-c:a", "libmp3lame", "-q:a", "3"],
    "wmv": ["-c:v", "wmv2", "-b:v", "4M", *EVEN, "-c:a", "wmav2", "-b:a", "192k"],
    "mpg": ["-c:v", "mpeg2video", "-q:v", "3", "-c:a", "mp2", "-b:a", "192k"],
    "ogv": ["-c:v", "libtheora", "-q:v", "7", "-pix_fmt", "yuv420p", *EVEN_THEORA, "-c:a", "libvorbis", "-q:a", "5"],
    "f4v": [*H264, *AAC],
    "asf": ["-c:v", "wmv2", "-b:v", "4M", *EVEN, "-c:a", "wmav2", "-b:a", "192k"],
    "vob": ["-c:v", "mpeg2video", "-q:v", "3", *EVEN, "-c:a", "ac3", "-b:a", "192k"],
}

# Hz: these encoders refuse anything finer and, unlike the rest, don't let FFmpeg choose a rate they take
TOP_RATE = {"wmav2": 48000, "libvorbis": 192000}
AUDIO = set(AUDIO_ARGS)
VIDEO = set(VIDEO_ARGS)
# FFmpeg reads these fine but can't (usefully) write them
AUDIO_IN = AUDIO | {"ape", "dsf", "dff"}
VIDEO_IN = VIDEO | {"mxf", "rm", "rmvb", "3g2"}


def ffmpeg(src: Path | list[Path], dst: Path | str, args: list[str], before: list[str] = ()) -> str:
    """Run FFmpeg on src (or several inputs, in order) into dst; before: input options for src
    (a start time). Returns what FFmpeg said (its stderr)."""
    exe = engines.require("ffmpeg")
    inputs = []
    for i, one in enumerate(src if isinstance(src, list) else [src]):
        # local files only: a playlist or concat file can't make FFmpeg fetch URLs
        inputs += ["-protocol_whitelist", "file", *(before if i == 0 else ()), "-i", str(one)]
    src = src[0] if isinstance(src, list) else src
    try:
        proc = engines.run([exe, "-hide_banner", "-nostdin", "-y", *inputs, *args, str(dst)])
        return getattr(proc, "stderr", "") or ""
    except ConversionError as exc:
        msg = str(exc)
        if "does not contain any stream" in msg or "matches no streams" in msg:
            kind = "subtitle" if "0:s:0" in args else "audio" if "-vn" in args else "video"
            raise ConversionError(f"{src.name} has no {kind} track to convert") from None
        if "only possible from text to text or bitmap to bitmap" in msg:
            raise ConversionError(f"{src.name}'s subtitles are pictures (DVD/Blu-ray style), not text") from None
        raise


def probe(src: Path) -> dict:
    """duration (s), width, height, fps, audio (bool) and rate (Hz) of a sound or video file."""
    from ..assurance import _ffprobe
    exe = _ffprobe()
    if not exe:
        engines.require("ffmpeg")
        raise ConversionError("FFmpeg's ffprobe is missing: reinstall FFmpeg")
    out = subprocess.run([exe, "-v", "error", "-protocol_whitelist", "file", "-show_entries",
                          "format=duration:stream=codec_type,width,height,avg_frame_rate,sample_rate,disposition",
                          "-of", "json", str(src)], capture_output=True, text=True, encoding="utf-8", errors="replace",
                         check=False, timeout=60, creationflags=engines.NO_WINDOW, stdin=subprocess.DEVNULL)
    info = json.loads(out.stdout or "{}")
    streams = [st for st in info.get("streams", []) if not (st.get("disposition") or {}).get("attached_pic")]
    facts = {"duration": 0.0, "audio": False, "rate": 48000}
    try:
        facts["duration"] = float(info["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        pass
    for st in streams:
        if st.get("codec_type") == "video" and st.get("width") and "width" not in facts:
            facts["width"], facts["height"] = int(st["width"]), int(st["height"])
            num, _, den = str(st.get("avg_frame_rate", "0/1")).partition("/")
            try:
                fps = float(num) / float(den or 1)
            except (ValueError, ZeroDivisionError):
                fps = 0.0
            facts["fps"] = fps if 0 < fps <= 1000 else 25.0
        elif st.get("codec_type") == "audio" and not facts["audio"]:
            facts["audio"] = True
            facts["rate"] = int(st.get("sample_rate") or 48000)
    return facts


# ---- cutting: Start at / End at

def _span(info: dict) -> tuple[float, float]:
    """(start, length) chosen for this file, in seconds; ConversionError when it's outside the file."""
    start = parse_time(option("start")) if option("start") else 0.0
    end = parse_time(option("end")) if option("end") else None
    length = info["duration"]
    if length and start >= length:
        raise ConversionError(f"Start at {show_time(start)} is past the end ({show_time(length)} long)")
    stop = min(end, length) if end is not None and length else end if end is not None else length
    return start, max(0.0, (stop or 0.0) - start)


def _cut(info: dict, video: bool) -> tuple[list[str], list[str]]:
    """(input options, output options) for the cut, and the length the result should have."""
    if not option("start") and not option("end"):
        return [], []
    start, length = _span(info)
    before = ["-ss", f"{start:.3f}"] if start else []
    after = ["-t", f"{length:.3f}"] if option("end") else []
    # one frame of video, or a sound frame and the encoder's padding, either way
    within = max(1.0 / info.get("fps", 25.0) if video else 0.0, 0.06)
    shown = f"{show_time(start)} to {show_time(start + length)}"
    expect("duration", round(length, 3), f"Cut to {shown}", within=within)
    return before, after


# ---- loudness: EBU R128 in two passes, the way FFmpeg's loudnorm is meant to be used

LOUDNESS = {"streaming": (-14.0, -1.0), "broadcast": (-23.0, -1.0)}  # (integrated LUFS, true peak dBTP)


def _loudnorm(src: Path | list[Path], info: dict, before: list[str], cut: list[str], graph: list[str] = (),
              label: str = "") -> list[str]:
    """The audio filter that brings src (as cut) to the chosen loudness, measured first."""
    target, peak = LOUDNESS[option("loudness")]
    first = f"loudnorm=I={target}:TP={peak}:LRA=11:print_format=json"
    measure = [*graph[:-1], graph[-1] + f";[{label}]{first}[m]", "-map", "[m]"] if graph else ["-af", first]
    said = ffmpeg(src, "-", [*cut, "-vn", *measure, "-f", "null"], before)
    found = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", said)
    if not found:
        raise ConversionError("FFmpeg couldn't measure how loud it is")
    m = json.loads(found[-1])
    if m.get("input_i") in ("-inf", None) or not math.isfinite(float(m["input_i"])):
        note("It's silent: there's no loudness to even out")
        return []
    expect("loudness", target, f"Evened out to {target:g} LUFS", within=1.0)
    second = (f"loudnorm=I={target}:TP={peak}:LRA=11:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
              f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:"
              "linear=true")
    return [second, f"aresample={info.get('rate', 48000)}"]  # loudnorm works at 192 kHz


def _with_audio_filters(args: list[str], filters: list[str]) -> list[str]:
    if not filters:
        return args
    args = list(args)
    if "-ar" in args:  # the format's own rate (8 kHz for AMR) wins over the source's
        filters = [f for f in filters if not f.startswith("aresample=")]
    return ["-af", ",".join(filters), *args]


# ---- fitting in a size: a bitrate from the length, then two passes (x264 and VP9 hit it closely)

TWO_PASS = {"libx264", "libvpx-vp9"}
SIZE_SHARE = 0.96  # of the target, for the container and the encoder's overshoot


def _audio_kbps(total: float) -> int:
    return 128 if total > 1200 else 96 if total > 500 else 64


def _fit_video(args: list[str], info: dict, length: float, dst: Path) -> tuple[list[str], list[str] | None]:
    """(args, first-pass args or None) that keep a video at most target_size MB."""
    target = option("target_size")
    total = target * 8 * 1024 * 1024 * SIZE_SHARE / max(length, 0.1) / 1000  # kbit/s
    audio = _audio_kbps(total) if info.get("audio") else 0
    video = total - audio
    if video < 40:
        raise ConversionError(f"{show_time(length)} of video can't fit in {target} MB: pick a bigger size or cut it shorter")
    args = list(args)
    codec = args[args.index("-c:v") + 1]
    w, h, fps = info.get("width", 1280), info.get("height", 720), info.get("fps", 30.0)
    if option("fps"):
        fps = float(option("fps"))
    # more than this many bits a pixel looks no better: the file just comes out smaller than asked
    cap = w * h * fps * 0.15 / 1000
    if video > cap:
        video = cap
        note(f"It fits in {target} MB at full quality, so it's smaller than that")
    elif not option("video_size") and video * 1000 / (w * h * fps) < 0.03:
        short = max(240, int(math.sqrt(video * 1000 / 0.04 / fps * min(w, h) / max(w, h))) // 2 * 2)
        if short < min(w, h):
            args = _with_filters(args, [_short_side(short)])
            why = f"Scaled down to {short}p so the picture stays clear at this size"
            long_side = round(max(w, h) * short / min(w, h) / 2) * 2
            expect("width", short if w < h else long_side, why, within=2)
            expect("height", long_side if w < h else short, why, within=2)
    for flag in ("-crf", "-q:v", "-b:v"):
        if flag in args:
            i = args.index(flag)
            del args[i:i + 2]
    rate = ["-b:v", f"{int(video)}k", "-maxrate", f"{int(video * 1.5)}k", "-bufsize", f"{int(video * 2)}k"]
    if audio:
        for flag in ("-b:a", "-q:a"):
            if flag in args:
                i = args.index(flag)
                del args[i:i + 2]
        args[args.index("-c:a") + 2:args.index("-c:a") + 2] = ["-b:a", f"{audio}k"]
    i = args.index("-c:v") + 2
    args[i:i] = rate
    expect("file_size", float(target), f"Fit in {target} MB", at_most=True)
    if codec not in TWO_PASS:
        return args, None
    log = str(dst.with_name(dst.stem + "-pass"))
    first = list(args)
    k = first.index("-c:a")
    del first[k:k + 2]
    first = [*first, "-an", "-pass", "1", "-passlogfile", log, "-f", "null"]
    return [*args, "-pass", "2", "-passlogfile", log], first


# MP3 and MP2 have fixed bitrates, and the encoder rounds up to the next one: take the one below
STEPS = {"mp3": (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
         "mp2": (32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384)}


def _fit_audio(args: list[str], length: float, fmt: str) -> list[str]:
    target = option("target_size")
    kbps = int(target * 8 * 1024 * 1024 * SIZE_SHARE / max(length, 0.1) / 1000)
    if kbps < 24:
        raise ConversionError(f"{show_time(length)} of sound can't fit in {target} MB: pick a bigger size or cut it shorter")
    kbps = min(kbps, 320)
    if fmt in STEPS:
        kbps = max((k for k in STEPS[fmt] if k <= kbps), default=STEPS[fmt][0])
    args = list(args)
    for flag in ("-q:a", "-b:a"):
        if flag in args:
            i = args.index(flag)
            del args[i:i + 2]
    args[2:2] = ["-b:a", f"{kbps}k"]
    expect("file_size", float(target), f"Fit in {target} MB", at_most=True)
    return args


# ---- settings (options.py): each replaces what the args above say when it's chosen

LOSSY_AUDIO = {"mp3", "ogg", "opus", "m4a", "aac", "wma", "ac3", "mka", "m4b", "mp2", "m4r"}
# video_quality "smaller", "balanced" (what VIDEO_ARGS use) or "best": per encoder, the value of
# the flag that sets its quality
VIDEO_QUALITY = {"libx264": ("-crf", ("28", "23", "18")), "libvpx-vp9": ("-crf", ("38", "32", "24")),
                 "mpeg4": ("-q:v", ("6", "3", "2")), "mpeg2video": ("-q:v", ("6", "3", "2")),
                 "libtheora": ("-q:v", ("5", "7", "9")), "wmv2": ("-b:v", ("2M", "4M", "8M"))}
QUALITY_LEVELS = ("smaller", "balanced", "best")
GIF_FPS, GIF_WIDTH, WEBP_FPS, WEBP_WIDTH = 12, 640, 15, 800


def _short_side(n: int) -> str:
    """A scale filter fitting the shorter side to n pixels (720 for "720p", portrait or not), never enlarging."""
    return f"scale='if(gte(iw,ih),-2,min({n},iw))':'if(gte(iw,ih),min({n},ih),-2)'"


def _with_filters(args: list[str], filters: list[str]) -> list[str]:
    """args with filters run first (before the -vf the args already have, if any)."""
    if not filters:
        return args
    args = list(args)
    if "-vf" in args:
        i = args.index("-vf") + 1
        args[i] = ",".join([*filters, args[i]])
        return args
    return ["-vf", ",".join(filters), *args]


def _picture_filters() -> list[str]:
    fps, size = option("fps"), option("video_size")
    return ([f"fps={int(fps)}"] if fps else []) + ([_short_side(int(str(size).rstrip("p")))] if size else [])


def video_args(fmt: str) -> list[str]:
    args = list(VIDEO_ARGS[fmt])
    level = option("video_quality")
    codec = args[args.index("-c:v") + 1]
    if level in QUALITY_LEVELS and codec in VIDEO_QUALITY:
        flag, values = VIDEO_QUALITY[codec]
        args[args.index(flag) + 1] = values[QUALITY_LEVELS.index(level)]
    return _with_filters(args, _picture_filters())


def audio_args(fmt: str) -> list[str]:
    args = list(AUDIO_ARGS[fmt])
    rate = option("bitrate")
    if rate and fmt in LOSSY_AUDIO:
        for flag in ("-q:a", "-b:a"):  # a fixed rate in place of the format's own quality
            if flag in args:
                i = args.index(flag)
                del args[i:i + 2]
        args[2:2] = ["-b:a", f"{int(rate)}k"]
    return args


def animation_args(fps: int, width: int, tail: list[str]) -> list[str]:
    size = option("video_size")
    scale = _short_side(int(str(size).rstrip("p"))) if size else f"scale='min({width},iw)':-1"
    return ["-vf", f"fps={int(option('fps', fps))},{scale}:flags=lanczos{tail[0]}", *tail[1:]]


def _gif_args() -> list[str]:
    return animation_args(GIF_FPS, GIF_WIDTH, [",split[a][b];[a]palettegen[p];[b][p]paletteuse", "-loop", "0"])


def _webp_args() -> list[str]:
    return animation_args(WEBP_FPS, WEBP_WIDTH, ["", "-c:v", "libwebp_anim", "-quality", "75", "-loop", "0", "-an"])


def _size_words(opts: dict, width: int) -> str:
    return f"at most {opts['video_size']}" if opts.get("video_size") else f"at most {width} pixels wide"


TRACKS = loss("Only one sound track is kept", "tracks", affects=("audio_tracks",))


def _rate_it_takes(args: list[str], src: Path) -> list[str]:
    """Sound finer than the encoder takes, brought down to what it does (DSD leaves FFmpeg at 352.8 kHz)."""
    top = TOP_RATE.get(args[args.index("-c:a") + 1]) if "-c:a" in args else None
    if not top or probe(src)["rate"] <= top:
        return args
    note(f"Sound brought down to {top / 1000:g} kHz, the finest this format holds")
    return [*args, "-ar", str(top)]


def _audio_notes(src: str, dst: str, opts: dict) -> list:
    out = [TRACKS]
    if dst == "m4r":
        out.append(loss("iPhone ringtones stop at 40 seconds", weight=0, affects=("duration",)))
    if dst == "amr":
        out.append(loss("AMR is phone-call sound: 8 kHz, one channel", weight=0))
    return out


SOUND_SETTINGS = ("start", "end", "loudness")
SIZED_AUDIO = LOSSY_AUDIO - {"m4r", "amr"}  # a ringtone and phone sound have their own fixed size


def _audio_settings(src: str, dst: str) -> tuple:
    return (("bitrate",) if dst in LOSSY_AUDIO else ()) + SOUND_SETTINGS + (("target_size",) if dst in SIZED_AUDIO else ())


@converter(inputs=AUDIO_IN | VIDEO_IN, outputs=AUDIO, engine="ffmpeg", notes=_audio_notes, options=_audio_settings, same=True)
def to_audio(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
    args = _rate_it_takes(audio_args(dst_fmt), src)
    if not (option("start") or option("end") or option("loudness") or option("target_size")):
        ffmpeg(src, dst, ["-vn", *args])
        return
    info = probe(src)
    before, cut = _cut(info, video=False)
    length = _span(info)[1] if cut or before else info["duration"]
    if option("target_size") and dst_fmt in SIZED_AUDIO:
        args = _fit_audio(args, length, dst_fmt)
    if option("loudness"):
        args = _with_audio_filters(args, _loudnorm(src, info, before, cut))
    ffmpeg(src, dst, [*cut, "-vn", *args], before)


def _video_notes(src: str, dst: str, opts: dict) -> list:
    out = [loss("The video is re-encoded: a little quality is lost", weight=0), TRACKS]
    if opts.get("video_size"):
        out.append(loss(f"Scaled down to at most {opts['video_size']}", weight=0, affects=("width", "height")))
    if dst == "ogv":
        out.append(loss(f"OGV pictures are at least {THEORA_MIN} pixels a side: smaller ones are enlarged",
                        weight=0, affects=("width", "height")))
    return out


VIDEO_SETTINGS = ("video_quality", "video_size", "fps")
SIZED_VIDEO = {f for f, a in VIDEO_ARGS.items() if a[a.index("-c:v") + 1] in TWO_PASS}


def _video_settings(src: str, dst: str) -> tuple:
    return VIDEO_SETTINGS + SOUND_SETTINGS + (("target_size",) if dst in SIZED_VIDEO else ())


@converter(inputs=VIDEO_IN, outputs=VIDEO, engine="ffmpeg", notes=_video_notes, options=_video_settings, same=True)
@converter(inputs={"gif"}, outputs=VIDEO, engine="ffmpeg", terminal=True,  # no gif -> video -> "audio"
           notes=_video_notes, options=VIDEO_SETTINGS)
def to_video(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
    args = video_args(dst_fmt)
    if src_fmt != "gif":
        args = _rate_it_takes(args, src)
    if src_fmt == "gif" or not (option("start") or option("end") or option("loudness") or option("target_size")):
        ffmpeg(src, dst, args)
        return
    info = probe(src)
    before, cut = _cut(info, video=True)
    length = _span(info)[1] if cut or before else info["duration"]
    first = None
    if option("target_size") and dst_fmt in SIZED_VIDEO:
        args, first = _fit_video(args, info, length, dst)
    if option("loudness") and info.get("audio"):
        args = _with_audio_filters(args, _loudnorm(src, info, before, cut))
    if first:
        ffmpeg(src, "-", [*cut, *first], before)
    ffmpeg(src, dst, [*cut, *args], before)


@converter(inputs=VIDEO_IN, outputs={"gif"}, engine="ffmpeg", options=("fps", "video_size", "start", "end"),
           notes=lambda s, d, o: [loss(f"GIF animations run at {o.get('fps') or GIF_FPS} frames a second, "
                                       f"{_size_words(o, GIF_WIDTH)}, with 256 colors and no sound", "colors",
                                       weight=0, affects=("width", "height", "frames"))])
def to_gif(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
    before, cut = _cut(probe(src), video=True) if option("start") or option("end") else ([], [])
    ffmpeg(src, dst, [*cut, *_gif_args()], before)


@converter(inputs=VIDEO_IN, outputs={"webp"}, engine="ffmpeg", terminal=True,  # animated: don't chain on
           options=("fps", "video_size", "start", "end"),
           notes=lambda s, d, o: [loss(f"Animated WebP runs at {o.get('fps') or WEBP_FPS} frames a second, "
                                       f"{_size_words(o, WEBP_WIDTH)}, with no sound", weight=0,
                                       affects=("width", "height", "frames"))])
def to_animated_webp(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
    before, cut = _cut(probe(src), video=True) if option("start") or option("end") else ([], [])
    ffmpeg(src, dst, [*cut, *_webp_args()], before)


# ---- joining several files into one (pipeline.join)

def _without(args: list[str], *flags: str) -> list[str]:
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a in flags:
            skip = True
        else:
            out.append(a)
    return out


def join(srcs: list[Path], dst: Path, dst_fmt: str) -> float:
    """Join srcs end to end into dst (a sound or video format). Pictures are fitted into the first
    video's size (bars where the shape differs) at its frame rate; sound is made 48 kHz stereo.
    Returns the length the result should have."""
    infos = [probe(s) for s in srcs]
    video = dst_fmt in VIDEO
    if video and not all("width" in i for i in infos):
        raise ConversionError("Only videos can be joined into a video: one of these is sound only")
    first = next((i for i in infos if "width" in i), {})
    w, h = (first.get("width", 1280) + 1) // 2 * 2, (first.get("height", 720) + 1) // 2 * 2
    fps = float(option("fps") or first.get("fps") or 30.0)
    pictures, sounds, silent = [], [], []  # silent: inputs of silence for the videos without sound
    for n, info in enumerate(infos):
        pictures.append(f"[{n}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
                        f"setsar=1,fps={fps:g},format=yuv420p[v{n}]")
        if info["audio"]:
            sounds.append(f"[{n}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a{n}]")
        else:
            silent += ["-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo:d={max(info['duration'], 0.04):.3f}"]
            sounds.append(f"[{len(srcs) + len(silent) // 4 - 1}:a]aformat=sample_fmts=fltp:channel_layouts=stereo[a{n}]")
    n = len(srcs)
    sound_only = ";".join([*sounds, "".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[a]"])
    afilter = []
    if option("loudness"):
        measured = _loudnorm(list(srcs), {"rate": 48000}, [], [], [*silent, "-filter_complex", sound_only], "a")
        afilter = [f for f in measured if not f.startswith("aresample")]
    tail = f";[j]{','.join(afilter)},aresample=48000[a]" if afilter else ""
    out_a = "[j]" if afilter else "[a]"
    if video:
        size = option("video_size")
        scale = f";[c]{_short_side(int(str(size).rstrip('p')))}[v]" if size else ""
        graph = ";".join([*pictures, *sounds, "".join(f"[v{i}][a{i}]" for i in range(n))
                          + f"concat=n={n}:v=1:a=1{'[c]' if size else '[v]'}{out_a}"]) + scale + tail
        args = _without(video_args(dst_fmt), "-vf")  # a -vf can't follow a filter graph: it's in the graph
        maps = ["-map", "[v]", "-map", "[a]"]
    else:
        graph = ";".join([*sounds, "".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1{out_a}"]) + tail
        args = audio_args(dst_fmt)
        maps = ["-map", "[a]"]
    ffmpeg(list(srcs), dst, [*silent, "-filter_complex", graph, *maps, *args])
    total = sum(i["duration"] for i in infos)
    expect("duration", round(total, 3), f"{n} files joined", within=0.1 + 0.05 * n)
    return total
