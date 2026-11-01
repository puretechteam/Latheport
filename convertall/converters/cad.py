"""CAD: STEP/IGES/BREP solids -> STL mesh via gmsh (OpenCASCADE); DXF drawings -> SVG via ezdxf (and
on to PDF and PNG with Qt), and SVG/PDF vector drawings -> DXF (PDFium reads their shapes)."""

import shutil
import threading
from pathlib import Path

from ..errors import ConversionError
from ..losses import loss
from ..registry import converter, option

# gmsh is one global model per process: the window's 3D preview and a conversion of the same
# part can run at the same time, on two threads, so they take turns.
_GMSH = threading.Lock()
# the detail setting: (largest triangle as a share of the part's size, curvature steps per circle)
DETAIL = {"coarse": (20, 12), "normal": (50, 24), "fine": (150, 48)}


@converter(inputs={"step", "iges", "brep"}, outputs={"stl"}, engine="gmsh", options=("detail",))
def cad_to_stl(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
    with _GMSH:
        _mesh_cad(src, dst, src_fmt)


def _mesh_cad(src: Path, dst: Path, src_fmt: str) -> None:
    from .. import engines
    gmsh = engines.load_gmsh()  # downloaded the first time it's needed (see engines.py)
    # gmsh's file reader can trip over non-ASCII paths on Windows, so work on a plain-named copy
    local = dst.parent / f"input.{src_fmt}"
    shutil.copyfile(src, local)
    gmsh.initialize(interruptible=False)  # no signal handlers: we may be on a worker thread
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        # IGES (and some STEP exports) store loose faces: sew them into closed solids
        gmsh.option.setNumber("Geometry.OCCSewFaces", 1)
        gmsh.option.setNumber("Geometry.OCCMakeSolids", 1)
        gmsh.model.add("part")
        gmsh.model.occ.importShapes(str(local))
        gmsh.model.occ.synchronize()
        if not gmsh.model.getEntities(3):
            # surfaces only (typical IGES): make neighbouring faces share edges -> watertight mesh.
            # Not done for solids, where it would weld touching parts of an assembly together.
            gmsh.model.occ.removeAllDuplicates()
            gmsh.model.occ.synchronize()
        if not gmsh.model.getEntities(2):
            raise ConversionError("No surfaces found in this CAD file")
        x0, y0, z0, x1, y1, z1 = gmsh.model.getBoundingBox(-1, -1)
        diag = ((x1 - x0) ** 2 + (y1 - y0) ** 2 + (z1 - z0) ** 2) ** 0.5 or 1.0
        # fine enough for 3D printing: follow curvature, cap triangle size relative to the part
        share, steps = DETAIL.get(option("detail", "normal"), DETAIL["normal"])
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", steps)
        gmsh.option.setNumber("Mesh.MeshSizeMax", diag / share)
        gmsh.option.setNumber("Mesh.MeshSizeMin", diag / 5000)
        gmsh.option.setNumber("Mesh.Binary", 1)
        gmsh.option.setNumber("Mesh.StlOneSolidPerSurface", 0)
        gmsh.model.mesh.generate(2)
        gmsh.write(str(dst))
    except Exception as exc:
        if isinstance(exc, ConversionError):
            raise
        raise ConversionError(f"Couldn't mesh this CAD file: {exc}") from exc
    finally:
        gmsh.finalize()


@converter(inputs={"dxf"}, outputs={"svg", "png"})
@converter(inputs={"dxf"}, outputs={"pdf"}, terminal=True)  # a drawing PDF has no text worth extracting
def dxf_render(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
    from ezdxf import recover
    from ezdxf.addons.drawing import Frontend, RenderContext, config, layout
    try:
        doc, _auditor = recover.readfile(str(src))
    except Exception as exc:
        raise ConversionError(f"Couldn't read this DXF: {exc}") from exc
    cfg = config.Configuration(background_policy=config.BackgroundPolicy.WHITE,
                               color_policy=config.ColorPolicy.COLOR)
    # auto-size to the drawing, but cap at A2 so huge-unit drawings still make sane pages/images
    page = layout.Page(0, 0, layout.Units.mm, margins=layout.Margins.all(10), max_width=594, max_height=594)
    from ezdxf.addons.drawing.svg import SVGBackend
    backend = SVGBackend()
    Frontend(RenderContext(doc), backend, config=cfg).draw_layout(doc.modelspace())
    svg = backend.get_string(page)
    if dst_fmt == "svg":
        dst.write_text(svg, encoding="utf-8")
        return
    from .. import qtdraw
    if dst_fmt == "pdf":  # vector, at the drawing's own size in millimetres
        qtdraw.svg_to_pdf(svg.encode("utf-8"), dst, src.name)
    else:
        qtdraw.svg_to_image(svg.encode("utf-8"), name=src.name, dpi=200, background="white").convert("RGB").save(dst, "PNG")


# ---- SVG / PDF vectors -> DXF (laser cutters, CNC, CAD): every stroked or filled path becomes a
# polyline in millimetres, curves flattened finely; text becomes TEXT entities. Each page sits
# below the previous one on its own layer.

PT_TO_MM = 25.4 / 72


@converter(inputs={"svg", "pdf"}, outputs={"dxf"}, terminal=True,
           notes=lambda s, d, o: [loss("Shapes become outlines (fills and pictures are left out) and curves "
                                       "become short straight lines", weight=0)])
def vectors_to_dxf(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
    import ezdxf
    from ezdxf import colors

    from .. import pdfium
    doc = ezdxf.new(units=4)  # millimetres
    msp = doc.modelspace()
    count, offset = 0, 0.0
    if src_fmt == "svg":  # drawn into a PDF first (Qt), then read like one
        from .. import qtdraw
        tmp = dst.parent / "_drawing.pdf"
        qtdraw.svg_to_pdf(qtdraw.svg_bytes(src), tmp, src.name)
        src = tmp
    with pdfium.opened(src) as pdf:
        for page_no in range(1, len(pdf) + 1):
            page = pdf[page_no - 1]
            layer = f"Page {page_no}" if len(pdf) > 1 else "0"
            if layer != "0":
                doc.layers.add(layer)
            height = page.get_height()

            def xy(p, off=offset):  # PDF and DXF both have y going up; pages stack downwards
                return (p[0] * PT_TO_MM, p[1] * PT_TO_MM - off)

            for shape in pdfium.shapes(page):
                if shape["kind"] == "path":
                    color = shape["stroke"] or shape["fill"]
                    attribs = {"layer": layer}
                    if color:
                        attribs["true_color"] = colors.rgb2int(color[:3])
                    for pts, closed in shape["runs"]:
                        msp.add_lwpolyline([xy(q) for q in pts], close=closed, dxfattribs=attribs)
                        count += 1
                elif shape["kind"] == "text" and shape["text"].strip():
                    msp.add_text(shape["text"], height=shape["size"] * PT_TO_MM * 0.7,
                                 dxfattribs={"layer": layer, "insert": xy(shape["origin"])})
                    count += 1
            offset += height * PT_TO_MM + 20
    if not count:
        raise ConversionError("No vector shapes or text found (a scanned page or photo has none to trace)")
    doc.saveas(dst)
