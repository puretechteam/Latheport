"""Build the app and zip it for a GitHub release: dist/ConvertAll-<version>-windows.zip.

    .venv\\Scripts\\python packaging\\release.py
    gh release create v<version> dist\\ConvertAll-<version>-windows.zip --notes-file <notes>

The zip has a licenses folder: every bundled component's license, the standard texts those need
(packaging/licenses), and a list of what's inside with where each one's source is.
"""

import ast
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEXTS = Path(__file__).resolve().parent / "licenses"  # GPL and LGPL, which some components only name
sys.path.insert(0, str(ROOT))
from convertall import __version__

SOURCE = "https://github.com/puretechteam/ConvertAll"
LICENSE_FILES = ("license", "licence", "copying", "notice", "copyright", "authors", "credits")

# parts inside a bundled package that its wheel names no license for: the libraries pillow-heif's
# extension is built with, and Qt, which PySide6 wraps (each package's own files only cover itself)
INSIDE = {
    "pillow-heif": [
        ("libheif (HEIF container, inside pillow-heif)", "LGPL-3.0-or-later", "https://github.com/strukturag/libheif",
         ["LGPL-3.0.txt", "GPL-3.0.txt"]),
        ("libde265 (HEIC decoder, inside pillow-heif)", "LGPL-3.0-or-later", "https://github.com/strukturag/libde265",
         ["LGPL-3.0.txt", "GPL-3.0.txt"]),
        ("x265 (HEIC encoder, inside pillow-heif)", "GPL-2.0-or-later", "https://bitbucket.org/multicoreware/x265_git",
         ["GPL-2.0.txt"]),
        ("libstdc++ (GCC runtime, inside pillow-heif)", "GPL-3.0-or-later WITH GCC-exception-3.1", "https://gcc.gnu.org",
         ["GPL-3.0.txt"]),
    ],
    "pyside6-essentials": [("Qt 6 (inside PySide6)", "LGPL-3.0-only", "https://download.qt.io/official_releases/qt/",
                            ["LGPL-3.0.txt", "GPL-3.0.txt"])],
}
# license files kept here for packages that ship none (cobble names its license only in its README)
OWN = {"cobble": ["cobble-LICENSE.txt"]}
# standard texts for licenses a package names in its metadata without shipping the text
NAMED = {"LGPL-3.0": ["LGPL-3.0.txt", "GPL-3.0.txt"], "LGPL-2.1": ["LGPL-2.1.txt"], "LGPLv2": ["LGPL-2.1.txt"],
         "GPL-2.0": ["GPL-2.0.txt"], "GPLv2": ["GPL-2.0.txt"], "GPL-3.0": ["GPL-3.0.txt"]}


@dataclass
class Component:
    name: str
    version: str
    license: str
    source: str
    files: list[Path]  # its own license files
    texts: list[str] = field(default_factory=list)  # standard texts from packaging/licenses
    base: Path | None = None  # where its files are kept: copied with their paths below it (pypdfium2 has dozens)


def leaks(folder: Path) -> list[Path]:
    """Files in folder that name this machine's home folder (a build path compiled into a
    binary, say): a release mustn't carry it."""
    home = str(Path.home()).lower()
    needles = [form.encode(enc) for form in {home, home.replace("\\", "/")} for enc in ("utf-8", "utf-16-le")]
    return [p for p in sorted(folder.rglob("*")) if p.is_file() and any(n in p.read_bytes().lower() for n in needles)]


# installed by the Visual C++ Redistributable (or another program), never by Windows itself
VC_RUNTIME = {"vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll",
              "concrt140.dll", "vcomp140.dll"}


def missing_runtime(folder: Path) -> list[tuple[Path, str]]:
    """Programs at the top of folder that import a Visual C++ runtime DLL that isn't beside them.
    On a computer without the redistributable they don't start ("VCRUNTIME140.dll was not
    found"): the copy in _internal is found only by what runs inside ConvertAll.exe."""
    import pefile
    found = []
    for exe in sorted(folder.glob("*.exe")):
        pe = pefile.PE(str(exe), fast_load=True)
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
                                               pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"]])
        entries = getattr(pe, "DIRECTORY_ENTRY_IMPORT", []) + getattr(pe, "DIRECTORY_ENTRY_DELAY_IMPORT", [])
        pe.close()
        for dll in sorted({e.dll.decode().lower() for e in entries} & VC_RUNTIME):
            if not (folder / dll).exists():
                found.append((exe, dll))
    return found


def _top_names(build: Path, internal: Path) -> set[str]:
    """Top-level import names in the build: the window exe's Python code (its PYZ table of
    contents) and the folders and extension files beside it."""
    names = set()
    for toc in build.glob("PYZ-*.toc"):
        entries = ast.literal_eval(toc.read_text(encoding="utf-8"))
        for entry in entries[1] if isinstance(entries, (tuple, list)) and len(entries) > 1 else []:
            if isinstance(entry, (tuple, list)) and entry:
                names.add(str(entry[0]).split(".")[0])
    for p in internal.iterdir():
        names.add(p.name.split(".")[0])
    return names


def bundled(build: Path, internal: Path) -> list[Component]:
    """Every installed distribution with code in the build, and the parts inside them."""
    tops = _top_names(build, internal)
    owners = metadata.packages_distributions()
    dists = {d for name in tops for d in owners.get(name, [])} - {"convertall"}  # the app: LICENSE.txt
    out = []
    present = {p.name.lower() for p in internal.rglob("*") if p.suffix.lower() in (".pyd", ".dll")}
    for name in sorted(dists, key=str.lower):
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        binaries = {Path(str(f)).name.lower() for f in dist.files or [] if Path(str(f)).suffix.lower() in (".pyd", ".dll")}
        if binaries and not binaries & present:
            continue  # it shares a top-level name with what's built in (PySide6_Addons), but none of its files are
        meta = dist.metadata
        stated = meta.get("License-Expression") or meta.get("License") or ""
        lic = stated.splitlines()[0][:80] if stated.strip() else ""
        labelled = [(u.split(",", 1)[0].strip().lower(), u.split(",", 1)[-1].strip())
                    for u in meta.get_all("Project-URL") or []]
        source = (next((u for label, u in labelled if label in ("source", "source code", "repository", "code")), None)
                  or next((u for _, u in labelled if "github.com" in u and "/issues" not in u), None)
                  or meta.get("Home-page") or next((u for label, u in labelled if label == "homepage"), ""))
        files = [Path(dist.locate_file(f)) for f in dist.files or [] if _is_license(str(f))
                 and Path(dist.locate_file(f)).is_file()]
        info = [f for f in dist.files or [] if ".dist-info/" in str(f).replace("\\", "/")]
        base = Path(dist.locate_file(info[0])).parent if info else None
        if not files:  # an editable install (a library being worked on): its project folder's LICENSE
            import json
            from urllib.parse import unquote, urlparse
            url = json.loads(dist.read_text("direct_url.json") or "{}").get("url", "")
            if url.startswith("file:"):
                project = Path(unquote(urlparse(url).path).lstrip("/"))
                files = [p for p in (project / "LICENSE", project / "LICENSE.txt") if p.is_file()][:1]
        if not files:
            files = [TEXTS / f for f in OWN.get(meta["Name"].lower(), [])]
        texts = sorted({t for key, names in NAMED.items() if key in lic for t in names})
        out.append(Component(meta["Name"], _version(dist, meta), lic, source, files, texts, base))
        for part, plic, psource, ptexts in INSIDE.get(meta["Name"].lower().replace("_", "-"), []):
            out.append(Component(part, "", plic, psource, [], ptexts))
    python = Path(sys.base_prefix) / "LICENSE.txt"
    out.append(Component("Python", sys.version.split()[0], "PSF-2.0", "https://www.python.org",
                         [python] if python.is_file() else []))
    return out


def _is_license(path: str) -> bool:
    """A license file: named so, or kept in a folder of licenses (dist-info/licenses/...)."""
    parts = [p.lower() for p in path.replace("\\", "/").split("/")]
    if parts[-1].endswith((".py", ".pyc", ".pyd", ".dll", ".so")):
        return False  # code (packaging has a module named licenses)
    return parts[-1].startswith(LICENSE_FILES) or any("licens" in p for p in parts[:-1] if not p.endswith(".dist-info"))


def _version(dist, meta) -> str:
    """The version of what's built in: an editable install's metadata can lag behind its code."""
    import importlib
    try:
        top = (dist.read_text("top_level.txt") or "").split()[0]
        return str(getattr(importlib.import_module(top), "__version__", "")) or meta["Version"]
    except Exception:  # noqa: BLE001 - no top-level module, or one that won't import here
        return meta["Version"]


def write_licenses(dest: Path, parts: list[Component]) -> list[str]:
    """The licenses folder; returns the components it has no license text for (nothing written then)."""
    missing = [f"{c.name} {c.version}".strip() for c in parts if not c.files and not c.texts]
    if missing:
        return missing
    dest.mkdir(parents=True)
    lines = ["Third-party components in this download of ConvertAll",
             "",
             f"ConvertAll's own code is under the MIT license (LICENSE.txt); its source is at {SOURCE}.",
             "This download also contains the components below, each under its own license: their license",
             "files are in this folder. x265 (inside pillow-heif, used to save HEIC pictures) is licensed under the",
             "GNU GPL version 2 or later, so this download as a whole is distributed under the GPL's terms. Each",
             "component's source is at the address listed with it.",
             "gmsh (for STEP, IGES and BREP parts) isn't included: ConvertAll downloads it from PyPI the first time",
             "it's needed, under its own license (GPL-2.0-or-later).",
             ""]
    for c in parts:
        folder = dest / c.name.split(" (")[0].replace("/", "_")
        for f in c.files:
            try:
                rel = f.relative_to(c.base) if c.base else Path(f.name)
            except ValueError:
                rel = Path(f.name)
            (folder / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, folder / rel)
        for t in c.texts:
            shutil.copyfile(TEXTS / t, dest / t)
        where = [f"{folder.name}/" if c.files else "", *c.texts]
        lines.append(f"{c.name} {c.version}".strip() + f"  |  {c.license or 'see its license file'}  |  {c.source}"
                     f"  |  {', '.join(w for w in where if w)}")
    (dest / "README.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return []


def build_launcher() -> Path:
    """convertall-cli.exe (packaging/launcher, Rust): build paths in it are made relative, and the
    C runtime is linked in, so it starts on a computer without the Visual C++ redistributable."""
    import os
    env = dict(os.environ, RUSTFLAGS=f"--remap-path-prefix={Path.home()}=~ -C target-feature=+crt-static")
    manifest = ROOT / "packaging" / "launcher" / "Cargo.toml"
    subprocess.run(["cargo", "build", "--release", "--manifest-path", str(manifest)], env=env, check=True)
    return manifest.parent / "target" / "release" / "convertall-cli.exe"


def main() -> int:
    if shutil.which("cargo") is None:
        print("Not built: convertall-cli.exe needs Rust's cargo (https://rustup.rs)")
        return 1
    launcher = build_launcher()
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "ConvertAll.spec"], cwd=ROOT, check=True)
    app = ROOT / "dist" / "ConvertAll"
    shutil.copyfile(launcher, app / "convertall-cli.exe")
    if found := leaks(app):
        print("Not zipped: these files name this computer's home folder (rebuild them without it):")
        for p in found:
            print(" ", p.relative_to(ROOT / "dist"))
        return 1
    if found := missing_runtime(app):
        print("Not zipped: these need a Visual C++ runtime DLL a fresh Windows doesn't have:")
        for exe, dll in found:
            print(f"  {exe.name}: {dll}")
        return 1
    shutil.copyfile(ROOT / "LICENSE", app / "LICENSE.txt")
    if missing := write_licenses(app / "licenses", bundled(ROOT / "build" / "ConvertAll", app / "_internal")):
        print("Not zipped: no license text found for", ", ".join(missing))
        return 1
    base = ROOT / "dist" / f"ConvertAll-{__version__}-windows"
    # the zip holds one ConvertAll folder with ConvertAll.exe, convertall-cli.exe, _internal and
    # the licenses; deflate at its strongest
    import zipfile
    zip_path = base.parent / (base.name + ".zip")  # not with_suffix: "1.4.0-windows" has a dot in it
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in sorted(app.rglob("*")):
            z.write(p, p.relative_to(app.parent).as_posix())
    print(zip_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
