# PyInstaller build: pyinstaller ConvertAll.spec
# Produces dist/ConvertAll/ with ConvertAll.exe (the window, and the terminal tool behind
# convertall-cli.exe, which packaging/release.py adds).
import re

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ICON = "convertall/assets/icon.ico"
SOURCES = ["**/*.c", "**/*.h", "**/*.pyx", "**/*.pxd", "**/*.pyi"]  # build leftovers, never loaded
DATAS = [("convertall/assets", "convertall/assets")]
for pkg in ("trimesh", "ezdxf", "collada"):  # json schemas, templates, fonts loaded at runtime
    DATAS += collect_data_files(pkg, excludes=SOURCES)
DATAS += collect_data_files("pydicom", excludes=["**/test_files/**", "**/charset_files/**"])
DATAS += collect_data_files("bookport", excludes=SOURCES)  # the fonts its PDFs embed, hyphenation data

# gmsh (STEP/IGES/BREP to a mesh, an 85 MB DLL) isn't shipped: ConvertAll downloads it the first
# time a CAD part needs it (convertall/engines.py)
BINARIES = []

MODULES = (collect_submodules("convertall") + collect_submodules("meshport") + collect_submodules("packport")
           + collect_submodules("bookport") + collect_submodules("pixport")  # pixport.raw loads its decoders by name
           + ["pydicom.pixels.decoders", "jpeg_ls", "lz4.frame", "lz4.block", "backports.zstd", "pycdlib", "pysubs2", "olefile", "vtracer", "brotli"])
# PyMuPDF (AGPL) is only the tests' second opinion on PDFs: ConvertAll reads them with PDFium.
# unittest stays: pyparsing (under ezdxf) imports it, and without it every DXF conversion failed
EXCLUDES = ["tkinter", "pydoc_data", "test", "pymupdf", "fitz", "pymupdf4llm", "pymupdf_layout",
            "onnxruntime", "matplotlib", "IPython", "pytest", "gmsh"]
# scipy comes in only through trimesh, which imports it for things ConvertAll doesn't do (convex
# hulls, smoothing, ray tests): every 3D sample converts to every mesh format to the same bytes
# without it, and the 3D tests pass with it blocked. Leaving it out saves ~60 MB (it brings a
# second copy of OpenBLAS). networkx stays: trimesh writes 3MF with it.
EXCLUDES += ["scipy"]
# Build tools, debuggers and tests that nothing here calls. pygments comes in only for Markdown's
# code highlighting, which no conversion turns on (it does without pygments when it isn't there).
EXCLUDES += ["setuptools", "pkg_resources", "distutils", "pygments", "numpy.f2py", "numpy.testing", "asyncio",
             "pdb", "doctest", "pydoc", "_pyrepl", "lxml.objectify"]

# The window needs QtCore/QtGui/QtWidgets/QtNetwork, QtSvg (SVGs into PDFs and pictures) and the
# qwindows platform plugin. PySide6's hook also ships plugins that drag in Quick/QML (virtual
# keyboard), a PDF reader, image plugins for PDF and SVG, TLS backends with their own OpenSSL,
# and a software OpenGL renderer.
QT_DROP = re.compile(r"""(?ix)
    PySide6[\\/](
        opengl32sw\.dll
      | Qt6(Qml\w*|Quick\w*|Pdf|VirtualKeyboard|OpenGL)\.dll
      | translations[\\/]
      | plugins[\\/](tls|networkinformation|platforminputcontexts|generic|iconengines|styles)[\\/]
      | plugins[\\/]platforms[\\/]q(direct2d|minimal|offscreen)\.dll
      | plugins[\\/]imageformats[\\/]q(pdf|svg|icns|tga|tiff|wbmp)\.dll
    )
    | ^lib(ssl|crypto)-3-x64\.dll$        # OpenSSL copy used only by Qt's TLS plugin
    | ^Cryptodome[\\/]PublicKey[\\/]      # py7zr only needs AES and its hash (AES itself loads Protocol)
""")


def slim(toc):
    return [entry for entry in toc if not QT_DROP.search(entry[0])]


# convertall-cli.exe isn't built here: it's a small native launcher (packaging/launcher, built by
# release.py) that runs ConvertAll.exe --cli on its console, so ConvertAll's code is in the
# download once, not once per program (each PyInstaller exe carries its own copy of it).
# optimize=2 leaves docstrings and asserts out of the code it packs: 15% off the archive inside the exe
gui = Analysis(["packaging/gui_entry.py"], pathex=["."], hiddenimports=MODULES, datas=DATAS,
               binaries=BINARIES, excludes=EXCLUDES, optimize=2)
gui.binaries, gui.datas = slim(gui.binaries), slim(gui.datas)

gui_exe = EXE(PYZ(gui.pure), gui.scripts, [], exclude_binaries=True, name="ConvertAll",
              console=False, icon=ICON, upx=False)

COLLECT(gui_exe, gui.binaries, gui.datas, name="ConvertAll", upx=False)
