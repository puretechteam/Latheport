"""Audio and video via FFmpeg: converting, and cutting (Start at / End at), fitting in a size,
evening out loudness, joining several files into one, and effects (turned, cropped, recoloured,
text and a watermark, faster, slower or backwards)."""

import json
import math
import re
import subprocess
from pathlib import Path

from .. import engines, options, overlay
from ..errors import ConversionError
from ..losses import loss
from ..options import area_box, parse_time, show_time
from ..registry import converter, expect, note, option
from .images import (
    WARMTH,
    blur_sigma,
    editor_notes,
    hide_box,
    pixel_grid,
    straighten_scale,
)

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
                          ("format=duration:stream=codec_type,width,height,avg_frame_rate,sample_rate,disposition"
                           ":stream_side_data=rotation"),
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
            turned = next((sd.get("rotation") for sd in st.get("side_data_list") or [] if "rotation" in sd), 0)
            if round(float(turned or 0)) % 180 == 90:  # a phone's upright video: FFmpeg turns the frames
                facts["width"], facts["height"] = facts["height"], facts["width"]
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


# ---- effects: turned, cropped, recoloured, text on top, faster, slower or backwards

QUARTER_FILTERS = {1: "transpose=1", 2: "hflip,vflip", 3: "transpose=2"}


def turn_filters(turn: str) -> list[str]:
    """FFmpeg filters for a Turn setting: mirrored first, then turned clockwise."""
    mirrored, quarters = options.orientation(turn)
    return (["hflip"] if mirrored else []) + ([QUARTER_FILTERS[quarters]] if quarters else [])
COLOUR_FILTERS = {"bw": "hue=s=0", "sepia": "colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131"}
PICTURE_EFFECTS = ("rotate", "straighten", "hides", "crop", "area", "adjust", "colour", "marks", "texts", "watermark",
                   "watermark_at", "watermark_image", "watermark_opacity")
TIME_EFFECTS = ("speed", "pitch", "reverse")
REVERSE_MOST = 600  # seconds: backwards takes the whole thing apart, a piece at a time
SHAPES = {"square": (1, 1), "4:5": (4, 5), "16:9": (16, 9), "9:16": (9, 16)}


def _speed() -> float:
    return float(option("speed") or 1)


def _effects_set() -> bool:
    return any(option(k) for k in PICTURE_EFFECTS + TIME_EFFECTS if k not in ("pitch", "watermark_at", "watermark_opacity"))


def _crop_box(w: int, h: int) -> tuple[int, int, int, int] | None:
    """(x, y, width, height) to keep of a w x h frame: chosen by hand or a shape from the middle,
    even sizes (most encoders need them), or None."""
    area, shape = option("area"), option("crop")
    if area:
        x0, y0, x1, y1 = area_box(area, w, h)
    elif shape:
        sw, sh = SHAPES[shape]
        cw, ch = (round(h * sw / sh), h) if w * sh > h * sw else (w, round(w * sh / sw))
        x0, y0 = (w - cw) // 2, (h - ch) // 2
        x1, y1 = x0 + cw, y0 + ch
    else:
        return None
    cw, ch = max(2, (x1 - x0) // 2 * 2), max(2, (y1 - y0) // 2 * 2)
    return min(x0, w - cw), min(y0, h - ch), cw, ch


def _even(n: float) -> int:
    return max(2, math.ceil(n / 2) * 2)


def hide_filters(hides: list[dict], w: int, h: int) -> list:
    """Filters that hide parts of a w x h frame: a covered part is a filled box; a blurred or pixelated
    one is a step _graph splits off, changes and lays back ({"part": (x, y, w, h), "do": filters})."""
    out: list = []
    for hide in hides:
        x, y, pw, ph = hide_box(hide, w, h, even=True)
        if hide["how"] == "cover":
            out.append(f"drawbox=x={x}:y={y}:w={pw}:h={ph}:color=black:t=fill")
        elif hide["how"] == "pixelate":
            cols, rows = pixel_grid(pw, ph, hide["strength"])
            out.append({"part": (x, y, pw, ph), "do": f"scale={cols}:{rows}:flags=area,scale={pw}:{ph}:flags=neighbor"})
        else:
            out.append({"part": (x, y, pw, ph), "do": f"gblur=sigma={blur_sigma(pw, ph, hide['strength']):.2f}"})
    return out


def adjust_filters(adjust: dict[str, int]) -> list[str]:
    """FFmpeg filters for Adjust, made to match the picture's (images.adjusted)."""
    b, c, sat, warm, sharp = (adjust[k] for k in options.ADJUSTMENTS)
    eq = [f"contrast={2 ** (c / 100):.4f}"] if c else []
    eq += [f"gamma={2 ** (b / 100):.4f}"] if b else []
    eq += [f"saturation={1 + sat / 100:.4f}"] if sat else []
    out = ["eq=" + ":".join(eq)] if eq else []
    if warm:
        out.append(f"colorchannelmixer=rr={1 + WARMTH * warm / 100:.4f}:bb={1 - WARMTH * warm / 100:.4f}")
    if sharp:
        out.append(f"unsharp=5:5:{2 * sharp / 100:.2f}:5:5:0")
    return out


def shape_filters(opts: dict, w: int, h: int) -> tuple[list, int, int, list[str]]:
    """(filters, the frame's width and height after them, why its size changed) for what moves the picture's
    pixels about, before any crop: turned, straightened, parts hidden."""
    turn, angle, hides = opts.get("rotate"), opts.get("straighten"), opts.get("hides")
    filters: list = []
    why = []
    if turn:
        filters += turn_filters(turn)
        if options.sideways(turn):
            w, h = h, w
            why.append(options.turn_words(turn))
    if angle:  # drawn bigger, turned and cut back to the frame: no empty corner
        scale = straighten_scale((w, h), float(angle))
        filters += [f"scale={_even(w * scale)}:{_even(h * scale)}", f"rotate={float(angle):g}*PI/180:ow={w}:oh={h}:c=black"]
    if hides:
        filters += hide_filters(options.items(hides), w, h)
    return filters, w, h, why


def tone_filters(opts: dict) -> list[str]:
    """Filters for what changes each pixel's colour: adjusted, black and white or sepia."""
    out = adjust_filters(options.adjustments(opts["adjust"])) if opts.get("adjust") else []
    return out + ([COLOUR_FILTERS[opts["colour"]]] if opts.get("colour") else [])


def look_filters(opts: dict, w: int, h: int) -> tuple[list, int, int]:
    """Every filter of the look on a w x h frame, with no crop and nothing laid over: what the editor shows
    under its tools for a paused video. (filters, width, height after them)"""
    filters, w, h, _ = shape_filters(opts, w, h)
    return filters + tone_filters(opts), w, h


def picture_effects(info: dict, work: Path, sized_later: bool) -> tuple[list, str, dict]:
    """(filters for the start of the picture's chain, a PNG to lay over it or "", info with the
    frame's new size). sized_later: something scales the frames afterwards (Video size, a GIF's
    width), so the size isn't checked here."""
    if "width" not in info:
        return [], "", info
    opts = {k: option(k) for k in PICTURE_EFFECTS}
    boxes, mark, marks, logo = opts["texts"], opts["watermark"], opts["marks"], opts["watermark_image"]
    filters, w, h, why = shape_filters(opts, info["width"], info["height"])
    full = (w, h)  # drawings are placed on the whole frame, then cut as it is
    box = _crop_box(w, h)
    if box:
        x, y, w, h = box
        filters.append(f"crop={w}:{h}:{x}:{y}")
        why.append("Cropped by hand" if option("area") else f"Cropped to {option('crop')}")
    filters += tone_filters(opts)  # each pixel on its own: the same before or after the crop
    if why and not sized_later:
        expect("width", w, ", ".join(why))
        expect("height", h, ", ".join(why))
    png = ""
    if boxes or marks or mark or logo:  # drawn once at the frame's size, laid over every frame
        from PIL import Image
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        if marks:
            drawn = overlay.marks_layer(full, options.items(marks))
            if box:
                drawn = drawn.crop((box[0], box[1], box[0] + w, box[1] + h))
            layer = Image.alpha_composite(layer, drawn)
        if boxes:
            layer = Image.alpha_composite(layer, overlay.text_layer((w, h), options.boxes(boxes)))
        if mark or logo:
            layer = Image.alpha_composite(layer, overlay.watermark_layer(
                (w, h), mark or "", option("watermark_at") or "bottom-right", False, int(option("watermark_opacity") or 60),
                overlay.watermark_picture(logo) if logo else None))
        png = str(work / "latheport-overlay.png")
        layer.save(png)
    return filters, png, dict(info, width=w, height=h)


def _filter_path(path: str) -> str:
    """A file's path as a filter's option takes it, escaped for both levels FFmpeg reads a filter graph at: the
    option's value (backslash, quote, colon), then the graph's (those and [ ] , ;). Quotes can't hold a quote at either level, and these
    paths may have one: the temp folder holds the user's name ("Bob's files")."""
    value = "".join("\\" + c if c in "\\':" else c for c in path.replace("\\", "/"))
    return "".join("\\" + c if c in "\\'[],;" else c for c in value)


def _graph(filters: list, png: str, rest: str) -> str:
    """One filter graph for -vf: the filters in order (a part to blur or pixelate is split off, changed
    and laid back where it was), the PNG laid over after them, then rest (sizes, a GIF's colours...)."""
    parts, chain, at, n = [], [], "", 0

    def close(label: str) -> None:
        nonlocal at
        parts.append(f"{at}{','.join(chain) or 'null'}[{label}]")
        chain.clear()
        at = f"[{label}]"
    for f in filters:
        if isinstance(f, str):
            chain.append(f)
            continue
        n += 1
        close(f"hd{n}")
        x, y, w, h = f["part"]
        parts += [f"[hd{n}]split[hd{n}a][hd{n}b]", f"[hd{n}b]crop={w}:{h}:{x}:{y},{f['do']}[hd{n}c]",
                  f"[hd{n}a][hd{n}c]overlay={x}:{y}[hd{n}d]"]
        at = f"[hd{n}d]"
    if png:
        close("base")
        return ";".join([f"movie={_filter_path(png)}[ov]", *parts, "[base][ov]overlay=0:0" + (f",{rest}" if rest else "")])
    tail = [*chain, *([rest] if rest else [])]
    return ";".join([*parts, f"{at}{','.join(tail) or 'null'}"])


def with_effects(args: list[str], filters: list, png: str) -> list[str]:
    """args with the effect filters first in the picture's chain, the PNG laid over after them, and
    the speed, so everything else (sizes, frame rates, a GIF's colours) works on the result."""
    speed = _speed()
    if speed != 1:
        filters = [*filters, f"setpts=PTS/{speed:g}"]
    if not filters and not png:
        return args
    args = list(args)
    rest = args[args.index("-vf") + 1] if "-vf" in args else ""
    chain = _graph(filters, png, rest)
    if "-vf" in args:
        args[args.index("-vf") + 1] = chain
        return args
    return ["-vf", chain, *args]


def tempo(speed: float, rate: int) -> list[str]:
    """Sound filters for speed: atempo (voices sound the same; it takes 0.5 to 2 a step) or, with
    Pitch "change", played faster or slower like a tape."""
    if speed == 1:
        return []
    if option("pitch") == "change":
        return [f"asetrate={round(rate * speed)}", f"aresample={rate}"]
    steps, left = [], speed
    while left > 2 or left < 0.5:
        step = 2.0 if left > 2 else 0.5
        steps.append(step)
        left /= step
    return [f"atempo={x:g}" for x in [*steps, left]]


def with_sound_effects(args: list[str], rate: int) -> list[str]:
    """args with the speed's sound filters first (before a loudness filter already there)."""
    filters = tempo(_speed(), rate)
    if not filters:
        return args
    args = list(args)
    if "-ar" in args:  # the format's own rate wins
        filters = [f for f in filters if not f.startswith("aresample=")]
    if "-af" in args:
        i = args.index("-af") + 1
        args[i] = ",".join([*filters, args[i]])
        return args
    return ["-af", ",".join(filters), *args]


def timed(info: dict, before: list[str], cut: list[str]) -> tuple[list[str], list[str], float]:
    """(input options, output options, length) with the speed counted in: a cut's length goes on the
    input side (it's of the original), and the result is checked to be that long divided by the speed."""
    length = _span(info)[1] if cut or before else info["duration"]
    speed = _speed()
    if speed == 1:
        return before, cut, length
    before, cut = [*before, *cut], []
    length = length / speed
    expect("duration", round(length, 3), f"Played at {speed:g}x", within=max(0.15, 0.01 * length))
    return before, cut, length


def reversed_copy(src: Path, info: dict, before: list[str], cut: list[str], work: Path) -> tuple[Path, dict]:
    """src (as cut) backwards, as an almost lossless file in work, and what it is. Video is turned
    round a few seconds at a time (all of it at once would take gigabytes of memory) and the pieces
    joined in reverse order."""
    start = parse_time(option("start")) if option("start") else 0.0
    length = _span(info)[1] if cut or before else info["duration"]
    if length > REVERSE_MOST:
        raise ConversionError(f"Backwards takes at most {REVERSE_MOST // 60} minutes ({show_time(length)} here): cut it shorter first")
    sound = ["-af", "areverse", "-c:a", "pcm_s16le"] if info.get("audio") else ["-an"]
    if "width" not in info:  # sound only: one piece
        out = work / "latheport-backwards.wav"
        # -ss and -t on the input: reversing reads to the end of what it's given, then turns it round
        ffmpeg(src, out, ["-vn", *sound], ["-ss", f"{start:.3f}", "-t", f"{length:.3f}"])
        return out, probe(out)
    fps = info.get("fps", 30.0)
    # ~400 MB of frames a piece, at most 10 s; each piece an exact number of frames (and as much sound),
    # so the pieces meet without a frame twice or a gap
    each = max(15, min(int(10 * fps), int(4e8 / (info["width"] * info["height"] * 1.5))))
    total = max(1, round(length * fps))
    pieces = []
    for first in range(0, total, each):
        count = min(each, total - first)
        out = work / f"latheport-back{len(pieces):04d}.mkv"
        sound = (["-af", f"atrim=end={count / fps:.6f},areverse", "-c:a", "pcm_s16le"] if info.get("audio")
                 else ["-an"])
        ffmpeg(src, out, ["-vf", f"trim=end_frame={count},reverse", *sound, "-c:v", "libx264", "-preset", "veryfast",
                          "-crf", "14", "-pix_fmt", "yuv420p"],
               ["-ss", f"{start + first / fps:.6f}", "-t", f"{(count + 2) / fps:.6f}"])
        pieces.append(out)
    listing = work / "latheport-back.txt"
    listing.write_text("".join(f"file '{q.name}'\n" for q in reversed(pieces)), encoding="utf-8")
    out = work / "latheport-backwards.mkv"
    ffmpeg(listing, out, ["-c", "copy"], ["-f", "concat", "-safe", "0"])
    for q in pieces:
        q.unlink(missing_ok=True)
    return out, probe(out)


def effect_notes(opts: dict, video: bool, sound: bool = True) -> list:
    out = []
    if video:
        if opts.get("rotate"):
            out.append(loss(options.turn_words(opts["rotate"]), weight=0,
                            affects=("width", "height") if options.sideways(opts["rotate"]) else ()))
        if opts.get("area") or opts.get("crop"):
            shape = "a square" if opts.get("crop") == "square" else opts.get("crop")
            out.append(loss("Cropped by hand" if opts.get("area") else f"Cropped to {shape} from the middle",
                            weight=0, affects=("width", "height")))
        if opts.get("colour"):
            out.append(loss("Made black and white" if opts["colour"] == "bw" else "Made sepia, like an old film", weight=0))
        out += editor_notes(opts)
    if opts.get("speed"):
        kept = "voices sound the same" if opts.get("pitch") != "change" else "higher or lower, like a tape"
        out.append(loss(f"Played at {opts['speed']}x" + (f" ({kept})" if sound else ""), weight=0, affects=("duration",)))
    if opts.get("reverse"):
        out.append(loss("Played backwards", weight=0))
    return out


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
    return out + effect_notes(opts, video=False)


SOUND_SETTINGS = ("start", "end", "loudness")
SIZED_AUDIO = LOSSY_AUDIO - {"m4r", "amr"}  # a ringtone and phone sound have their own fixed size


def _audio_settings(src: str, dst: str) -> tuple:
    return ((("bitrate",) if dst in LOSSY_AUDIO else ()) + SOUND_SETTINGS + (("target_size",) if dst in SIZED_AUDIO else ())
            + TIME_EFFECTS)


@converter(inputs=AUDIO_IN | VIDEO_IN, outputs=AUDIO - {"mka"}, engine="ffmpeg", notes=_audio_notes, options=_audio_settings,
           same=True)
# sound made into MKA goes no further: an MKA made from an MP3 has no subtitle track to take out
@converter(inputs=AUDIO_IN | VIDEO_IN, outputs={"mka"}, engine="ffmpeg", notes=_audio_notes, options=_audio_settings,
           same=True, terminal=True)
def to_audio(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
    args = _rate_it_takes(audio_args(dst_fmt), src)
    if not (option("start") or option("end") or option("loudness") or option("target_size") or option("speed")
            or option("reverse")):
        ffmpeg(src, dst, ["-vn", *args])
        return
    info = probe(src)
    before, cut = _cut(info, video=False)
    if option("reverse"):
        sound = {k: v for k, v in info.items() if k not in ("width", "height")}  # the sound only
        src, info = reversed_copy(src, sound, before, cut, dst.parent)
        before, cut = [], []
    before, cut, length = timed(info, before, cut)
    if option("target_size") and dst_fmt in SIZED_AUDIO:
        args = _fit_audio(args, length, dst_fmt)
    if option("loudness"):
        args = _with_audio_filters(args, _loudnorm(src, info, before, cut))
    args = with_sound_effects(args, info.get("rate", 48000))
    ffmpeg(src, dst, [*cut, "-vn", *args], before)


def _video_notes(src: str, dst: str, opts: dict) -> list:
    out = [loss("The video is re-encoded: a little quality is lost", weight=0), TRACKS]
    if opts.get("speech_subs"):
        out.append(loss("What's said is written down by speech recognition (Whisper) and added "
                        + ("into the picture" if opts["speech_subs"] == "burn" else "as a subtitle track")
                        + ": names and rare words can come out wrong", weight=0))
    if opts.get("video_size"):
        out.append(loss(f"Scaled down to at most {opts['video_size']}", weight=0, affects=("width", "height")))
    if dst == "ogv":
        out.append(loss(f"OGV pictures are at least {THEORA_MIN} pixels a side: smaller ones are enlarged",
                        weight=0, affects=("width", "height")))
    return out + effect_notes(opts, video=True)


VIDEO_SETTINGS = ("video_quality", "video_size", "fps")
SIZED_VIDEO = {f for f, a in VIDEO_ARGS.items() if a[a.index("-c:v") + 1] in TWO_PASS}


def _video_settings(src: str, dst: str) -> tuple:
    return (VIDEO_SETTINGS + SOUND_SETTINGS + (("target_size",) if dst in SIZED_VIDEO else ()) + PICTURE_EFFECTS
            + TIME_EFFECTS + ("speech_subs", "language"))


SUB_TRACK = {"mp4": "mov_text", "m4v": "mov_text", "mov": "mov_text", "3gp": "mov_text", "mkv": "srt",
             "webm": "webvtt"}  # the subtitle track each video format holds
SUB_SIZE = 0.055   # burned subtitles' height, a share of the picture's
SUB_FONT = "Inter"  # the semibold Inter Latheport ships (overlay.FONT_DIR), named plain "Inter" inside


def speech_subtitles(src: Path, info: dict, work: Path, dst_fmt: str) -> tuple[str, Path] | None:
    """What's said in src written down for the video being made, timed to it (its cut and speed): ("burn", an
    ASS file styled for the picture) or ("track", an SRT file); None when Subtitles of what's said is off."""
    how = option("speech_subs")
    if not how:
        return None
    if how == "track" and dst_fmt not in SUB_TRACK:
        raise ConversionError(f"{dst_fmt.upper()} can't hold a subtitle track: choose Burned into the picture, "
                              "or make an MP4, MKV, MOV or WebM")
    if option("reverse"):
        raise ConversionError("Speech played backwards can't be written down: turn Backwards off, or the subtitles")
    if not info.get("audio"):
        raise ConversionError(f"{src.name} has no sound to write down")
    import pysubs2

    from ..smart import need
    from .smart import cues, heard
    need("speech")
    start, length = _span(info) if (option("start") or option("end")) else (0.0, info["duration"] or 1e9)
    speed = _speed()
    subs = pysubs2.SSAFile()
    for a, b, text in cues(heard(src)):
        a, b = max(a, start), min(b, start + length)
        if b - a > 0.05:
            subs.append(pysubs2.SSAEvent(start=round((a - start) / speed * 1000), end=round((b - start) / speed * 1000),
                                         text=text.replace("\n", "\\N")))
    if not subs:
        note("Nothing is said in the part kept: no subtitles added")
        return None
    note(f"{len(subs)} subtitle{'s' if len(subs) != 1 else ''} of what's said "
         + ("burned in" if how == "burn" else "added as a track"))
    if how == "track":
        path = work / "speech.srt"
        subs.save(str(path), encoding="utf-8", format_="srt")
        return how, path
    w, h = info["width"], info["height"]
    subs.info.update(PlayResX=str(w), PlayResY=str(h), ScaledBorderAndShadow="yes")
    style = subs.styles["Default"]
    style.fontname, style.fontsize = SUB_FONT, max(8, round(h * SUB_SIZE))
    style.primarycolor, style.outlinecolor = pysubs2.Color(255, 255, 255), pysubs2.Color(0, 0, 0)
    style.outline, style.shadow = max(1.0, h / 300), 0
    style.marginv, style.marginl, style.marginr = round(h * 0.05), round(w * 0.05), round(w * 0.05)
    style.alignment = pysubs2.Alignment.BOTTOM_CENTER
    path = work / "speech.ass"
    subs.save(str(path), encoding="utf-8", format_="ass")
    return how, path


def with_subtitles(args: list[str], subs: tuple[str, Path] | None, dst_fmt: str) -> list[str]:
    """args with the subtitles burned in last in the picture's chain (on the finished frame), or mapped in
    as a track from the second input."""
    if not subs:
        return args
    how, path = subs
    args = list(args)
    if how == "burn":
        burn = f"subtitles={_filter_path(str(path))}:fontsdir={_filter_path(str(overlay.FONT_DIR))}"
        if "-vf" in args:
            args[args.index("-vf") + 1] += f",{burn}"
        else:
            args = ["-vf", burn, *args]
        return args
    return ["-map", "0:v:0", "-map", "0:a?", "-map", "1:0", *args, "-c:s", SUB_TRACK[dst_fmt]]


def _gif_video_settings(src: str, dst: str) -> tuple:
    return VIDEO_SETTINGS + PICTURE_EFFECTS + ("speed",)


def _animation_settings(src: str, dst: str) -> tuple:
    return ("fps", "video_size", "start", "end", *PICTURE_EFFECTS, "speed", "reverse")


@converter(inputs=VIDEO_IN, outputs=VIDEO, engine="ffmpeg", notes=_video_notes, options=_video_settings, same=True)
@converter(inputs={"gif"}, outputs=VIDEO, engine="ffmpeg", terminal=True,  # no gif -> video -> "audio"
           notes=_video_notes, options=_gif_video_settings)
def to_video(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
    args = video_args(dst_fmt)
    if src_fmt != "gif":
        args = _rate_it_takes(args, src)
    if src_fmt == "gif":
        if _effects_set():
            info = probe(src)
            filters, png, info = picture_effects(info, dst.parent, sized_later=bool(option("video_size")))
            args = with_effects(args, filters, png)
            timed(info, [], [])
        ffmpeg(src, dst, args)
        return
    if not (option("start") or option("end") or option("loudness") or option("target_size") or _effects_set()
            or option("speech_subs")):
        ffmpeg(src, dst, args)
        return
    info = probe(src)
    before, cut = _cut(info, video=True)
    if option("reverse"):
        src, info = reversed_copy(src, info, before, cut, dst.parent)
        before, cut = [], []
    before, cut, length = timed(info, before, cut)
    filters, png, info = picture_effects(info, dst.parent, sized_later=bool(option("video_size")))
    # heard on the original, timed to the result, styled for the picture as the effects leave it
    subs = speech_subtitles(src, info, dst.parent, dst_fmt)
    first = None
    if option("target_size") and dst_fmt in SIZED_VIDEO:
        args, first = _fit_video(args, info, length, dst)
    if option("loudness") and info.get("audio"):
        args = _with_audio_filters(args, _loudnorm(src, info, before, cut))
    args = with_effects(args, filters, png)
    if info.get("audio"):
        args = with_sound_effects(args, info.get("rate", 48000))
    if first:
        ffmpeg(src, "-", [*cut, *with_effects(first, filters, png)], before)
    track = subs is not None and subs[0] == "track"
    ffmpeg([src, subs[1]] if track else src, dst, [*cut, *with_subtitles(args, subs, dst_fmt)], before)


@converter(inputs=VIDEO_IN, outputs={"gif"}, engine="ffmpeg", options=_animation_settings,
           notes=lambda s, d, o: [loss(f"GIF animations run at {o.get('fps') or GIF_FPS} frames a second, "
                                       f"{_size_words(o, GIF_WIDTH)}, with 256 colors and no sound", "colors",
                                       weight=0, affects=("width", "height", "frames"))] + effect_notes(o, True, sound=False))
def to_gif(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
    _animation(src, dst, _gif_args())


def _animation(src: Path, dst: Path, args: list[str]) -> None:
    """A video into an animated GIF or WebP, cut and with the effects asked for."""
    if not (option("start") or option("end") or _effects_set()):
        ffmpeg(src, dst, args)
        return
    info = probe(src)
    before, cut = _cut(info, video=True)
    if option("reverse"):
        src, info = reversed_copy(src, info, before, cut, dst.parent)
        before, cut = [], []
    before, cut, _ = timed(info, before, cut)
    filters, png, info = picture_effects(info, dst.parent, sized_later=True)
    ffmpeg(src, dst, [*cut, *with_effects(args, filters, png)], before)


@converter(inputs=VIDEO_IN, outputs={"webp"}, engine="ffmpeg", terminal=True,  # animated: don't chain on
           options=_animation_settings,
           notes=lambda s, d, o: [loss(f"Animated WebP runs at {o.get('fps') or WEBP_FPS} frames a second, "
                                       f"{_size_words(o, WEBP_WIDTH)}, with no sound", weight=0,
                                       affects=("width", "height", "frames"))] + effect_notes(o, True, sound=False))
def to_animated_webp(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
    _animation(src, dst, _webp_args())


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
