"""PDF to PDF: pages chosen, ordered and turned, split, made smaller, PDFs joined (bookport's rewrite); and the
Pages window. Results are read back with PDFium, a reader of its own."""

from __future__ import annotations

import io
import json
import os

import pytest
from PIL import Image

from latheport import Report, options, pipeline, registry
from latheport.cli import main
from latheport.errors import ConversionError


def _pdf(path, n: int, title: bytes = b"Hand Made"):
    """A PDF of n pages, each saying its number, a bookmark on every page."""
    objs = {1: b"<< /Type /Catalog /Pages 2 0 R /Outlines 50 0 R >>",
            5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>", 9: b"<< /Title (" + title + b") >>"}
    kids = []
    for i in range(n):
        page, content, mark = 100 + i, 200 + i, 300 + i
        kids.append(b"%d 0 R" % page)
        objs[page] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] /Contents %d 0 R "
                      b"/Resources << /Font << /F1 5 0 R >> >> >>" % content)
        text = b"BT /F1 24 Tf 40 300 Td (Page %d) Tj ET" % (i + 1)
        objs[content] = b"<< /Length %d >>\nstream\n" % len(text) + text + b"\nendstream"
        objs[mark] = (b"<< /Title (Mark %d) /Parent 50 0 R /Dest [%d 0 R /Fit] " % (i + 1, page)
                      + (b"/Prev %d 0 R " % (mark - 1) if i else b"") + (b"/Next %d 0 R " % (mark + 1) if i + 1 < n else b"")
                      + b">>")
    objs[2] = b"<< /Type /Pages /Kids [" + b" ".join(kids) + b"] /Count %d >>" % n
    objs[50] = b"<< /Type /Outlines /First 300 0 R /Last %d 0 R /Count %d >>" % (300 + n - 1, n)
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for num in sorted(objs):
        offsets[num] = len(out)
        out += b"%d 0 obj\n" % num + objs[num] + b"\nendobj\n"
    size = max(objs) + 1
    start = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    for num in range(1, size):
        out += b"%010d 00000 n \n" % offsets[num] if num in offsets else b"0000000000 65535 f \n"
    out += b"trailer\n<< /Size %d /Root 1 0 R /Info 9 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, start)
    path.write_bytes(bytes(out))
    return path


def _read(path):
    """(each page's text, each page's turn, bookmarks as (title, page from 1), title) by PDFium."""
    import pypdfium2 as pdfium
    d = pdfium.PdfDocument(str(path))
    try:
        texts = [d[i].get_textpage().get_text_range().strip() for i in range(len(d))]
        turns = [d[i].get_rotation() for i in range(len(d))]
        toc = [(b.get_title(), b.get_dest().get_index() + 1) for b in d.get_toc()]
        return texts, turns, toc, d.get_metadata_value("Title")
    finally:
        d.close()


# ---- the page list

def test_a_page_list_is_read_and_written_one_way():
    assert options.parse_pages("1-3, 5,7R,4-2") == [(0, 0), (1, 0), (2, 0), (4, 0), (6, 1), (3, 0), (2, 0), (1, 0)]
    assert options.check_pages("1,2,3,5,6u,7u,9l") == "1-3,5,6-7u,9l"
    assert options.SETTINGS["pages"].check(" 3 , 1 ") == "3,1" and not options.lasting({"pages": "1"})
    for bad, words in (("", "one page at least"), ("0", "count from 1"), ("2x", "like 1-3,5,7r"), ("1-200000", "at most")):
        with pytest.raises(ValueError, match=words):
            options.check_pages(bad)


def test_pdf_to_pdf_takes_pages_split_and_compress():
    route = registry.find_route("pdf", "pdf")
    assert {"pages", "split", "compress"} <= set(route.settings) and len(route.edges) == 1
    assert "compress" in options.lasting({"compress": "smaller", "split": 2})  # a habit; a split is for these files


# ---- through the pipeline and the command line

def test_pages_chosen_ordered_and_turned_keep_bookmarks_and_title(tmp_path):
    src = _pdf(tmp_path / "doc.pdf", 5)
    r = Report()
    out = pipeline.convert(src, "pdf", tmp_path / "out", options={"pages": "5,1r,3u,3l"}, report=r)
    texts, turns, toc, title = _read(out)
    assert texts == ["Page 5", "Page 1", "Page 3", "Page 3"] and turns == [0, 90, 180, 270]
    assert toc == [("Mark 1", 2), ("Mark 3", 3), ("Mark 5", 1)] and title == "Hand Made"  # 2 and 4 went with their pages
    checks = {c.name: c for c in r.checks}
    assert checks["Pages"].before == 5 and checks["Pages"].after == 4 and checks["Pages"].state == "expected"
    assert any("Pages chosen, put in order and turned by hand (4 pages)" in n for n in r.notes)


# ---- PostScript and EPS, through bookport.ps (no Ghostscript)

EPS = ("%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 60 440 400 730\n%%EndComments\n"
       "/Helvetica findfont 24 scalefont setfont 72 700 moveto (Hello PostScript) show\n"
       "newpath 72 600 moveto 300 450 lineto 72 450 lineto closepath stroke\n")


def test_ghostscript_is_gone_and_postscript_needs_no_engine():
    from latheport import engines
    assert "ghostscript" not in engines.ENGINES
    for src, dst in (("eps", "pdf"), ("ps", "pdf"), ("pdf", "eps"), ("pdf", "ps"), ("eps", "png")):
        route = registry.find_route(src, dst)
        assert route is not None and not route.missing_engines, (src, dst)


def test_eps_becomes_a_pdf_the_size_of_its_drawing(tmp_path):
    import pypdfium2 as pdfium
    src = tmp_path / "chart.eps"
    src.write_text(EPS, encoding="ascii")
    out = pipeline.convert(src, "pdf", tmp_path / "out")
    d = pdfium.PdfDocument(str(out))
    try:
        assert d[0].get_size() == (340, 290)  # the bounding box, not a sheet of paper
    finally:
        d.close()
    assert _read(out)[0] == ["Hello PostScript"]


def test_pdf_to_eps_and_ps_and_back_keeps_the_words(tmp_path):
    src = _pdf(tmp_path / "doc.pdf", 2)
    eps = pipeline.convert(src, "eps", tmp_path / "eps")
    assert eps.read_bytes().startswith(b"%!PS-Adobe-3.0 EPSF-3.0")
    assert _read(pipeline.convert(eps, "pdf", tmp_path / "back1"))[0] == ["Page 1"]  # EPS is one picture: page 1
    ps = pipeline.convert(src, "ps", tmp_path / "ps")
    assert _read(pipeline.convert(ps, "pdf", tmp_path / "back2"))[0] == ["Page 1", "Page 2"]


def test_a_see_through_page_goes_into_postscript_as_a_picture(tmp_path):
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=200)
    page.draw_rect(pymupdf.Rect(20, 20, 180, 180), color=None, fill=(1, 0, 0), fill_opacity=0.5)
    src = tmp_path / "glass.pdf"
    doc.save(src)
    r = Report()
    out = pipeline.convert(src, "ps", tmp_path / "out", report=r)
    assert any("drawn as a picture" in n for n in r.notes), r.notes
    back = pipeline.convert(out, "pdf", tmp_path / "back")
    import pypdfium2 as pdfium
    d = pdfium.PdfDocument(str(back))
    try:
        rgb = d[0].render(scale=1).to_pil().convert("RGB").getpixel((100, 100))
    finally:
        d.close()
    assert rgb[0] > 240 and 100 < rgb[1] < 160  # red at half strength over white: pink, as PDFium drew it


def test_nothing_set_copies_the_pdf_as_it_is(tmp_path):
    src = _pdf(tmp_path / "doc.pdf", 3)
    out = pipeline.convert(src, "pdf", tmp_path / "out")
    assert out.read_bytes() == src.read_bytes()


def test_split_makes_a_folder_of_parts_named_for_their_pages(tmp_path):
    src = _pdf(tmp_path / "doc.pdf", 5)
    r = Report()
    out = pipeline.convert(src, "pdf", tmp_path / "out", options={"split": 2}, report=r)
    assert out.is_dir() and sorted(p.name for p in out.iterdir()) == ["doc 1-2.pdf", "doc 3-4.pdf", "doc 5-5.pdf"]
    assert _read(out / "doc 3-4.pdf")[0] == ["Page 3", "Page 4"] and _read(out / "doc 3-4.pdf")[2] == [("Mark 3", 1), ("Mark 4", 2)]
    pages = {c.name: c for c in r.checks}["Pages"]
    assert (pages.before, pages.after, pages.state) == (5, 5, "ok")  # every page, across the parts
    each = pipeline.convert(src, "pdf", tmp_path / "each", options={"split": 1})
    assert sorted(p.name for p in each.iterdir()) == [f"doc {i}.pdf" for i in range(1, 6)]


def test_compress_makes_a_scan_smaller_and_says_how(tmp_path):
    pics = [Image.radial_gradient("L").resize((2550, 3300)).convert("RGB") for _ in range(2)]
    buf = io.BytesIO()
    pics[0].save(buf, "PDF", save_all=True, append_images=pics[1:], resolution=300, quality=95)
    src = tmp_path / "scan.pdf"
    src.write_bytes(buf.getvalue())
    r = Report()
    small = pipeline.convert(src, "pdf", tmp_path / "s", options={"compress": "smaller"}, report=r)
    balanced = pipeline.convert(src, "pdf", tmp_path / "b", options={"compress": "balanced"})
    assert small.stat().st_size < balanced.stat().st_size < src.stat().st_size / 2
    assert {c.name: c for c in r.checks}["Pages"].state == "ok"
    assert any("saved again at 150 dpi" in n for n in r.notes)


def test_pdfs_joined_with_a_page_list_across_them(tmp_path):
    a, b = _pdf(tmp_path / "a.pdf", 2), _pdf(tmp_path / "b.pdf", 3, b"Other")
    r = Report()
    out = pipeline.join([a, b], "pdf", tmp_path / "out", report=r)
    texts, _, toc, title = _read(out)
    assert out.name == "a (joined).pdf" and texts == ["Page 1", "Page 2", "Page 1", "Page 2", "Page 3"]
    assert len(toc) == 5 and title == "Hand Made"  # the first one's details
    assert {c.name: c for c in r.checks}["Pages"].after == 5
    out = pipeline.join([a, b], "pdf", tmp_path / "plan", options={"pages": "5,1"})
    assert _read(out)[0] == ["Page 3", "Page 1"]


def test_pages_and_split_from_the_command_line_and_mistakes_in_words(tmp_path, capsys):
    src = _pdf(tmp_path / "doc.pdf", 4)
    assert main([str(src), "--to", "pdf", "--pages", "4,1-2", "-o", str(tmp_path / "o"), "--json"]) == 0
    report = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert report["settings"]["pages"] == "4,1-2" and _read(tmp_path / "o" / "doc.pdf")[0] == ["Page 4", "Page 1", "Page 2"]
    assert main([str(src), "--to", "pdf", "--pages", "9"]) == 1
    assert "There's no page 9: this PDF has 4 pages" in capsys.readouterr().err
    assert main([str(src), "--to", "pdf", "--pages", "nine"]) == 2
    assert "Pages is like 1-3,5,7r" in capsys.readouterr().err
    a, b = _pdf(tmp_path / "a.pdf", 1), _pdf(tmp_path / "b.pdf", 1)
    assert main([str(a), str(b), "--to", "pdf", "--join", "-o", str(tmp_path / "j")]) == 0
    assert len(_read(tmp_path / "j" / "a (joined).pdf")[0]) == 2


def test_a_protected_pdf_is_left_as_it_is(tmp_path, monkeypatch):
    from bookport.errors import BookportError
    from bookport.pdf import rewrite
    src = _pdf(tmp_path / "doc.pdf", 2)

    def refuse(*a, **k):
        raise BookportError("This PDF is protected (a password or permissions): it's left as it is")
    monkeypatch.setattr(rewrite, "edit", refuse)
    with pytest.raises(ConversionError, match="protected"):
        pipeline.convert(src, "pdf", tmp_path / "o", options={"pages": "2"})


# ---- the Pages window

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def window(tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    from latheport import gui

    from .test_gui import _pump  # noqa: F401 - the app and window fixtures live there
    app = QApplication.instance() or QApplication([])
    monkeypatch.setenv("LATHEPORT_PRESETS", str(tmp_path / "presets.json"))
    store = QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat)
    monkeypatch.setattr(gui, "QSettings", lambda *a: store)
    w = gui.Window()
    yield w, app
    w.close()


def _wait_pictures(e, app, n):
    from .test_gui import _pump
    assert _pump(app, lambda: len(e.pictures) >= n, 15)


def test_pages_window_orders_turns_takes_out_and_the_conversion_follows(window, tmp_path):
    from PySide6.QtCore import QModelIndex

    from .test_gui import _pump, run
    w, app = window
    src = _pdf(tmp_path / "doc.pdf", 5)
    w.set_files([src])
    w.picker.set_current("pdf")
    panel = w.options_panel
    assert not panel.edit_row.isHidden() and panel.edit_note.text() == "Choose, order and turn pages"
    assert {"split", "compress"} <= set(panel.widgets)  # format settings in the panel; pages in the window
    w.open_pages([src])
    e = w.editor
    _wait_pictures(e, app, 5)
    assert e.grid.count() == 5 and e.strip.isHidden() and e.detail.text() == "5 pages"
    e.grid.model().moveRows(QModelIndex(), 4, 1, QModelIndex(), 0)  # page 5 dragged to the front
    assert [x[1] for x in e.entries()] == [4, 0, 1, 2, 3] and e.undo_btn.isEnabled()
    e.grid.item(1).setSelected(True)
    e.turn(1)
    e.grid.clearSelection()
    for r in (3, 4):
        e.grid.item(r).setSelected(True)
    e.remove()  # pages 3 and 4 out
    assert [x[1:] for x in e.entries()] == [(4, 0), (0, 1), (1, 0)] and e.detail.text() == "3 of 5 pages"
    e.undo()
    assert e.grid.count() == 5
    e.redo()
    e.grid.selectAll()
    e.remove()  # every page: refused in words
    assert e.grid.count() == 3 and "one page at least" in e.detail.text()
    e.grid.clearSelection()
    e.grid.item(0).setSelected(True)
    e.save_picked()
    assert _pump(app, lambda: e.save_btn.isEnabled(), 15)
    assert _read(tmp_path / "doc pages 5.pdf")[0] == ["Page 5"] and "Saved as doc pages 5.pdf" in e.detail.text()
    e.finish()
    assert w.by_hand[src] == {"pages": "5,1r,2"} and panel.summary() == "3 pages chosen"
    texts, turns, _, _ = _read(run(w)[0])
    assert texts == ["Page 5", "Page 1", "Page 2"] and turns == [0, 90, 0]


def test_pages_window_merges_joined_pdfs_in_one_grid(window, tmp_path):
    from PySide6.QtCore import QModelIndex

    from .test_gui import run
    w, app = window
    a, b = _pdf(tmp_path / "a.pdf", 2), _pdf(tmp_path / "b.pdf", 2)
    w.show()  # the Join box counts only when it's on screen
    w.set_files([a, b])
    w.picker.set_current("pdf")
    assert w.join_box.isVisible()  # PDFs can be joined now
    w.join_box.setChecked(True)
    w.open_pages([a, b])
    e = w.editor
    _wait_pictures(e, app, 4)
    assert e.grid.count() == 4 and e.name.text() == "2 PDFs, joined"
    e.grid.model().moveRows(QModelIndex(), 3, 1, QModelIndex(), 0)  # b's last page first
    e.finish()
    assert w.join_pages == "4,1-3" and w.options_panel.summary() == "4 pages chosen"
    out = run(w)[0]
    assert _read(out)[0] == ["Page 2", "Page 1", "Page 2", "Page 1"] and out.name == "a (joined).pdf"
    w.join_box.setChecked(False)  # the pages counted across both no longer fit
    assert w.join_pages is None and w.options_panel.summary() != "4 pages chosen"


# ---- locked PDFs

def _locked(path, n: int, user_pw: str = ""):
    """A PDF of n pages that may be printed and copied but not edited (user_pw "": it opens with no password)."""
    import pymupdf
    with pymupdf.open() as doc:
        for i in range(n):
            doc.new_page().insert_text((72, 72), f"Page {i + 1}")
        doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw=user_pw,
                 permissions=pymupdf.PDF_PERM_PRINT | pymupdf.PDF_PERM_COPY)
    return path


def test_a_pdf_locked_by_permissions_is_edited_joined_split_and_made_smaller(tmp_path):
    import pymupdf
    a, b = _locked(tmp_path / "a.pdf", 3), _locked(tmp_path / "b.pdf", 2)
    assert _read(pipeline.convert(a, "pdf", tmp_path / "o1", options={"pages": "3,1"}))[0] == ["Page 3", "Page 1"]
    folder = pipeline.convert(a, "pdf", tmp_path / "o2", options={"split": "1"})
    assert len(list(folder.glob("*.pdf"))) == 3
    assert len(_read(pipeline.convert(a, "pdf", tmp_path / "o3", options={"compress": "smaller"}))[0]) == 3
    joined = pipeline.join([a, b], "pdf", tmp_path / "o4")
    assert len(_read(joined)[0]) == 5
    with pymupdf.open(a) as was, pymupdf.open(joined) as now:  # locked again, with the same permissions
        assert b"/Encrypt" in joined.read_bytes() and now.permissions == was.permissions
        assert not now.permissions & pymupdf.PDF_PERM_MODIFY


def test_a_pdf_that_needs_a_password_says_so_not_drm(tmp_path):
    src = _locked(tmp_path / "secret.pdf", 2, user_pw="open")
    for opts in ({"pages": "2"}, {"split": "1"}, {"compress": "smaller"}):
        with pytest.raises(ConversionError, match=r"secret\.pdf is password-protected; Latheport can't open it"):
            pipeline.convert(src, "pdf", tmp_path / "o", options=opts)
