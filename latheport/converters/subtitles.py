"""Subtitles: SRT <-> WebVTT built in; ASS/SSA, MicroDVD and TTML via pysubs2, with our own YouTube
SBV and SAMI writers; subtitle tracks pulled out of videos via FFmpeg."""

import re
from pathlib import Path

from ..errors import ConversionError
from ..losses import loss
from ..registry import converter, option
from ..textfile import read_text
from .media import VIDEO_IN, ffmpeg

TEXT_SUBS = {"srt", "vtt"}
STYLED_SUBS = {"ass", "ssa"}
OTHER_SUBS = {"sbv", "sub", "ttml", "smi"}
ALL_SUBS = TEXT_SUBS | STYLED_SUBS | OTHER_SUBS
PYSUBS2 = {"srt": "srt", "vtt": "vtt", "ass": "ass", "ssa": "ssa", "sub": "microdvd", "ttml": "ttml",
           "smi": "sami"}
MICRODVD_FPS = 23.976  # .sub counts frames; files that don't say their frame rate are usually film

_TIMING = re.compile(r"(?P<a>[\d:.,]+)\s*-->\s*(?P<b>[\d:.,]+)")
_VTT_TAG = re.compile(r"</?(?:c|v|lang|ruby|rt)(?:[.\s][^>]*)?>|<\d[\d:.]*>")  # keeps <i> <b> <u>


def _blocks(text: str) -> list[list[str]]:
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    return [b.split("\n") for b in re.split(r"\n\s*\n", text.strip()) if b.strip()]


def _stamp(t: str, sep: str) -> str:
    """'1:02.5' / '00:01:02,500' -> '00:01:02<sep>500'."""
    parts = t.replace(",", ".").split(":")
    secs = float(parts[-1])
    mins = int(parts[-2]) if len(parts) > 1 else 0
    hours = int(parts[-3]) if len(parts) > 2 else 0
    ms = round(secs * 1000)
    return f"{hours:02}:{mins:02}:{ms // 1000:02}{sep}{ms % 1000:03}"


def _cues(src: Path) -> list[tuple[str, str, list[str]]]:
    cues = []
    for block in _blocks(read_text(src)):
        idx = next((i for i, line in enumerate(block) if "-->" in line), None)
        if idx is None:  # WEBVTT header, NOTE, STYLE, REGION, or junk
            continue
        m = _TIMING.search(block[idx])
        if m:
            cues.append((m["a"], m["b"], block[idx + 1:]))
    if not cues:
        raise ConversionError("No subtitles found in this file")
    return cues


@converter(inputs=TEXT_SUBS, outputs=TEXT_SUBS)
def srt_vtt(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
    out = []
    for n, (a, b, lines) in enumerate(_cues(src), 1):
        if dst_fmt == "vtt":
            out.append(f"{_stamp(a, '.')} --> {_stamp(b, '.')}\n" + "\n".join(lines))
        else:
            lines = [_VTT_TAG.sub("", line) for line in lines]
            out.append(f"{n}\n{_stamp(a, ',')} --> {_stamp(b, ',')}\n" + "\n".join(lines))
    head = "WEBVTT\n\n" if dst_fmt == "vtt" else ""
    dst.write_text(head + "\n\n".join(out) + "\n", encoding="utf-8")


# ---- YouTube's .sbv: "0:00:01.000,0:00:02.500", then the text; blocks split by blank lines

def _sbv_time(t: str) -> int:
    h, m, sec = t.strip().split(":")
    return round((int(h) * 3600 + int(m) * 60 + float(sec)) * 1000)


def _read_sbv(src: Path):
    import pysubs2
    subs = pysubs2.SSAFile()
    for block in _blocks(read_text(src)):
        try:
            a, b = block[0].split(",")
            start, end = _sbv_time(a), _sbv_time(b)
        except ValueError:
            continue
        subs.append(pysubs2.SSAEvent(start=start, end=end, text="\\N".join(block[1:])))
    return subs


def _write_sbv(subs, dst: Path) -> None:
    def stamp(ms: int) -> str:
        return f"{ms // 3600000}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02}.{ms % 1000:03}"
    blocks = [f"{stamp(e.start)},{stamp(e.end)}\n{e.plaintext}" for e in subs if not e.is_comment]
    dst.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def _write_sami(subs, dst: Path) -> None:
    """Microsoft SAMI (.smi): each line shown at its <SYNC Start>, cleared by an empty one at its end."""
    import html
    out = ["<SAMI>", "<HEAD>", "<TITLE></TITLE>", '<STYLE TYPE="text/css"><!--',
           "P { margin-left: 8pt; margin-right: 8pt; text-align: center; font-family: sans-serif; }",
           ".ENCC { Name: English; lang: en-US; }", "--></STYLE>", "</HEAD>", "<BODY>"]
    events = sorted((e for e in subs if not e.is_comment), key=lambda e: e.start)
    for i, e in enumerate(events):
        out.append(f"<SYNC Start={e.start}><P Class=ENCC>" + "<br>".join(html.escape(s) for s in e.plaintext.split("\n")))
        if i + 1 == len(events) or events[i + 1].start > e.end:
            out.append(f"<SYNC Start={e.end}><P Class=ENCC>&nbsp;")
    out += ["</BODY>", "</SAMI>"]
    dst.write_text("\n".join(out) + "\n", encoding="utf-8")


def _load(src: Path, fmt: str):
    import pysubs2
    if fmt == "sbv":
        return _read_sbv(src)
    if fmt == "sub" and b"\x00" in src.read_bytes()[:16]:
        raise ConversionError("This .sub holds DVD subtitle pictures (VobSub), not text")
    text = read_text(src)  # UTF-8, Notepad's UTF-16, or the old Windows code page
    try:
        try:
            return pysubs2.SSAFile.from_string(text, format_=PYSUBS2[fmt])
        except pysubs2.exceptions.UnknownFPSError:  # MicroDVD without a frame-rate line
            return pysubs2.SSAFile.from_string(text, format_=PYSUBS2[fmt], fps=MICRODVD_FPS)
    except pysubs2.exceptions.Pysubs2Error as exc:
        raise ConversionError(f"Couldn't read these subtitles: {exc}") from None


def _frame_notes(src: str, dst: str, opts: dict) -> list:
    if "sub" in (src, dst):
        return [loss(f"SUB counts frames: times assume {MICRODVD_FPS} frames a second unless the file says", weight=0)]
    return []


@converter(inputs=ALL_SUBS, outputs=STYLED_SUBS | OTHER_SUBS, notes=_frame_notes)
@converter(inputs=STYLED_SUBS | OTHER_SUBS, outputs=TEXT_SUBS, notes=_frame_notes)
def pysubs2_convert(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
    subs = _load(src, src_fmt)
    if not [e for e in subs if not e.is_comment]:
        raise ConversionError("No subtitles found in this file")
    if dst_fmt == "sbv":
        return _write_sbv(subs, dst)
    if dst_fmt == "smi":  # pysubs2 reads SAMI but doesn't write it
        return _write_sami(subs, dst)
    subs.save(str(dst), encoding="utf-8", format_=PYSUBS2[dst_fmt], fps=MICRODVD_FPS)


def _video_subs_notes(src: str, dst: str, opts: dict) -> list:
    if opts.get("subtitles_from") == "speech":
        from .smart import SPEECH_NOTE
        return [SPEECH_NOTE]
    return [loss("Only the first subtitle track is taken", "subtitle_tracks", affects=("subtitle_tracks",))]


@converter(inputs=VIDEO_IN | {"mka"}, outputs=TEXT_SUBS | STYLED_SUBS, engine="ffmpeg", notes=_video_subs_notes,
           options=("subtitles_from", "language"))
def video_subs(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
    """The video's first subtitle track, or what's said in it written down."""
    if option("subtitles_from") == "speech":
        from ..smart import need
        from .smart import heard, write_subtitles
        need("speech")
        return write_subtitles(heard(src), dst, dst_fmt)
    try:
        ffmpeg(src, dst, ["-map", "0:s:0"])
    except ConversionError as exc:
        if "no subtitle track" in str(exc) or "matches no streams" in str(exc):
            raise ConversionError("This video has no subtitle track. To write down what's said instead, "
                                  "set Subtitles from to What's said (--subtitles-from speech)") from None
        raise
