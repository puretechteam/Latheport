"""What's said in a recording, written down with timings: OpenAI's Whisper (base), run with ONNX Runtime.

The sound is read by FFmpeg as 16 kHz mono, made into a log-mel spectrogram (80 bands, 10 ms steps) and
read 30 seconds at a time: the encoder hears the window, the decoder writes the words one token at a time,
with Whisper's own timestamp tokens marking where each sentence starts and ends. The next window starts
where the last whole sentence ended. A window that comes out repeating itself or unsure is tried again a
little less greedily (Whisper's temperature fallback, seeded, so the same file always gives the same text),
and one that's silent is skipped."""

from __future__ import annotations

import json
import os
import zlib
from dataclasses import dataclass

import numpy as np

from . import need, session
from .languages import LANGUAGES

RATE = 16000
N_FFT, HOP, BANDS = 400, 160, 80
WINDOW = 3000         # mel frames in 30 seconds
SAMPLE_LEN = 224      # most tokens the decoder writes in one window
LAYERS, HEADS, HEAD_DIM = 6, 8, 64
EOT, SOT, TRANSLATE, TRANSCRIBE = 50257, 50258, 50358, 50359
NO_SPEECH, NO_TIMESTAMPS, TIME_0 = 50362, 50363, 50364  # TIME_0: <|0.00|>, then a token every 0.02 s
FIRST_TIME_MAX = 50   # the first timestamp is at most 1 s into the window
TEMPERATURES = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
REPEATS = 2.4         # text that zlib squeezes more than this is repeating itself
UNSURE = -1.0         # average log probability below which a window is tried again
SILENT = 0.6          # Whisper's own "no speech" probability past which an unsure window is skipped
# tokens Whisper never writes (symbols that aren't speech), from the model's generation_config.json
SUPPRESS = (1, 2, 7, 8, 9, 10, 14, 25, 26, 27, 28, 29, 31, 58, 59, 60, 61, 62, 63, 90, 91, 92, 93, 359, 503, 522,
            542, 873, 893, 902, 918, 922, 931, 1350, 1853, 1982, 2460, 2627, 3246, 3253, 3268, 3536, 3846, 3961,
            4183, 4667, 6585, 6647, 7273, 9061, 9383, 10428, 10929, 11938, 12033, 12331, 12562, 13793, 14157,
            14635, 15265, 15618, 16553, 16604, 18362, 18956, 20075, 21675, 22520, 26130, 26161, 26435, 28279,
            29464, 31650, 32302, 32470, 36865, 42863, 47425, 49870, 50254, 50258, 50358, 50359, 50360, 50361, 50362)
LANGUAGE_TOKEN = {code: EOT + 2 + i for i, code in enumerate(LANGUAGES)}


@dataclass
class Said:
    start: float   # seconds
    end: float
    text: str


# ---- sound to the spectrogram Whisper hears


def _mel_filters() -> np.ndarray:
    """80 triangular bands on the Slaney mel scale from 0 to 8 kHz, each scaled to the same area."""
    def to_mel(f):
        f = np.asarray(f, np.float64)
        lin = f / (200 / 3)
        return np.where(f >= 1000, 15 + np.log(np.maximum(f, 1e-9) / 1000) / (np.log(6.4) / 27), lin)

    def to_hz(m):
        m = np.asarray(m, np.float64)
        return np.where(m >= 15, 1000 * np.exp((np.log(6.4) / 27) * (m - 15)), m * (200 / 3))

    freqs = np.linspace(0, RATE / 2, N_FFT // 2 + 1)
    edges = to_hz(np.linspace(to_mel(0), to_mel(RATE / 2), BANDS + 2))
    gaps = np.diff(edges)
    ramps = edges[:, None] - freqs[None, :]
    lower = -ramps[:-2] / gaps[:-1, None]
    upper = ramps[2:] / gaps[1:, None]
    weights = np.maximum(0, np.minimum(lower, upper))
    return (weights * (2.0 / (edges[2:] - edges[:-2]))[:, None]).astype(np.float32)


def spectrogram(samples: np.ndarray) -> np.ndarray:
    """Whisper's log-mel spectrogram of 16 kHz samples (80 x frames, 100 frames a second), with 30 s of
    silence after the end, as Whisper reads it."""
    audio = np.concatenate([samples.astype(np.float32), np.zeros(RATE * 30, np.float32)])
    padded = np.pad(audio, (N_FFT // 2, N_FFT // 2), mode="reflect")
    frames = (len(padded) - N_FFT) // HOP  # the last frame is dropped, as Whisper does
    window = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(N_FFT) / N_FFT)).astype(np.float32)
    filters = _mel_filters()
    out = np.empty((BANDS, frames), np.float32)
    step = 20000  # frames at a time: an hour's spectrogram is made without holding all its slices at once
    for k in range(0, frames, step):
        n = min(step, frames - k)
        idx = (k + np.arange(n))[:, None] * HOP + np.arange(N_FFT)[None, :]
        power = np.abs(np.fft.rfft(padded[idx] * window, axis=1)) ** 2
        out[:, k:k + n] = filters @ power.T.astype(np.float32)
    log = np.log10(np.maximum(out, 1e-10))
    log = np.maximum(log, log.max() - 8.0)
    return (log + 4.0) / 4.0


def sound(path) -> np.ndarray:
    """The recording's sound as 16 kHz mono samples (float32), read by FFmpeg."""
    import subprocess

    from .. import cancel, engines
    ffmpeg = engines.require("ffmpeg")
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-protocol_whitelist", "file", "-i", str(path),
           "-map", "0:a:0", "-ac", "1", "-ar", str(RATE), "-f", "f32le", "-"]
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                          creationflags=engines.NO_WINDOW) as p, cancel.track(p):
        data, err = p.communicate()
    cancel.check()
    if p.returncode != 0:
        from ..errors import ConversionError
        if b"0:a:0" in err:  # "Stream map '0:a:0' matches no streams"
            raise ConversionError("This file has no sound to write down")
        tail = err.decode("utf-8", "replace").strip().splitlines()[-3:]
        raise ConversionError("FFmpeg couldn't read the sound: " + (" ".join(tail) or f"exit {p.returncode}"))
    return np.frombuffer(data, np.float32)


# ---- tokens to text


def _byte_table() -> dict[str, int]:
    """GPT-2's byte-level BPE: each byte has a printable stand-in character; this maps them back."""
    keep = list(range(ord("!"), ord("~") + 1)) + list(range(ord("¡"), ord("¬") + 1)) + list(range(ord("®"), ord("ÿ") + 1))
    chars = keep[:]
    n = 0
    for b in range(256):
        if b not in keep:
            keep.append(b)
            chars.append(256 + n)
            n += 1
    return {chr(c): b for b, c in zip(keep, chars)}


class Tokens:
    def __init__(self, folder: str):
        with open(os.path.join(folder, "tokenizer.json"), encoding="utf-8") as f:
            data = json.load(f)
        self.pieces = {i: t for t, i in data["model"]["vocab"].items()}
        self.bytes = _byte_table()

    def text(self, ids) -> str:
        raw = bytearray()
        for i in ids:
            piece = self.pieces.get(int(i))
            if piece is not None and int(i) < EOT:
                raw += bytes(self.bytes.get(ch, 32) for ch in piece)
        return raw.decode("utf-8", "replace")


# ---- the model


class Whisper:
    def __init__(self):
        folder = need("speech")
        self.encoder = session("speech", "encoder.onnx")
        self.decoder = session("speech", "decoder.onnx")
        self.tokens = Tokens(folder)
        self.suppress = np.array(SUPPRESS, np.int64)
        # what each output after the logits is fed back in as
        self.cache_names = [o.name.replace("present.", "past_key_values.") for o in self.decoder.get_outputs()[1:]]

    def hear(self, mel: np.ndarray) -> np.ndarray:
        return self.encoder.run(None, {"input_features": mel[None].astype(np.float32)})[0]

    def step(self, ids: list[int], heard: np.ndarray, past: dict | None):
        """The next token's logits, and the cache to go on with."""
        feeds = {"input_ids": np.array([ids], np.int64), "encoder_hidden_states": heard,
                 "use_cache_branch": np.array([past is not None])}
        for i in range(LAYERS):
            for part in ("decoder", "encoder"):
                for kv in ("key", "value"):
                    name = f"past_key_values.{i}.{part}.{kv}"
                    feeds[name] = past[name] if past is not None else np.zeros((1, HEADS, 0, HEAD_DIM), np.float32)
        outs = self.decoder.run(None, feeds)
        cache = {n: v for n, v in zip(self.cache_names, outs[1:])}
        if past is not None:  # with the cache on, its encoder parts come back empty: they stay as the first step made them
            cache.update({n: v for n, v in past.items() if ".encoder." in n})
        return outs[0][0], cache

    def language(self, heard: np.ndarray) -> str:
        logits, _ = self.step([SOT], heard, None)
        ids = np.array(list(LANGUAGE_TOKEN.values()))
        return list(LANGUAGE_TOKEN)[int(np.argmax(logits[-1][ids]))]

    def window(self, heard: np.ndarray, lang: str, temperature: float, rng: np.random.Generator):
        """One window's tokens (timestamps among them), their average log probability, and how likely Whisper
        thinks it is that nothing is said."""
        prompt = [SOT, LANGUAGE_TOKEN[lang], TRANSCRIBE]
        logits, cache = self.step(prompt, heard, None)
        first = logits[0].astype(np.float64)
        no_speech = float(np.exp(first[NO_SPEECH] - _logsumexp(first)))
        out: list[int] = []
        total = 0.0
        cur = logits[-1]
        while len(out) < SAMPLE_LEN:
            scores = self._rules(cur.astype(np.float64), out)
            logp = scores - _logsumexp(scores)
            if temperature > 0:
                p = np.exp((scores - scores.max()) / temperature)
                tok = int(rng.choice(len(p), p=p / p.sum()))
            else:
                tok = int(np.argmax(scores))
            total += float(logp[tok])
            out.append(tok)
            if tok == EOT:
                break
            logits, cache = self.step([tok], heard, cache)
            cur = logits[-1]
        return out, total / (len(out) + 1), no_speech

    def _rules(self, s: np.ndarray, out: list[int]) -> np.ndarray:
        """Whisper's rules for the next token: no symbols, timestamps in pairs and never going back."""
        s[self.suppress] = -np.inf
        s[NO_TIMESTAMPS] = -np.inf
        if not out:
            s[[220, EOT]] = -np.inf  # it doesn't start with a space or stop at once
            s[:TIME_0] = -np.inf      # it starts with a timestamp, in the first second
            s[TIME_0 + FIRST_TIME_MAX + 1:] = -np.inf
            return s
        last_time = out[-1] >= TIME_0
        before_time = len(out) < 2 or out[-2] >= TIME_0
        if last_time:
            if before_time:
                s[TIME_0:] = -np.inf  # a pair just closed: words next
            else:
                s[:EOT] = -np.inf     # one timestamp alone: another (or the end) next
        times = [t for t in out if t >= TIME_0]
        if times:
            floor = times[-1] if last_time and not before_time else times[-1] + 1
            s[TIME_0:floor] = -np.inf
        logp = s - _logsumexp(s)
        if _logsumexp(logp[TIME_0:]) > logp[:TIME_0].max():
            s[:TIME_0] = -np.inf      # a timestamp is likelier than any word: take one
        return s


def _logsumexp(a: np.ndarray) -> float:
    m = np.max(a)
    if not np.isfinite(m):
        return float(m)
    return float(m + np.log(np.exp(a - m).sum()))


QUIET = 10 ** (-50 / 20)  # a window whose loudest 100 ms is quieter than -50 dBFS has nothing to hear


def _quiet(samples: np.ndarray) -> bool:
    step = RATE // 10
    n = len(samples) // step
    if n == 0:
        return float(np.sqrt(np.mean(samples ** 2))) < QUIET if len(samples) else True
    rms = np.sqrt((samples[:n * step].reshape(n, step).astype(np.float64) ** 2).mean(axis=1))
    return float(rms.max()) < QUIET


def _repeats(text: str) -> float:
    raw = text.encode("utf-8")
    return len(raw) / max(1, len(zlib.compress(raw)))


def transcribe(samples: np.ndarray, language: str | None = None, progress=None) -> tuple[str, list[Said]]:
    """The language (as given, or heard in the first 30 s) and what's said, in order."""
    from .. import cancel
    model = Whisper()
    mel = spectrogram(samples)
    content = mel.shape[1] - WINDOW  # frames of the recording itself (the rest is the silence added)
    rng = np.random.default_rng(0)
    said: list[Said] = []
    seek = 0
    lang = language
    while seek < content:
        cancel.check()
        size = min(WINDOW, content - seek)
        if _quiet(samples[seek * HOP:(seek + size) * HOP]):  # Whisper writes "you" over silence: not asked
            seek += size
            if progress:
                progress(min(seek, content), content)
            continue
        chunk = mel[:, seek:seek + size]
        if size < WINDOW:
            chunk = np.pad(chunk, ((0, 0), (0, WINDOW - size)))
        heard = model.hear(chunk)
        if lang is None:
            lang = model.language(heard)
        for t in TEMPERATURES:
            tokens, avg, no_speech = model.window(heard, lang, t, rng)
            text = model.tokens.text(tokens)
            if no_speech > SILENT and avg < UNSURE:
                tokens = []  # nothing said here
                break
            if _repeats(text) <= REPEATS and avg >= UNSURE:
                break
        offset = seek * HOP / RATE
        seek = _place(tokens, model.tokens, offset, size, seek, said)
        if progress:
            progress(min(seek, content), content)
    return lang or "en", [s for s in said if s.text]


def _place(tokens: list[int], words: Tokens, offset: float, size: int, seek: int, said: list[Said]) -> int:
    """The window's sentences added to said; returns where the next window starts."""
    tokens = [t for t in tokens if t != EOT]
    is_time = [t >= TIME_0 for t in tokens]
    single_end = len(is_time) >= 2 and is_time[-1] and not is_time[-2]
    cuts = [i + 1 for i in range(len(tokens) - 1) if is_time[i] and is_time[i + 1]]
    if cuts:
        if single_end:
            cuts.append(len(tokens))
        last = 0
        for cut in cuts:
            part = tokens[last:cut]
            start = offset + (part[0] - TIME_0) * 0.02
            end = offset + (part[-1] - TIME_0) * 0.02
            said.append(Said(start, max(end, start), words.text(part).strip()))
            last = cut
        if single_end:
            return seek + size
        nxt = seek + (tokens[last - 1] - TIME_0) * 2  # the next window starts where the last sentence ended
        return nxt if nxt > seek else seek + size
    times = [t for t in tokens if t >= TIME_0]
    end = (times[-1] - TIME_0) * 0.02 if times and times[-1] != TIME_0 else size * HOP / RATE
    text = words.text(tokens).strip()
    if text:
        said.append(Said(offset, offset + end, text))
    return seek + size
