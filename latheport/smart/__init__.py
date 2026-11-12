"""Smart tools: a picture's background taken out, a picture made bigger with detail drawn in, the text
in pictures and scanned pages read (OCR), and what's said in sound and video written down. Each runs
a model with ONNX Runtime on this computer: nothing is sent anywhere.

Neither the runtime nor the models ship with Latheport. Each tool's model, and the runtime the first
time any tool runs, is downloaded once when it's first needed (asked first, with its size), from a
pinned address, checked against its SHA-256, into Latheport's own folder. The tools count as engines
(engines.py), so the window and the command line ask and install them as they do FFmpeg.

Inference keeps to three quarters of the cores, so the computer stays usable while it runs."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import os
import shutil
import sys
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass

from ..errors import ConversionError

HOME = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Latheport")
# where the models go; LATHEPORT_MODELS points the tests (and anyone who wants) at another folder
MODELS = os.environ.get("LATHEPORT_MODELS") or os.path.join(HOME, "models")


@dataclass(frozen=True)
class Download:
    url: str
    sha256: str
    size: int                              # bytes, as downloaded
    keep: tuple[tuple[str, str], ...] = ()  # a zip: (the file inside, saved as); a plain file: ()
    name: str = ""                         # a plain file: saved as


@dataclass(frozen=True)
class Tool:
    key: str
    name: str                      # as the window says it
    downloads: tuple[Download, ...]
    licence: str                   # the model's
    source: str                    # where the model comes from, for the licences list

    @property
    def files(self) -> list[str]:
        """The files the tool needs, in its folder."""
        return [n for d in self.downloads for n in ([d.name] if not d.keep else [s for _, s in d.keep])]

    @property
    def size(self) -> int:
        return sum(d.size for d in self.downloads)


HF = "https://huggingface.co"
OCR_REPO = f"{HF}/monkt/paddleocr-onnx/resolve/7b02d0a30a07ba2b92ad1ff5a8941ae2c633de65"
WHISPER_REPO = f"{HF}/onnx-community/whisper-base/resolve/27a07b4b52b3a4c5886134bb4320e93e1a4bbc7c"
QAIHUB = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models"

ESRGAN = "real_esrgan_general_x4v3-onnx-float/real_esrgan_general_x4v3"
TOOLS: dict[str, Tool] = {t.key: t for t in (
    Tool("background", "Background remover", (
        Download(f"{HF}/studioludens/birefnet-lite-512/resolve/962e67969ec6acec9c19fab76acee4f5e22e990c/onnx/model.onnx",
                 "1cb0fb360dadd15af77c639085d77a9df67db0c64315560c3de005f676345ac2", 191877254, name="birefnet.onnx"),),
        "MIT", "BiRefNet lite (Zheng et al.), github.com/ZhengPeng7/BiRefNet; its 512 x 512 ONNX from "
               "huggingface.co/studioludens/birefnet-lite-512"),
    Tool("upscale", "Upscaler", (
        Download(f"{QAIHUB}/real_esrgan_general_x4v3/releases/v0.66.0/real_esrgan_general_x4v3-onnx-float.zip",
                 "fc094329b24a00c936ebc1cc4c51bc59633e7dc36f4a553344342171cbd3c7cf", 4540895,
                 # the .onnx finds its weights by the .data file's name: both keep their names
                 keep=((f"{ESRGAN}.onnx", "real_esrgan_general_x4v3.onnx"),
                       (f"{ESRGAN}.data", "real_esrgan_general_x4v3.data"))),),
        "BSD-3-Clause", "Real-ESRGAN General x4v3 (Wang et al.), github.com/xinntao/Real-ESRGAN; "
                        "ONNX from Qualcomm AI Hub (aihub.qualcomm.com)"),
    Tool("ocr", "Text reader (OCR)", (
        Download(f"{OCR_REPO}/detection/v3/det.onnx",
                 "ee40e80071ba3a320d4efda75f3e22047a7d049e9bf7bcaaf9daea23fc21b935", 2429873, name="detect.onnx"),
        Download(f"{OCR_REPO}/languages/latin/rec.onnx",
                 "614ffc2d6d3902d360fad7f1b0dd455ee45e877069d14c4e51a99dc4ef144409", 7862832, name="read.onnx"),
        Download(f"{OCR_REPO}/languages/latin/dict.txt",
                 "3c0a8a79b612653c25f765271714f71281e4e955962c153e272b7b8c1d2b13ff", 1634, name="letters.txt")),
        "Apache-2.0", "PP-OCRv3 detection and PP-OCRv5 Latin recognition (PaddleOCR), github.com/PaddlePaddle/PaddleOCR; "
                      "ONNX from huggingface.co/monkt/paddleocr-onnx"),
    Tool("speech", "Speech recognition", (
        Download(f"{WHISPER_REPO}/onnx/encoder_model_int8.onnx",
                 "ca6177401f86a2c6b4dc5f7fc02fbca680678906bd0c22f6d89f0b80f124253f", 23201297, name="encoder.onnx"),
        Download(f"{WHISPER_REPO}/onnx/decoder_model_merged_int8.onnx",
                 "fa3ef9902734ce5ae6f9ef2bdb2ba9a6c4b5785b09f4f420ce036573dc9d090b", 53693315, name="decoder.onnx"),
        Download(f"{WHISPER_REPO}/tokenizer.json",
                 "27fc476bfe7f17299480be2273fc0608e4d5a99aba2ab5dec5374b4482d1a566", 2480466, name="tokenizer.json")),
        "MIT", "Whisper base (OpenAI), github.com/openai/whisper; ONNX from huggingface.co/onnx-community/whisper-base"),
)}


# ---- ONNX Runtime: the pip package when Latheport runs from source, else downloaded from PyPI

RUNTIME_VERSION = "1.31.0"
_PYPI = "https://files.pythonhosted.org/packages"
RUNTIME_WHEELS = {  # the wheel for each Python Latheport runs on (the built app has one of them)
    "cp311": (f"{_PYPI}/37/de/6cab7e39917cc87728d2f00abe97c81fe86b29f9e1f758627864c28f0c21/onnxruntime-1.31.0-cp311-cp311-win_amd64.whl",
              "317608967b03807ed4661113b08293fac02a1db6496a6863a07d9f19232936ad", 14885750),
    "cp312": (f"{_PYPI}/be/ed/b12cea136ccd7b03d924f46b8393faf7ceac21115c0c50e729faa248cf23/onnxruntime-1.31.0-cp312-cp312-win_amd64.whl",
              "09d56445c1753e66e0912de69d3f0184016ad9a191dcd6925bf5dd570d2bfbe5", 14888841),
    "cp313": (f"{_PYPI}/c4/86/05ed2056f43b27aaf12ebc592ebd9037a26bed315958cf882f43425fd469/onnxruntime-1.31.0-cp313-cp313-win_amd64.whl",
              "73e0165d58ece068c2a8a1c477c90b38e5a8adbbd399fdfdfd4bd79cbc28ff8d", 14888306),
    "cp314": (f"{_PYPI}/6a/66/0bf4fdb9f58efa69cf4eddde24c72aebcc628d6ff1d67c9546145c6b9922/onnxruntime-1.31.0-cp314-cp314-win_amd64.whl",
              "83e3dbcf6abc6189c4bdf7d329c07ba1133c88172134c266d84b4409aa3b9dbf", 15268709),
}
# the parts of the wheel inference needs: not its tools for making and changing models
RUNTIME_LEAVE = ("onnxruntime/tools/", "onnxruntime/transformers/", "onnxruntime/quantization/",
                 "onnxruntime/datasets/", "onnxruntime/backend/")


def _tag() -> str:
    return f"cp{sys.version_info[0]}{sys.version_info[1]}"


def runtime_folder() -> str:
    from .. import engines  # the engines folder: ONNX Runtime is one of them
    return os.path.join(engines.ENGINE_HOME, f"onnxruntime-{RUNTIME_VERSION}-{_tag()}")


def runtime_found() -> bool:
    """ONNX Runtime is here: downloaded, or (running from source) the pip package."""
    if os.path.isfile(os.path.join(runtime_folder(), "onnxruntime", "capi", "onnxruntime.dll")):
        return True
    if getattr(sys, "frozen", False):
        return False
    try:
        return importlib.util.find_spec("onnxruntime") is not None
    except (ImportError, ValueError):
        return False


def runtime_size() -> int:
    """Bytes to download for the runtime (0 when it's here)."""
    return 0 if runtime_found() else RUNTIME_WHEELS.get(_tag(), ("", "", 15 << 20))[2]


_load_lock = threading.Lock()


def runtime():
    """The onnxruntime module, from where runtime_found() saw it."""
    with _load_lock:
        if "onnxruntime" in sys.modules:
            return sys.modules["onnxruntime"]
        folder = runtime_folder()
        if os.path.isdir(os.path.join(folder, "onnxruntime")) and folder not in sys.path:
            sys.path.insert(0, folder)
        try:
            return importlib.import_module("onnxruntime")
        except ImportError as exc:
            raise ConversionError(f"ONNX Runtime didn't load: {exc}") from None


def download_runtime(log: Callable[[str], None] | None = None) -> None:
    import packport
    if _tag() not in RUNTIME_WHEELS:
        raise ConversionError(f"There's no ONNX Runtime for Python {sys.version_info[0]}.{sys.version_info[1]} here")
    url, sha, _ = RUNTIME_WHEELS[_tag()]
    dest = runtime_folder()
    home = os.path.dirname(dest)
    os.makedirs(home, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="latheport-ort-", dir=home) as tmp:
        wheel = os.path.join(tmp, "runtime.whl")
        fetch(url, sha, wheel, "ONNX Runtime", log)
        unpacked = os.path.join(tmp, "unpacked")
        try:
            with packport.open(wheel) as archive:
                for item, stream in archive.files():
                    name = item.name.replace("\\", "/")
                    if not name.startswith("onnxruntime/") or name.startswith(RUNTIME_LEAVE) or ".." in name.split("/"):
                        continue
                    out = os.path.join(unpacked, *name.split("/"))
                    os.makedirs(os.path.dirname(out), exist_ok=True)
                    with open(out, "xb") as f:
                        shutil.copyfileobj(stream, f, 1 << 20)
        except packport.PackportError as exc:
            raise ConversionError(f"Couldn't unpack ONNX Runtime: {exc}") from None
        if not os.path.isfile(os.path.join(unpacked, "onnxruntime", "capi", "onnxruntime.dll")):
            raise ConversionError("The ONNX Runtime download has no onnxruntime.dll")
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(unpacked, dest)


# ---- the models


def folder(key: str) -> str:
    return os.path.join(MODELS, key)


def found(key: str) -> bool:
    """The tool's model files are all here (they're only ever put here whole, checked)."""
    tool = TOOLS[key]
    return all(os.path.isfile(os.path.join(folder(key), f)) for f in tool.files)


def find(key: str) -> str | None:
    """The tool's folder when it can run: its model and the runtime are here."""
    return folder(key) if found(key) and runtime_found() else None


def download_size(key: str) -> int:
    """Bytes the tool still needs to download: its model and the runtime, as far as either is missing."""
    return (0 if found(key) else TOOLS[key].size) + runtime_size()


def megabytes(n: int) -> str:
    return f"{max(1, round(n / 1e6)):,} MB"


def fetch(url: str, sha256: str, dest: str, what: str, log: Callable[[str], None] | None) -> None:
    """url saved as dest, refused unless its SHA-256 is sha256."""
    import urllib.request  # here: opening the window doesn't load the web code (only the update check needs it)
    if log:
        log(f"Downloading {what}")
    h = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": "Latheport"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done, told = 0, 0
            while chunk := r.read(1 << 20):
                h.update(chunk)
                f.write(chunk)
                done += len(chunk)
                if log and total > 20 << 20 and done - told >= total // 10:
                    told = done
                    log(f"  {done * 100 // total}% of {megabytes(total)}")
    except OSError as exc:
        raise ConversionError(f"Couldn't download {what}: {exc}") from None
    if h.hexdigest() != sha256:
        os.remove(dest)
        raise ConversionError(f"The {what} download didn't match its published checksum, so it isn't used")


def download(key: str, log: Callable[[str], None] | None = None) -> None:
    """The tool's model (and the runtime first, if it's missing), whole or not at all."""
    import packport
    if not runtime_found():
        download_runtime(log)
    if found(key):
        return
    tool = TOOLS[key]
    os.makedirs(MODELS, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"latheport-{key}-", dir=MODELS) as tmp:
        ready = os.path.join(tmp, "ready")
        os.makedirs(ready)
        for i, d in enumerate(tool.downloads):
            part = os.path.join(tmp, f"part{i}")
            what = tool.name if len(tool.downloads) == 1 else f"{tool.name} ({i + 1} of {len(tool.downloads)})"
            fetch(d.url, d.sha256, part, what, log)
            if not d.keep:
                os.replace(part, os.path.join(ready, d.name))
                continue
            wanted = dict(d.keep)
            try:
                with packport.open(part) as archive:
                    for item, stream in archive.files():
                        name = item.name.replace("\\", "/")
                        if name in wanted:
                            with open(os.path.join(ready, wanted.pop(name)), "xb") as f:
                                shutil.copyfileobj(stream, f, 1 << 20)
            except packport.PackportError as exc:
                raise ConversionError(f"Couldn't unpack {tool.name}: {exc}") from None
            if wanted:
                raise ConversionError(f"The {tool.name} download has no {', '.join(wanted)}")
        shutil.rmtree(folder(key), ignore_errors=True)
        os.replace(ready, folder(key))


NEEDS = {"background": "background", "upscale": "upscale", "searchable": "ocr", "speech_subs": "speech",
         "subtitles_from": "speech"}  # the tool each setting needs when it's set


def wanted(opts: dict, takes) -> list[str]:
    """The smart tools the settings opts ask for, of those a conversion takes (its setting keys)."""
    return list(dict.fromkeys(tool for key, tool in NEEDS.items() if opts.get(key) and key in takes))


def need(key: str) -> str:
    """The tool's folder, or EngineMissingError so the window or command line can offer to install it."""
    path = find(key)
    if path is None:
        from ..errors import EngineMissingError
        raise EngineMissingError([key])
    return path


# ---- running a model


def threads() -> int:
    """Three quarters of the cores: the computer stays usable while a model runs."""
    return max(1, (os.cpu_count() or 4) * 3 // 4)


_sessions: dict[str, object] = {}
_session_lock = threading.Lock()


def session(key: str, name: str, lean: bool = False):
    """An inference session for the tool's model file name, made once and kept. lean: memory handed back
    after each run instead of pooled (a big model: 15% less at its peak, a little slower)."""
    path = os.path.join(need(key), name)
    with _session_lock:
        if path not in _sessions:
            ort = runtime()
            so = ort.SessionOptions()
            so.intra_op_num_threads = threads()
            so.inter_op_num_threads = 1
            so.log_severity_level = 3  # errors only
            # waiting threads sleep instead of spinning: 4x faster when other programs are busy too (a game,
            # a build), and no cores burnt waiting
            so.add_session_config_entry("session.intra_op.allow_spinning", "0")
            if lean:
                so.enable_cpu_mem_arena = False
            try:
                _sessions[path] = ort.InferenceSession(path, sess_options=so, providers=["CPUExecutionProvider"])
            except Exception as exc:  # noqa: BLE001 - ONNX Runtime has no one error type
                raise ConversionError(f"{TOOLS[key].name}'s model didn't load ({exc}); "
                                      f"delete {folder(key)} to download it again") from None
        return _sessions[path]

