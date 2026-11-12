"""packaging/release.py won't zip a build that names this computer's home folder."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _release():
    spec = importlib.util.spec_from_file_location("release", ROOT / "packaging" / "release.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_home_folder_paths_are_found(tmp_path, monkeypatch):
    home = tmp_path / "home" / "someone"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    build = tmp_path / "build"
    (build / "lib").mkdir(parents=True)
    (build / "clean.dll").write_bytes(b"\0" * 64 + b"~\\.cargo\\registry\\src\\x.rs")
    (build / "lib" / "ascii.pyd").write_bytes(b"\0" + str(home).upper().encode() + b"\\.cargo\\x.rs")
    (build / "lib" / "slashes.bin").write_bytes(str(home).replace("\\", "/").encode())
    (build / "wide.exe").write_bytes(str(home).encode("utf-16-le"))
    found = [p.relative_to(build).as_posix() for p in _release().leaks(build)]
    assert found == ["lib/ascii.pyd", "lib/slashes.bin", "wide.exe"]


def test_licenses_folder_has_every_component_and_refuses_one_without_a_license(tmp_path):
    """The zip carries a licenses folder: each bundled component's own license files, the
    standard texts its license needs, and a list of what's inside with where its source is. A
    component with no license text stops the release."""
    rel = _release()
    lic = tmp_path / "MIT.txt"
    lic.write_text("MIT License ...")
    parts = [rel.Component("Pillow", "13.0.0", "MIT-CMU", "https://github.com/python-pillow/Pillow", [lic]),
             rel.Component("x265 (inside pillow-heif)", "", "GPL-2.0-or-later",
                           "https://bitbucket.org/multicoreware/x265_git", [], ["GPL-2.0.txt"])]
    out = tmp_path / "licenses"
    assert rel.write_licenses(out, parts) == []
    listing = (out / "README.txt").read_text(encoding="utf-8")
    assert "Pillow 13.0.0" in listing and "x265" in listing and "GPL-2.0-or-later" in listing
    assert "bitbucket.org/multicoreware/x265_git" in listing
    assert (out / "Pillow" / "MIT.txt").read_text() == "MIT License ..."
    assert (out / "GPL-2.0.txt").read_text(encoding="utf-8").lstrip().startswith("GNU GENERAL PUBLIC LICENSE")
    bare = [rel.Component("mystery", "1.0", "", "", [])]
    assert rel.write_licenses(tmp_path / "again", bare) == ["mystery 1.0"]


BUILT_IN = ["ezdxf", "ezdxf.addons.drawing", "ezdxf.addons.drawing.svg", "ezdxf.recover", "trimesh", "pydicom",
            "py7zr", "openpyxl", "mammoth", "markdown", "markdownify", "bs4", "pysubs2", "fontTools.ttLib", "PIL.Image",
            "pillow_heif", "pypdfium2", "meshport", "bookport", "packport", "vtracer", "pycdlib", "olefile", "puremagic",
            "yaml", "tomli_w", "lxml.etree", "networkx", "lz4.frame", "brotli", "numpy", "PySide6.QtSvg",
            "latheport.converters", "latheport.pdfium", "latheport.qtdraw"]


def test_nothing_the_converters_use_is_left_out_of_the_build():
    """Every library a conversion imports still imports with the modules Latheport.spec leaves out
    blocked, as in the built app. (pyparsing, under ezdxf, imports unittest: with it left out,
    every DXF conversion in the Windows app failed with "No module named 'unittest'".)"""
    import ast
    import subprocess
    import sys
    tree = ast.parse((ROOT / "Latheport.spec").read_text(encoding="utf-8"))
    excluded = []
    for node in ast.walk(tree):
        target = getattr(node, "target", None) or (node.targets[0] if isinstance(node, ast.Assign) else None)
        if isinstance(target, ast.Name) and target.id == "EXCLUDES" and isinstance(node.value, ast.List):
            excluded += [e.value for e in node.value.elts if isinstance(e, ast.Constant)]
    assert "pymupdf" in excluded and "scipy" in excluded  # the spec was read
    code = f"""
import importlib, sys
blocked = {excluded!r}
class Block:
    def find_spec(self, name, path=None, target=None):
        if any(name == b or name.startswith(b + ".") for b in blocked):
            raise ModuleNotFoundError(f"No module named {{name!r}} (left out of the build)")
sys.meta_path.insert(0, Block())
failed = []
for name in {BUILT_IN!r}:
    try:
        importlib.import_module(name)
    except ImportError as exc:
        failed.append(f"{{name}}: {{exc}}")
print(failed)
"""
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, timeout=300, check=False)
    assert p.stdout.strip() == "[]", p.stdout + p.stderr[-500:]


def test_a_program_that_needs_the_visual_cpp_runtime_isnt_shipped(tmp_path):
    """A fresh Windows has no Visual C++ runtime (vcruntime140.dll and the rest) unless something
    installed it. The app carries one in _internal, which only what runs inside Latheport.exe
    finds: an exe at the top of the folder that imports it fails to start there. Python's own
    python.exe imports it; Windows' where.exe doesn't."""
    import shutil
    import sys
    rel = _release()
    app = tmp_path / "app"
    (app / "_internal").mkdir(parents=True)
    shutil.copyfile(Path(sys._base_executable), app / "needs.exe")
    shutil.copyfile(r"C:\Windows\System32\where.exe", app / "fine.exe")
    (app / "_internal" / "vcruntime140.dll").write_bytes(b"MZ")
    assert [(p.name, dll) for p, dll in rel.missing_runtime(app)] == [("needs.exe", "vcruntime140.dll")]
    shutil.copyfile(app / "_internal" / "vcruntime140.dll", app / "vcruntime140.dll")
    assert rel.missing_runtime(app) == []
