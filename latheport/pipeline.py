"""Run a conversion route step by step, and write results without ever overwriting."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from . import assurance, cancel, engines, registry, smart
from .assurance import Report
from .detect import detect, mislabelled, strip_ext
from .errors import ConversionError, EngineMissingError
from .formats import MULTI_EXT, category
from .options import clean as clean_options
from .registry import Route, find_route

# Single-stream compressors keep the original name: notes.txt -> notes.txt.gz
STREAM_COMPRESSORS = {"gz", "bz2", "xz", "zst", "lz4", "br", "lzma", "z"}


def unique_path(path: Path) -> Path:
    """path, or 'name (1).ext', 'name (2).ext'... if it already exists."""
    if not path.exists():
        return path
    name = path.name
    ext = ""
    if not path.is_dir():
        low = name.lower()
        multi = next((m for m in MULTI_EXT if low.endswith("." + m)), None)
        ext = name[-len(multi) - 1:] if multi else path.suffix
    base = name[: len(name) - len(ext)] if ext else name
    i = 1
    while True:
        cand = path.with_name(f"{base} ({i}){ext}")
        if not cand.exists():
            return cand
        i += 1


def place(item: Path, target: Path) -> Path:
    """item (a file or folder) moved to target, or to 'name (1).ext'... when that's taken, never over anything:
    the window and `latheport watch` writing into one folder at once each get a name of their own. (A name
    looked up as free, then written to, could be taken in between; Windows' rename refuses to replace.)"""
    while sys.platform == "win32":
        final = unique_path(target)
        try:
            os.rename(item, final)
            return final
        except FileExistsError:  # taken a moment ago: the next free name
            continue
        except OSError:  # another drive: copied below
            break
    while True:  # the name claimed first (made empty), then filled
        final = unique_path(target)
        try:
            final.mkdir() if item.is_dir() else final.open("xb").close()
            break
        except FileExistsError:
            continue
    try:
        if item.is_dir():
            shutil.copytree(item, final, dirs_exist_ok=True)
            shutil.rmtree(item, ignore_errors=True)
        else:
            shutil.copy2(item, final)
            item.unlink(missing_ok=True)
    except BaseException:
        shutil.rmtree(final, ignore_errors=True) if final.is_dir() else final.unlink(missing_ok=True)
        raise
    return final


def plan(src: str | Path, dst_fmt: str, options: dict | None = None) -> dict:
    """What converting src to dst_fmt would do, converting nothing: the route and its steps, what it can't keep
    (notes), the settings it takes and uses, the engines it still needs, and what was measured of the file.
    The same as `latheport FILE --to FMT --plan --json`. Raises ConversionError when there's no way there."""
    from . import options as opts
    path = Path(src)
    fmt = detect(path)
    try:
        settings = opts.clean(options or {})
    except ValueError as exc:
        raise ConversionError(str(exc)) from None
    facts = assurance.measure(path, fmt)
    held = assurance.features(facts, fmt)
    route = pick_route(fmt, dst_fmt, held, settings)
    relabel = mislabelled(path)
    notes = list(dict.fromkeys(([relabel] if relabel else []) + [n.text for n in route.explain(held, settings)]))
    return {"source": str(path), "source_format": fmt, "target": dst_fmt,
            "route": " → ".join([fmt.upper()] + [e.dst.upper() for e in route.edges]),
            "steps": [{"from": e.src, "to": e.dst, "tool": assurance.tool(e)} for e in route.edges],
            "notes": notes, "settings": opts.applying(settings, route.settings), "takes": route.settings,
            "needs": [engines.ENGINES[k].name for k in route.missing_engines], "facts": facts or {}}


def pick_route(src_fmt: str, dst_fmt: str, held: frozenset[str] | None, options: dict) -> Route:
    """The route a conversion takes (and --plan shows): the one that keeps most of what this file has (held, when
    it was measured), unless that one needs an engine that isn't installed and the usual route doesn't."""
    route = planned = find_route(src_fmt, dst_fmt, options=options)
    if held is not None:
        route = find_route(src_fmt, dst_fmt, held, options)
        if route.missing_engines and not planned.missing_engines:
            route = planned
    return route


def step_name(cur: Path, base: str, dst_fmt: str, src_fmt: str | None = None) -> str:
    if dst_fmt in STREAM_COMPRESSORS:
        name = cur.name
        if src_fmt in STREAM_COMPRESSORS and name.lower().endswith(f".{src_fmt}"):  # notes.txt.gz -> notes.txt.xz
            name = name[:-len(src_fmt) - 1]
        return f"{name}.{'Z' if dst_fmt == 'z' else dst_fmt}"  # Unix compress is always .Z
    return f"{base}.{dst_fmt}"


def convert(src: str | Path, dst_fmt: str, out_dir: str | Path | None = None,
            src_fmt: str | None = None, report: Report | None = None, options: dict | None = None) -> Path:
    """Convert src to dst_fmt. Returns the path written (a file, or a folder for multi-page output).

    options: settings for the steps that take them (options.py); unset ones keep each format's
    defaults. report, when given, is filled in: the route, what it can't keep, and the checks
    made on the result (assurance.py). Checking never fails a conversion: a result that doesn't
    match is kept, and its report says what differs."""
    src = Path(src).resolve()
    if not src.exists():
        raise ConversionError(f"{src} doesn't exist")
    try:
        options = clean_options(options or {})
    except ValueError as exc:
        raise ConversionError(str(exc)) from None
    relabel = None if src_fmt else mislabelled(src)
    src_fmt = src_fmt or detect(src)
    if relabel and src_fmt == dst_fmt:
        raise ConversionError(f"{src.name} is a {dst_fmt.upper()} already: only its name says "
                              f"{src.suffix.lower()} (rename it to keep it as it is)")
    # what the check reads of the source is handed to the step that reads it again (registry.keep)
    with registry.sharing():
        before = assurance.measure(src, src_fmt) if report is not None else None
        held = assurance.features(before, src_fmt) if before is not None else None
        route = pick_route(src_fmt, dst_fmt, held, options)  # what this very file has can change the route
        missing = route.missing_engines + [k for k in smart.wanted(options, route.settings) if not engines.available(k)]
        if missing:  # a step's engine, or a smart tool a setting asks for
            raise EngineMissingError(list(dict.fromkeys(missing)))
        out = Path(out_dir).resolve() if out_dir else src.parent
        out.mkdir(parents=True, exist_ok=True)
        base = strip_ext(src)
        found: list[str] = []
        expected: list[registry.Expect] = []

        # a library still holding a temp file open must not fail a conversion that already succeeded
        with tempfile.TemporaryDirectory(prefix="latheport-", ignore_cleanup_errors=True) as tmp:
            cur = src
            tokens = registry.running(options, found, expected)
            try:
                for i, edge in enumerate(route.edges):
                    cancel.check()
                    step_dir = Path(tmp) / f"step{i}"
                    step_dir.mkdir()
                    target = step_dir / step_name(cur, base, edge.dst, edge.src)
                    try:
                        result = edge.func(cur, target, edge.src, edge.dst) or target
                    except ConversionError:
                        raise
                    except Exception as exc:  # library errors -> readable message
                        raise ConversionError(readable(exc, edge.src, edge.dst, src.name)) from exc
                    if not Path(result).exists():
                        raise ConversionError(f"{edge.src.upper()} -> {edge.dst.upper()} produced no output")
                    cur = Path(result)
            finally:
                registry.reset(tokens)
            cancel.check()  # stopped during the last step: write nothing
            final = place(cur, out / cur.name)
    if report is not None:
        _fill(report, src, src_fmt, relabel, route, held, options, found, before, final, expected)
    return final


def names_only(text: str) -> str:
    """text with every full path in it (a temp folder, the user's home) cut to the file's name."""
    import ntpath
    import re
    def name(m: re.Match) -> str:
        path = m.group(0).rstrip()
        return ntpath.basename(path.replace("\\\\", "\\").rstrip("\\/ ")) + m.group(0)[len(path):]
    # a path runs to a quote or the line's end, or up to the next path ("rename C:\a.txt to C:\b.txt")
    return re.sub(r"[A-Za-z]:[\\/](?:(?![A-Za-z]:[\\/])[^'\"\n<>|])*", name, text)


def readable(exc: Exception, src_fmt: str, dst_fmt: str, name: str) -> str:
    """A library's error as a person can read it: what went wrong with which file."""
    kind = src_fmt.upper()
    text = names_only(str(exc)).strip()
    low = text.lower()
    if type(exc).__name__ == "UnidentifiedImageError" or "cannot identify image file" in low:
        return f"{name} doesn't open as a {kind} picture: it may be damaged, or not really a {kind}"
    if "truncated" in low or "broken data stream" in low or "unexpected end" in low:
        return f"{name} is cut short or damaged, so it can't be read as {kind}"
    return f"{kind} to {dst_fmt.upper()} didn't work for {name}: {text or type(exc).__name__}"


def _fill(report: Report, src: Path, src_fmt: str, relabel: str | None, route: Route, held, options: dict,
          found: list[str], before: dict | None, final: Path, expected=()) -> None:
    lost = route.explain(held, options)
    report.source, report.source_format, report.target = str(src), src_fmt, route.dst
    report.detected = "content" if relabel else "extension"
    report.output = str(final)
    report.settings = {k: v for k, v in options.items() if k in route.settings}
    report.steps = [{"from": e.src, "to": e.dst, "tool": assurance.tool(e)} for e in route.edges]
    try:
        after = assurance.inspect(final, route.dst, words=bool(before and "words" in before))
        after = assurance.measure_expected(final, after, expected)
        report.checks = assurance.verify(before, after, lost, found, video=category(route.dst) == "Video",
                                         expected=expected)
    except assurance.Unreadable as exc:
        report.checks = [assurance.Check("Opens", None, None, "error",
                                         f"the result doesn't open as {route.dst.upper()} ({exc})")]
    # a "may" that the checks show didn't happen to this file ("pictures bigger than 1920 pixels are
    # scaled down", and it was smaller) isn't worth saying
    same = {c.name for c in report.checks if c.state == "ok"}
    lost = [n for n in lost if not (n.affects and not n.drops and
                                    all(assurance.NAMES.get(k) in same for k in n.affects if k in assurance.NAMES))]
    report.notes = list(dict.fromkeys(([relabel] if relabel else []) + [n.text for n in lost] + found))
    report.seconds = round(time.perf_counter() - report.started, 3)


JOINABLE = ("Audio", "Video")


def _join_pdfs(paths: list[Path], out_dir, report: Report | None, options: dict | None) -> Path:
    """PDFs one after another in one PDF (bookport), named after the first; Pages counts across all of them,
    and Split into and Compress apply to the whole."""
    from .converters import pdf
    try:
        options = clean_options(options or {})
    except ValueError as exc:
        raise ConversionError(str(exc)) from None
    out = Path(out_dir).resolve() if out_dir else paths[0].parent
    out.mkdir(parents=True, exist_ok=True)
    found: list[str] = []
    expected: list[registry.Expect] = []
    name = f"{strip_ext(paths[0])} (joined).pdf"
    with tempfile.TemporaryDirectory(prefix="latheport-", ignore_cleanup_errors=True) as tmp:
        tokens = registry.running(options, found, expected)
        try:
            made = pdf.rewrite_pdfs(paths, Path(tmp) / name, options.get("pages"), options.get("split"),
                                    options.get("compress"), bool(options.get("searchable")))
        finally:
            registry.reset(tokens)
        cancel.check()
        final = place(made, out / made.name)
    if report is not None:
        report.source, report.source_format = str(paths[0]), "pdf"
        report.target, report.output = "pdf", str(final)
        report.settings = {k: v for k, v in options.items() if k in ("pages", "split", "compress")}
        report.steps = [{"from": "pdf", "to": "pdf", "tool": "bookport"}]
        total = 0
        for p in paths:
            total += (assurance.inspect(p, "pdf") or {}).get("pages", 0)
        try:
            after = assurance.measure_expected(final, assurance.inspect(final, "pdf"), expected)
            want = {"pages": total}  # every page of every PDF, or as many as Pages lists
            report.checks = assurance.verify(want, after, [], found, expected=expected)
        except assurance.Unreadable as exc:
            report.checks = [assurance.Check("Opens", None, None, "error", f"the result doesn't open as PDF ({exc})")]
        report.notes = [f"{len(paths)} PDFs joined, in this order: " + ", ".join(x.name for x in paths), *found]
        report.seconds = round(time.perf_counter() - report.started, 3)
    return final


def join(srcs: list[str | Path], dst_fmt: str, out_dir: str | Path | None = None, report: Report | None = None,
         options: dict | None = None) -> Path:
    """Join sound or video files end to end, in the order given, into one dst_fmt file named after the
    first ("clip (joined).mp4"), next to it or in out_dir. The report, when given, has the check:
    the result's length is the parts' lengths added up."""
    from . import engines
    from .converters import media
    paths = [Path(s).resolve() for s in srcs]
    if len(paths) < 2:
        raise ConversionError("Joining needs two files or more")
    if dst_fmt == "pdf" and all(p.is_file() and detect(p) == "pdf" for p in paths):
        return _join_pdfs(paths, out_dir, report, options)
    for path in paths:
        if not path.is_file():
            raise ConversionError(f"{path} doesn't exist")
        if category(detect(path)) not in JOINABLE:
            raise ConversionError(f"{path.name} isn't a sound or video file: only those can be joined")
    if dst_fmt not in media.AUDIO | media.VIDEO:
        raise ConversionError(f"Files can be joined into a sound or video format, not {dst_fmt.upper()}")
    if not engines.available("ffmpeg"):
        raise EngineMissingError(["ffmpeg"])
    try:
        options = clean_options(options or {})
    except ValueError as exc:
        raise ConversionError(str(exc)) from None
    out = Path(out_dir).resolve() if out_dir else paths[0].parent
    out.mkdir(parents=True, exist_ok=True)
    found: list[str] = []
    expected: list[registry.Expect] = []
    name = f"{strip_ext(paths[0])} (joined).{dst_fmt}"
    with tempfile.TemporaryDirectory(prefix="latheport-", ignore_cleanup_errors=True) as tmp:
        tokens = registry.running(options, found, expected)
        try:
            total = media.join(paths, Path(tmp) / name, dst_fmt)
        finally:
            registry.reset(tokens)
        cancel.check()
        final = place(Path(tmp) / name, out / name)
    if report is not None:
        report.source = str(paths[0])
        report.source_format = detect(paths[0])
        report.target, report.output = dst_fmt, str(final)
        report.settings = {k: v for k, v in options.items() if k in ("video_quality", "video_size", "fps", "bitrate",
                                                                  "loudness")}
        report.steps = [{"from": "+".join(dict.fromkeys(detect(x) for x in paths)), "to": dst_fmt, "tool": "FFmpeg"}]
        try:
            after = assurance.measure_expected(final, assurance.inspect(final, dst_fmt), expected)
            report.checks = assurance.verify({"duration": round(total, 3)}, after, [], found,
                                             video=category(dst_fmt) == "Video", expected=expected)
        except assurance.Unreadable as exc:
            report.checks = [assurance.Check("Opens", None, None, "error",
                                             f"the result doesn't open as {dst_fmt.upper()} ({exc})")]
        report.notes = [f"{len(paths)} files joined, in this order: " + ", ".join(x.name for x in paths), *found]
        report.seconds = round(time.perf_counter() - report.started, 3)
    return final


def can_extract(fmt: str) -> bool:
    from .converters.archives import NATIVE_EXTRACT
    return fmt in NATIVE_EXTRACT


def extract(src: str | Path, out_dir: str | Path | None = None, src_fmt: str | None = None,
            notes: list[str] | None = None) -> Path:
    """Extract an archive next to it (or into out_dir). A lone top-level folder isn't double-nested.
    notes, when given, gets a line for anything left out or renamed (links, unsafe names)."""
    from .converters.archives import extract_to
    src = Path(src).resolve()
    fmt = src_fmt or detect(src)
    if not can_extract(fmt):
        raise ConversionError(f"{src.name} isn't an archive Latheport can extract")
    out = Path(out_dir).resolve() if out_dir else src.parent
    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".latheport-", dir=out))
    try:
        try:
            extract_to(src, tmp, fmt, notes)
        except ConversionError:
            raise
        except Exception as exc:  # library errors on a damaged archive -> a readable message
            raise ConversionError(f"Couldn't extract {src.name}: {names_only(str(exc))}") from exc
        cancel.check()
        entries = list(tmp.iterdir())
        if len(entries) == 1:
            final = place(entries[0], out / entries[0].name)
        else:
            final = place(tmp, out / strip_ext(src))
        return final
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
