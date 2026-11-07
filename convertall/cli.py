"""convertall command line.

  convertall photo.heic --to png          convert (output lands next to the input)
  convertall *.wav --to mp3 -o out        batch, into a folder
  convertall report.docx --list           what can this become?
  convertall stuff --to 7z                compress a file or folder
  convertall archive.rar --extract        unpack
  convertall clip.mov --to gif --plan     show the route and what it can't keep, convert nothing
  convertall *.jpg --to webp --preset "Smaller for email and web"
  convertall photo.png --to jpg --quality 80 --json
  convertall talk.mp4 --to mp4 --start 1:30 --end 5:00 --target-size 10
  convertall part1.mp4 part2.mp4 --to mp4 --join   join videos (or sound) into one, in order
  convertall view model.fbx               look at a 3D model, in a window
  convertall watch Downloads --to pdf     convert what lands in a folder (into Downloads/Converted)
  convertall presets                      list presets and settings
  convertall engines [--install KEY]      show / install conversion engines
"""

from __future__ import annotations

import argparse
import glob
import json
import signal
import subprocess
import sys
from pathlib import Path

from . import __version__, assurance, cancel, engines, options, pipeline, registry
from .assurance import Report
from .detect import detect, mislabelled, normalize
from .errors import ConversionError, EngineMissingError
from .formats import CATEGORIES, category, describe, suggest, viewable


def _expand(patterns: list[str]) -> list[Path]:
    """Windows shells don't expand wildcards, so do it here."""
    paths: list[Path] = []
    for p in patterns:
        hits = glob.glob(p) if any(c in p for c in "*?[") else [p]
        paths += [Path(h) for h in hits]
    return paths


def _ask_install(keys: list[str], assume_yes: bool, out=None) -> bool:
    out = out or sys.stdout
    for key in keys:
        eng = engines.ENGINES[key]
        if not assume_yes:
            required = f"{eng.name} is required. Re-run with -y to install it."
            if sys.stdin is None or not sys.stdin.isatty():  # the built exe can have no stdin at all
                print(required, file=sys.stderr)
                return False
            # the question goes to stderr, so --json output stays only JSON
            print(f"{eng.name} ({eng.size}) is needed for this. Install it now? [y/N] ", end="", file=sys.stderr, flush=True)
            try:
                answer = input()
            except EOFError:  # NUL passes for a terminal on Windows, and has nothing to read
                print(f"\n{required}", file=sys.stderr)
                return False
            if answer.strip().lower() not in ("y", "yes"):
                return False
        print(f"Installing {eng.name}...", file=out)
        try:
            ok = engines.install(key, log=lambda line: print("  " + line, file=out))
        except ConversionError as exc:
            print(f"Couldn't install {eng.name}: {exc}", file=sys.stderr)
            return False
        if not ok:
            print(f"Couldn't install {eng.name}.", file=sys.stderr)
            return False
    return True


def cmd_engines(args) -> int:
    if args.install:
        key = args.install.lower()
        shown = [e.key for e in engines.ENGINES.values() if not e.hidden]
        if key not in shown:
            print(f"Unknown engine '{key}'. Choose from: {', '.join(shown)}", file=sys.stderr)
            return 2
        return 0 if _ask_install([key], True) else 1
    for eng, path in engines.status():
        state = path if path else f"not installed  (convertall engines --install {eng.key}, {eng.size})"
        print(f"  {eng.name:<12} {state}")
    import packport
    from bookport.djvu import _accel as books
    from pixport import _accel as pictures
    for name, compiled, without in (("packport", packport.accelerated(), "archives unpack more slowly"),
                                    ("bookport", books.accelerated(), "DjVu books take longer"),
                                    ("pixport", pictures.accelerated(), "RAW photos take longer, and no JPEG XL")):
        state = f"compiled decoders ({name}-native)" if compiled else f"Python decoders ({name}-native isn't installed: {without})"
        print(f"  {name:<12} {state}")
    return 0


def cmd_list(paths: list[Path], as_json: bool = False) -> int:
    for path in paths:
        fmt = detect(path)
        routes = registry.targets(fmt)
        if as_json:
            _emit({"source": str(path), "source_format": fmt, "kind": describe(fmt),
                   "suggested": suggest(fmt, routes), "extract": pipeline.can_extract(fmt),
                   "targets": [{"format": dst, "category": category(dst), "route": _arrow(fmt, route),
                                "needs": [engines.ENGINES[k].name for k in route.missing_engines]}
                               for dst, route in sorted(routes.items())]})
            continue
        print(f"{path.name}  [{describe(fmt)}]")
        if not routes:
            print("  (nothing)")
        common = suggest(fmt, routes)
        if common:
            print(f"  {'Most common':<13} {common}")
        by_cat: dict[str, list[str]] = {}
        for dst, route in routes.items():
            by_cat.setdefault(category(dst), []).append(dst + ("*" if route.missing_engines else ""))
        for cat in CATEGORIES:
            if cat in by_cat:
                print(f"  {cat:<13} {' '.join(sorted(by_cat[cat]))}")
        if any(r.missing_engines for r in routes.values()):
            print("  * needs an engine that will be installed on first use")
        if pipeline.can_extract(fmt):
            print("  Archive       --extract")
    return 0


def _emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False, default=str), flush=True)


def _arrow(fmt: str, route) -> str:
    return " → ".join([fmt.upper()] + [e.dst.upper() for e in route.edges])


def cmd_plan(paths: list[Path], target: str, settings: dict, as_json: bool) -> int:
    """Each file's route, what it can't keep and the settings it takes, converting nothing."""
    failures = 0
    for path in paths:
        try:
            fmt = detect(path)
            facts = assurance.measure(path, fmt)
            held = assurance.features(facts, fmt)
            route = registry.find_route(fmt, target, held, settings)
        except (ConversionError, OSError) as exc:  # OSError: missing, locked or unreadable
            _fail(path, exc, "", as_json)
            failures += 1
            continue
        relabel = mislabelled(path)
        notes = list(dict.fromkeys(([relabel] if relabel else []) + [n.text for n in route.explain(held, settings)]))
        steps = [{"from": e.src, "to": e.dst, "tool": assurance.tool(e)} for e in route.edges]
        used = options.applying(settings, route.settings)
        needs = [engines.ENGINES[k].name for k in route.missing_engines]
        if as_json:
            _emit({"source": str(path), "source_format": fmt, "target": target,
                   "route": _arrow(fmt, route),
                   "steps": steps, "notes": notes, "settings": used, "takes": route.settings, "needs": needs,
                   "facts": facts or {}})
            continue
        tools = ", ".join(dict.fromkeys(st["tool"] for st in steps))
        print(f"{path.name}: {_arrow(fmt, route)} via {tools}")
        for line in notes:
            print(f"  {line}")
        if route.settings:
            print("  Settings: " + ", ".join(f"{k}={used[k]}" if k in used else k for k in route.settings))
        if needs:
            print(f"  Needs {' and '.join(needs)} (installed on first use)")
    return 1 if failures else 0


def cmd_convert(paths: list[Path], target: str | None, extract: bool, out: str | None, yes: bool,
                settings: dict | None = None, as_json: bool = False, join: bool = False) -> int:
    """Ctrl+C stops the conversion as Cancel does in the window: at its next safe point, cleaned up
    (and the files after it aren't started). A second Ctrl+C stops at once."""
    token = cancel.Token()

    def stop(_signum, _frame):
        if token.cancelled:
            raise KeyboardInterrupt
        print("Stopping... (Ctrl+C again to stop at once)", file=sys.stderr, flush=True)
        token.cancel()

    previous = signal.signal(signal.SIGINT, stop)
    try:
        with cancel.using(token):
            if join:
                return _join(paths, target, out, yes, settings, as_json)
            return _convert_all(paths, target, extract, out, yes, settings, as_json)
    except cancel.Cancelled:
        print("Stopped: what was being converted wasn't saved", file=sys.stderr)
        return 130
    finally:
        signal.signal(signal.SIGINT, previous)


def _join(paths: list[Path], target: str, out: str | None, yes: bool, settings: dict | None, as_json: bool) -> int:
    info = sys.stderr if as_json else sys.stdout
    for attempt in range(2):
        try:
            report = Report()
            result = pipeline.join(paths, target, out, report=report, options=settings)
        except EngineMissingError as exc:
            if attempt == 0 and _ask_install(exc.engines, yes, info):
                continue
            _fail(paths[0], exc, "", as_json)
            return 1
        except (ConversionError, OSError) as exc:
            _fail(paths[0], exc, "", as_json)
            return 1
        if as_json:
            _emit(report.to_dict())
        else:
            print(f"{len(paths)} files -> {result}")
            for note in report.lines()[1:]:
                print(f"  {note}")
        return 0
    return 1


def _convert_all(paths: list[Path], target: str | None, extract: bool, out: str | None, yes: bool,
                 settings: dict | None, as_json: bool) -> int:
    failures = 0
    info = sys.stderr if as_json else sys.stdout  # with --json, stdout holds only the reports
    for i, path in enumerate(paths, 1):
        prefix = f"[{i}/{len(paths)}] " if len(paths) > 1 and not as_json else ""
        for attempt in range(2):
            try:
                notes: list[str] = []
                report = None
                if extract:
                    result = pipeline.extract(path, out, notes=notes)
                else:
                    report = Report()
                    result = pipeline.convert(path, target, out, report=report, options=settings)
                    notes = report.lines()[1:]
                if as_json:
                    _emit(report.to_dict() if report else {"source": str(path), "output": str(result), "notes": notes})
                else:
                    print(f"{prefix}{path.name} -> {result}")
                    for note in notes:
                        print(f"{prefix}  {note}")
                break
            except cancel.Cancelled:
                raise
            except EngineMissingError as exc:
                if attempt == 0 and _ask_install(exc.engines, yes, info):
                    continue
                _fail(path, exc, prefix, as_json)
                failures += 1
                break
            except (ConversionError, OSError) as exc:
                _fail(path, exc, prefix, as_json)
                failures += 1
                break
    return 1 if failures else 0


def _fail(path: Path, exc: Exception, prefix: str, as_json: bool) -> None:
    if as_json:
        _emit({"source": str(path), "error": str(exc)})
    else:
        print(f"{prefix}{path.name}: {exc}", file=sys.stderr)


def cmd_presets() -> int:
    print("Presets (--preset NAME):")
    for name, values in options.all_presets().items():
        mine = "" if name in options.PRESETS else "  (yours)"
        print(f"  {name}{mine}: " + (", ".join(f"{k}={v}" for k, v in values.items()) or "ConvertAll's own settings"))
    print("\nSettings (each applies only where a conversion takes it; unset ones keep the defaults):")
    for st in options.SETTINGS.values():
        kind = "|".join(st.choices) if st.choices else "seconds or m:ss" if st.time else f"{st.low}-{st.high}"
        print(f"  --{st.key.replace('_', '-'):<14} {kind:<34} {st.help}. Default: {st.auto}")
    return 0


def cmd_watch(folders: list[Path], target: str, settings: dict, as_json: bool) -> int:
    """Convert what lands in the folders until Ctrl+C."""
    from . import watch
    watches = [watch.Watch(f.resolve(), target, settings) for f in folders]
    info = sys.stderr if as_json else sys.stdout
    for w in watches:
        print(f"Watching {w.folder}: new files become {target.upper()} in {w.out}", file=info)
    print("Files already there are left alone. Ctrl+C stops.", file=info, flush=True)
    token = cancel.Token()

    def convert(path: Path, w) -> None:
        try:
            with cancel.using(token):
                report = Report()
                result = pipeline.convert(path, w.target, w.out, report=report, options=w.options)
        except cancel.Cancelled:
            return
        except Exception as exc:  # noqa: BLE001 - one odd file doesn't end a watch that runs for days
            _fail(path, exc, "", as_json)
            return
        if as_json:
            _emit(report.to_dict())
        else:
            print(f"{path.name} -> {result}", flush=True)
            for note in report.lines()[1:]:
                print(f"  {note}")

    previous = signal.signal(signal.SIGINT, lambda *_: token.cancel())
    try:
        watch.run(watches, convert, lambda: token.cancelled)
    finally:
        signal.signal(signal.SIGINT, previous)
    print("Stopped watching", file=info)
    return 0


def cmd_view(paths: list[Path]) -> int:
    """Open each model in the 3D viewer. The installed terminal tool has no window code of its
    own, so it hands the files to ConvertAll.exe next to it."""
    for path in paths:
        if not path.is_file() or not viewable(detect(path)):
            print(f"{path.name} isn't a 3D model or CAD part ConvertAll can show", file=sys.stderr)
            return 2
    files = [str(p.resolve()) for p in paths]
    if getattr(sys, "frozen", False):
        app = Path(sys.executable).with_name("ConvertAll.exe")
        if not app.is_file():
            print(f"ConvertAll.exe isn't next to {Path(sys.executable).name}, so there's no window to show "
                  "the model in", file=sys.stderr)
            return 1
        subprocess.Popen([str(app), "--view", *files])
        return 0
    from .instance import main as gui_main
    return gui_main(["--view", *files])


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            # a pipe or file gets UTF-8 (what scripts reading --json expect); a console keeps its own
            piped = not stream.isatty()
            stream.reconfigure(**({"encoding": "utf-8"} if piped else {}), errors="replace")

    if argv and argv[0] == "engines":
        p = argparse.ArgumentParser(prog="convertall engines", description="Show or install conversion engines")
        shown = [e.key for e in engines.ENGINES.values() if not e.hidden]
        p.add_argument("--install", metavar="KEY", help=f"one of: {', '.join(shown)}")
        return cmd_engines(p.parse_args(argv[1:]))

    if argv and argv[0] == "presets":
        return cmd_presets()

    if argv and argv[0] == "watch":
        p = argparse.ArgumentParser(prog="convertall watch", description="Convert the files that land in folders, "
                                    "into a Converted folder inside each, until Ctrl+C")
        p.add_argument("folders", nargs="+", help="folders to watch")
        p.add_argument("--to", "-t", required=True, metavar="FORMAT", help="what new files become")
        p.add_argument("--json", action="store_true", help="one JSON report per file on stdout")
        p.add_argument("--preset", metavar="NAME", help="a named set of settings")
        for st in options.SETTINGS.values():
            if st.remember:
                p.add_argument(f"--{st.key.replace('_', '-')}", dest=st.key, metavar="|".join(st.choices) or "N",
                               help=f"{st.help} (default: {st.auto})")
        a = p.parse_args(argv[1:])
        folders = [Path(f) for f in a.folders]
        missing = [str(f) for f in folders if not f.is_dir()]
        if missing:
            print(f"Not a folder: {', '.join(missing)}", file=sys.stderr)
            return 2
        try:
            settings = options.preset(a.preset) if a.preset else {}
            settings.update(options.clean({k: getattr(a, k, None) for k in options.SETTINGS}))
        except KeyError:
            print(f"No preset named '{a.preset}'. Run 'convertall presets' to see them.", file=sys.stderr)
            return 2
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return cmd_watch(folders, normalize(a.to), settings, a.json)

    if argv and argv[0] == "view":
        p = argparse.ArgumentParser(prog="convertall view", description="Show 3D models in a window")
        p.add_argument("files", nargs="+", help="3D models or CAD parts")
        paths = _expand(p.parse_args(argv[1:]).files)
        missing = [str(x) for x in paths if not x.exists()]
        if missing or not paths:
            print(f"Not found: {', '.join(missing) or ' '.join(argv[1:])}", file=sys.stderr)
            return 2
        return cmd_view(paths)

    p = argparse.ArgumentParser(prog="convertall", description="Convert, compress or extract any file.",
                                epilog="Run 'convertall engines' to see installed conversion engines, "
                                       "'convertall view FILE' to look at a 3D model, or "
                                       "'convertall presets' for presets and settings.")
    p.add_argument("files", nargs="+", help="files or folders (wildcards ok)")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--to", "-t", metavar="FORMAT", help="target format, e.g. png, mp3, pdf, zip")
    mode.add_argument("--list", "-l", action="store_true", help="show what each file can become")
    mode.add_argument("--extract", "-x", action="store_true", help="extract archives")
    p.add_argument("-o", "--out", metavar="DIR", help="output folder (default: next to each input)")
    p.add_argument("-y", "--yes", action="store_true", help="install missing engines without asking")
    p.add_argument("--plan", action="store_true", help="show each file's route and what it can't keep; convert nothing")
    p.add_argument("--join", action="store_true", help="join sound or video files into one --to file, in the order given")
    p.add_argument("--json", action="store_true", help="one JSON report per file on stdout (route, notes, checks)")
    group = p.add_argument_group("settings", "Each applies only where a conversion takes it; unset ones keep the "
                                             "defaults. 'convertall presets' lists them all.")
    group.add_argument("--preset", metavar="NAME", help='a named set of settings, e.g. "Smaller for email and web"')
    for st in options.SETTINGS.values():
        group.add_argument(f"--{st.key.replace('_', '-')}", dest=st.key, metavar="|".join(st.choices) or ("TIME" if st.time else "N"),
                           help=f"{st.help} (default: {st.auto})")
    p.add_argument("--version", action="version", version=f"ConvertAll {__version__}")
    args = p.parse_args(argv)

    paths = _expand(args.files)
    missing = [str(x) for x in paths if not x.exists()]
    if missing or not paths:
        print(f"Not found: {', '.join(missing) or ' '.join(args.files)}", file=sys.stderr)
        return 2
    if args.list:
        return cmd_list(paths, args.json)
    try:
        settings = options.preset(args.preset) if args.preset else {}
        settings.update(options.clean({k: getattr(args, k) for k in options.SETTINGS}))
    except KeyError:
        print(f"No preset named '{args.preset}'. Run 'convertall presets' to see them.", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.plan:
        if not args.to:
            print("--plan needs --to FORMAT", file=sys.stderr)
            return 2
        return cmd_plan(paths, normalize(args.to), settings, args.json)
    if args.join and (not args.to or len(paths) < 2):
        print("--join needs --to FORMAT and two files or more", file=sys.stderr)
        return 2
    return cmd_convert(paths, normalize(args.to) if args.to else None, args.extract, args.out, args.yes,
                       settings, args.json, args.join)


if __name__ == "__main__":
    sys.exit(main())
