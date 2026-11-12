"""Conversions done by the smart tools (smart/): a picture's text read into a text file (OCR), and what's
said in sound or video written down as subtitles or a text file (speech recognition). The tools that
change a picture (background, upscale) are settings of the picture conversions (images.py)."""

from __future__ import annotations

import textwrap
from pathlib import Path

from PIL import Image, ImageOps, ImageSequence

from ..errors import ConversionError
from ..losses import loss
from ..registry import converter, note, option, take
from .images import ICONS, READ, enable_heif
from .media import AUDIO_IN, VIDEO_IN
from .subtitles import PYSUBS2, STYLED_SUBS, TEXT_SUBS

OCR_IN = READ - set(ICONS) - {"xbm", "xpm"}  # pictures with something to read; not icons or 1-bit marks
LINE = 42        # characters a subtitle line holds (the usual limit for reading at a glance)
LINES = 2        # lines a subtitle shows at once
PAUSE = 2.0      # seconds of quiet that start a new paragraph in a transcript


# ---- the text in pictures


@converter(inputs=OCR_IN, outputs={"txt"}, engine="ocr", terminal=True,
           notes=lambda s, d, o: [loss("The text is read from the picture (OCR): in Latin letters (English, French, "
                                       "German, Spanish and 28 more); its layout and fonts aren't kept", weight=0)])
def picture_to_text(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
    from ..smart import ocr
    if src_fmt == "heic":
        enable_heif()
    kept = take(src, src_fmt, "picture") if src_fmt != "tiff" else None  # a TIFF's pages are read from the file
    pages = []
    with kept if kept is not None else Image.open(src) as img:
        frames = ImageSequence.Iterator(img) if src_fmt == "tiff" else [img]  # a scan's pages, each read
        for frame in frames:
            upright = ImageOps.exif_transpose(frame) or frame
            pages.append(ocr.text(upright.convert("RGB")))
    text = "\n\n\f".join(pages).strip()
    if not text:
        raise ConversionError("No text was found in this picture")
    if len(pages) > 1:
        note(f"{len(pages)} pages read")
    dst.write_text(text + "\n", encoding="utf-8")


# ---- what's said


def heard(src: Path):
    """What's said in the recording, and the language it's in (as set, or heard)."""
    from ..smart import speech
    samples = speech.sound(src)
    if len(samples) < speech.RATE // 4:
        raise ConversionError("The sound is too short to write anything down")
    lang, said = speech.transcribe(samples, option("language") or None)
    if not said:
        raise ConversionError("No speech was heard in this recording")
    if not option("language"):
        from ..smart.languages import LANGUAGES
        note(f"Heard as {LANGUAGES.get(lang, lang)}: choose the language if that's wrong")
    return said


def cues(said) -> list[tuple[float, float, str]]:
    """Sentences as subtitles: at most LINES lines of LINE characters on screen at once; a longer sentence
    is shown in turns, each for its share of the time."""
    out = []
    for s in said:
        lines = textwrap.wrap(s.text, LINE) or [s.text]
        groups = [lines[i:i + LINES] for i in range(0, len(lines), LINES)]
        total = sum(len(" ".join(g)) for g in groups)
        t = s.start
        for g in groups:
            text = "\n".join(g)
            end = s.end if g is groups[-1] else t + (s.end - s.start) * len(" ".join(g)) / max(1, total)
            out.append((t, max(end, t + 0.3), text))
            t = end
    return out


def write_subtitles(said, dst: Path, fmt: str) -> None:
    import pysubs2
    subs = pysubs2.SSAFile()
    for start, end, text in cues(said):
        subs.append(pysubs2.SSAEvent(start=round(start * 1000), end=round(end * 1000),
                                     text=text.replace("\n", "\\N")))
    subs.save(str(dst), encoding="utf-8", format_=PYSUBS2[fmt])


def transcript(said) -> str:
    """The sentences as running text: a new paragraph wherever the speaker paused."""
    paras, cur, last_end = [], [], None
    for s in said:
        if cur and last_end is not None and s.start - last_end >= PAUSE:
            paras.append(" ".join(cur))
            cur = []
        cur.append(s.text)
        last_end = s.end
    if cur:
        paras.append(" ".join(cur))
    return "\n\n".join(paras) + "\n"


SPEECH_NOTE = loss("What's said is written down by speech recognition (Whisper): names and rare words can come out "
                   "wrong, and music or several people talking at once are harder to follow", weight=0)


@converter(inputs=AUDIO_IN, outputs=TEXT_SUBS | STYLED_SUBS, engine="speech", options=("language",),
           notes=lambda s, d, o: [SPEECH_NOTE])
@converter(inputs=AUDIO_IN | VIDEO_IN, outputs={"txt"}, engine="speech", options=("language",), terminal=True,
           notes=lambda s, d, o: [SPEECH_NOTE])
def speech_to_text(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
    said = heard(src)
    if dst_fmt == "txt":
        dst.write_text(transcript(said), encoding="utf-8")
    else:
        write_subtitles(said, dst, dst_fmt)
