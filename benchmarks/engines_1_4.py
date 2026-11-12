"""Before/after numbers for 1.4: which routes need an engine, and how long the conversions that
used Pandoc, ImageMagick and DjVuLibre take, with what they write.

    python benchmarks/engines_1_4.py census
    python benchmarks/engines_1_4.py time <corpus dir> <out dir> [--kinds markup,raw,image,djvu] [--limit N]

`census` counts every route Latheport knows (best route, engines installed) and how many still
work with no engine at all. `time` converts each corpus file in-process through
pipeline.convert, best of `--repeat` runs, keeps every result in <out dir> (so a later run can
be compared against it pixel by pixel or word by word) and writes <out dir>/timings.json.
Runs one conversion at a time, so it stays on one core."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from latheport import engines, pipeline, registry

MARKUP = {".tex": "tex", ".latex": "tex", ".rst": "rst", ".org": "org", ".adoc": "adoc", ".asc": "adoc",
          ".asciidoc": "adoc", ".wiki": "wiki", ".mediawiki": "wiki", ".typ": "typ", ".docbook": "docbook",
          ".dbk": "docbook", ".man": "man", ".textile": "textile"}
RAW = {".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2", ".raf", ".dng", ".pef"}
IMAGE = {".exr", ".hdr", ".xcf", ".jxl"}
DJVU = {".djvu", ".djv"}


def census() -> dict:
    registry._ensure_loaded()
    sources = sorted({e.src for edges in registry._edges.values() for e in edges} - {registry.WILDCARD})
    per_engine: Counter = Counter()
    total = engine_routes = 0
    for src in sources:
        for route in registry.all_routes(src).values():
            total += 1
            used = {e.engine for e in route.edges if e.engine}
            engine_routes += bool(used)
            per_engine.update(used)
    real = engines.available
    engines.available = lambda key: False  # as on a PC with no engine installed
    try:
        free = sum(not r.missing_engines for src in sources for r in registry.all_routes(src).values())
    finally:
        engines.available = real
    return {"routes": total, "need_an_engine": engine_routes, "work_with_no_engine": free,
            "per_engine": dict(per_engine.most_common())}


def jobs(corpus: Path, kinds: set[str], limit: int) -> list[tuple[Path, str]]:
    registry._ensure_loaded()
    found: list[tuple[Path, str]] = []
    seen: Counter = Counter()
    for path in sorted(p for p in corpus.rglob("*") if p.is_file()):
        ext = path.suffix.lower()
        if "markup" in kinds and ext in MARKUP:
            key = MARKUP[ext]
            if key in registry._edges and seen[key] < limit:
                seen[key] += 1
                found += [(path, dst) for dst in ("html", "docx", "md") if dst != key]
        elif "raw" in kinds and ext in RAW and seen["raw"] < limit * 4:
            seen["raw"] += 1
            found.append((path, "png"))
        elif "image" in kinds and ext in IMAGE and seen[ext] < limit:
            seen[ext] += 1
            found.append((path, "png"))
        elif "djvu" in kinds and ext in DJVU and seen["djvu"] < limit:
            seen["djvu"] += 1
            found += [(path, "pdf"), (path, "txt")]
    return found


def time_all(corpus: Path, out: Path, kinds: set[str], limit: int, repeat: int) -> list[dict]:
    rows = []
    out.mkdir(parents=True, exist_ok=True)
    for src, dst in jobs(corpus, kinds, limit):
        rel = src.relative_to(corpus)
        target = out / rel.parent / f"{src.name}.{dst}"
        best, written, error = None, None, None
        for _ in range(repeat):
            tmp = out / "_tmp"
            shutil.rmtree(tmp, ignore_errors=True)
            t = time.perf_counter()
            try:
                written = pipeline.convert(src, dst, tmp)
            except Exception as exc:  # noqa: BLE001 - a failure is a row, not the end of the run
                error = f"{type(exc).__name__}: {exc}"[:300]
                break
            took = time.perf_counter() - t
            best = took if best is None else min(best, took)
        row = {"src": str(rel).replace("\\", "/"), "dst": dst, "seconds": best, "error": error}
        if written and not error:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                shutil.rmtree(target) if target.is_dir() else target.unlink()
            shutil.move(str(written), target)
            row["bytes"] = sum(p.stat().st_size for p in target.rglob("*")) if target.is_dir() else target.stat().st_size
        rows.append(row)
        print(f"{row['src']} -> {dst}: {best if best is None else round(best, 3)} {error or ''}", flush=True)
    shutil.rmtree(out / "_tmp", ignore_errors=True)
    (out / "timings.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["census", "time"])
    ap.add_argument("corpus", nargs="?")
    ap.add_argument("out", nargs="?")
    ap.add_argument("--kinds", default="markup,raw,image,djvu")
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--repeat", type=int, default=3)
    a = ap.parse_args()
    if a.what == "census":
        print(json.dumps(census(), indent=1))
    else:
        time_all(Path(a.corpus), Path(a.out), set(a.kinds.split(",")), a.limit, a.repeat)


if __name__ == "__main__":
    main()
