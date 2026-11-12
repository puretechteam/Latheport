"""The window, driven offscreen: drop files, check what's preselected, convert."""

import gc
import itertools
import os
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtCore = pytest.importorskip("PySide6.QtCore")
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from latheport import gui
from latheport.editor import playback

playback.SOUND = False  # no speakers needed (or heard) while videos play in the tests

from .conftest import needs
from .samples import Samples


@pytest.fixture(scope="module")
def app():
    from latheport.qtdraw import app as qt_app
    return qt_app()  # kept for the whole session: a Qt application made twice has no fonts


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    settings = QtCore.QSettings(str(tmp_path / "settings.ini"), QtCore.QSettings.IniFormat)
    monkeypatch.setattr(gui, "QSettings", lambda *a: settings)  # a new user, every time
    # earlier tests' windows go now, between tests, not whenever the collector next runs mid-test
    # (a test that swaps in its own run_job makes a cycle only the collector frees)
    gc.collect()
    win = gui.Window()
    yield win
    if win.job is not None:  # a test that failed mid-conversion: stop it without asking, and wait
        win.confirm_stop = lambda: True
        win.close()
        _pump(app, lambda: win.job is None)
    win.close()
    wait_for_previews(app)


def wait_for_previews(app):
    """Dropping a 3D model starts loading its preview on a thread: let it finish."""
    import sys
    viewer = sys.modules.get("latheport.viewer")
    for job in list(viewer._RUNNING) if viewer else []:
        job.wait()
    app.processEvents()


@pytest.fixture
def samples(tmp_path):
    return Samples(tmp_path / "samples")


def run(win):
    """Click Convert, but run the job on this thread so the test can wait for it."""
    jobs = []
    win.run_job = lambda job: (jobs.append(job), job.done.connect(win.job_done), job.run())
    win.start_convert()
    assert jobs, "nothing was started"
    return win.outputs


def test_a_closed_window_is_freed(app, tmp_path, monkeypatch, samples):
    """Nothing may keep the window alive once it's closed and dropped (a lambda holding it on a child's
    signal did): Python then destroyed it at exit, after Qt, and crashed."""
    import gc
    settings = QtCore.QSettings(str(tmp_path / "settings.ini"), QtCore.QSettings.IniFormat)
    monkeypatch.setattr(gui, "QSettings", lambda *a: settings)
    win = gui.Window()
    win.set_files([samples("png")])
    win.picker.set_current("webp")
    run(win)
    win.close()
    app.processEvents()
    del win
    gc.collect()
    assert not [o for o in gc.get_objects() if isinstance(o, gui.Window)]


@pytest.mark.parametrize("fmt,expected", [
    ("cbz", "pdf"), ("cbt", "pdf"), ("png", "jpg"), ("jpg", "png"), ("heic", "jpg"), ("webp", "png"),
    ("json", "csv"), ("srt", "vtt"), ("ttf", "woff2"), ("step", "stl"), ("dcm", "jpg"), ("zip", "7z"),
    ("folder", "zip"), ("epub", "pdf"),
])
def test_new_user_gets_the_most_common_conversion(window, samples, fmt, expected):
    window.set_files([samples(fmt)])
    assert window.picker.current() == expected


@needs("ffmpeg")
def test_video_defaults_to_mp3(window, samples):
    window.set_files([samples("mp4")])
    assert window.picker.current() == "mp3"


def test_comic_to_pdf_from_the_window(window, samples):
    import pymupdf
    window.set_files([samples("cbz")])
    out, = run(window)
    assert out.suffix == ".pdf" and window.status.property("state") == "ok"
    with pymupdf.open(out) as doc:
        assert doc.page_count == 4


def test_remembers_what_this_user_picked(window, samples):
    png = samples("png")
    window.set_files([png])
    window.picker.set_current("webp")
    out, = run(window)
    assert out.suffix == ".webp"
    window.set_files([png])
    assert window.picker.current() == "webp"
    window.set_files([samples("jpg")])  # another kind of file still gets the common pick
    assert window.picker.current() == "png"


def test_mixed_batch_offers_only_shared_targets(window, samples):
    window.set_files([samples("png"), samples("cbz")])
    offered = set(window.picker.formats())
    assert {"pdf", "zip"} <= offered and "jpg" not in offered


def test_errors_are_shown_not_raised(window, tmp_path):
    bad = tmp_path / "broken.cbz"
    bad.write_bytes(b"PK\x03\x04 not really a zip")
    window.set_files([bad])
    assert window.picker.current() == "pdf"
    assert run(window) == []
    assert window.status.property("state") == "error" and "broken.cbz" in window.status.text()


# ---- the format panel


def _open(window, samples, fmt="jpg"):
    window.set_files([samples(fmt)])
    window.picker.click()
    popup = window.picker.popup
    assert popup is not None and popup.isVisible()
    return popup


def _chip_formats(popup):
    return [c.property("fmt") for c in popup.chips]


def test_panel_groups_formats_and_suggests_first(window, samples):
    popup = _open(window, samples)
    fmts = _chip_formats(popup)
    assert fmts[0] == "png"  # the Suggested row comes first
    assert sorted(set(fmts)) == sorted(window.picker.formats())
    selected = [c.property("fmt") for c in popup.chips if c.property("selected")]
    assert set(selected) == {"png"}


def test_search_filters_and_enter_picks(window, samples):
    popup = _open(window, samples)
    popup.search.setText(".web")
    assert _chip_formats(popup) == ["webp", "webm"] or set(_chip_formats(popup)) <= {"webp", "webm"}
    from PySide6.QtTest import QTest
    QTest.keyClick(popup.search, QtCore.Qt.Key_Return)
    assert window.picker.current() == "webp" and not popup.isVisible()


def test_search_by_full_name(window, samples):
    popup = _open(window, samples)
    popup.search.setText("bitmap")
    assert "bmp" in _chip_formats(popup)


def test_arrow_keys_move_through_the_grid(window, samples):
    from PySide6.QtTest import QTest
    popup = _open(window, samples)
    QTest.keyClick(popup.search, QtCore.Qt.Key_Down)
    assert popup.focusWidget() is popup.chips[0] or QApplication.focusWidget() is popup.chips[0]
    QTest.keyClick(popup.chips[0], QtCore.Qt.Key_Down)  # one row down = COLUMNS chips on
    QTest.keyClick(popup.chips[gui.FormatPopup.COLUMNS], QtCore.Qt.Key_Return)
    assert window.picker.current() == popup.chips[gui.FormatPopup.COLUMNS].property("fmt")


def test_formats_needing_an_install_are_marked(window, samples, monkeypatch):
    real = gui.engines.find
    monkeypatch.setattr(gui.engines, "find", lambda k: None if k == "libreoffice" else real(k))
    gui.engines.refresh()
    popup = _open(window, samples, "pdf")
    doc = next(c for c in popup.chips if c.property("fmt") == "doc")
    assert doc.property("needs") is True and "LibreOffice" in doc.toolTip()
    assert any(lbl.objectName() == "legend" for lbl in popup.findChildren(gui.QLabel))


def test_no_conversions_disables_the_picker(window, tmp_path):
    odd = tmp_path / "mystery.qqq"
    odd.write_bytes(b"\x00\x01 nothing anyone knows")
    window.set_files([odd])
    if not window.routes:
        assert not window.picker.isEnabled() and window.picker.current() is None


# ---- right-click "Extract with Latheport"


def test_forwarded_message_carries_what_to_do(tmp_path):
    a, b = tmp_path / "a.zip", tmp_path / "b.rar"
    assert gui.parse_message(f"{gui.EXTRACT_MARKER}\n{a}\n{b}\n") == ([a, b], "extract")
    assert gui.parse_message(f"{gui.VIEW_MARKER}\n{a}\n") == ([a], "view")
    assert gui.parse_message(f"{a}\n") == ([a], "convert")
    assert gui.parse_message("") == ([], "convert")


def test_a_long_forwarded_file_list_arrives_whole(app, tmp_path):
    """A second Latheport process hands its files to the open window, as Explorer causes."""
    import json
    import subprocess
    import sys
    import time

    from PySide6.QtNetwork import QLocalServer
    name = f"Latheport-test-{os.getpid()}"
    server = QLocalServer()
    QLocalServer.removeServer(name)
    assert server.listen(name)
    got = []
    gui.listen(server, got.append)
    files = [str(tmp_path / f"file {i:05d} with a longish name é.txt") for i in range(5000)]  # ~400 KB
    (tmp_path / "files.json").write_text(json.dumps(files), encoding="utf-8")
    script = ("import json, sys; from latheport import instance; "  # no QApplication: the launcher has none
              "files = json.load(open(sys.argv[1], encoding='utf-8')); "
              "sys.exit(0 if instance.forward(files, 'extract', sys.argv[2]) else 1)")
    sender = subprocess.Popen([sys.executable, "-c", script, str(tmp_path / "files.json"), name],
                              env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
    deadline = time.monotonic() + 30
    while not got and time.monotonic() < deadline:
        app.processEvents()
    assert sender.wait(10) == 0
    server.close()
    assert gui.parse_message(got[0]) == ([tmp_path / os.path.basename(f) for f in files], "extract")


def _run_now(win):
    """Jobs run on this thread so the test can wait for them."""
    win.run_job = lambda job: (setattr(win, "job", job), job.done.connect(win.job_done), job.run())


def test_right_clicked_archives_extract_as_one_batch(window, samples, tmp_path):
    from latheport.converters import archives
    zips = []
    for name in ("first", "second"):
        z = tmp_path / f"{name}.zip"
        archives.compress(samples("folder"), z, "zip")
        zips.append(z)
    _run_now(window)
    for z in zips:  # Explorer starts one process per file; each forwards its file
        window.add_files_from_other_instance([z], extract=True)
    assert window.extract_timer.isActive() and window.pending_extract == zips
    window.extract_timer.stop()
    window.extract_pending()
    assert len(window.outputs) == 2 and all(o.is_dir() for o in window.outputs)
    assert window.status.text() == "Done · 2 files"


def test_archives_arriving_mid_extraction_wait_their_turn(window, samples, tmp_path):
    window.job = object()  # something is running
    window.add_files_from_other_instance([samples("zip")], extract=True)
    window.extract_timer.stop()
    window.extract_pending()
    assert window.pending_extract  # kept for later
    window.job = None
    _run_now(window)
    window.job_done([], [])
    assert window.extract_timer.isActive()


def test_extract_refuses_what_isnt_an_archive(window, samples):
    window.add_files_from_other_instance([samples("png")], extract=True)
    window.extract_timer.stop()
    window.extract_pending()
    assert window.status.property("state") == "error" and "isn't an archive" in window.status.text()


def test_extract_menu_covers_archives_but_not_comics():
    exts = gui.extract_extensions()
    assert {"zip", "rar", "7z", "gz", "tgz", "iso", "jar", "001"} <= set(exts)
    assert not {"cbz", "cbr", "txt", "docx", "chm", "lit"} & set(exts)


@pytest.mark.skipif(not gui.sys.platform.startswith("win"), reason="Windows registry")
def test_registers_extract_on_archives_and_convert_elsewhere(monkeypatch):
    import winreg
    root = r"Software\Latheport-test"
    monkeypatch.setattr(gui, "_STAR_KEY", rf"{root}\*\shell\Latheport")
    monkeypatch.setattr(gui, "_DIR_KEY", rf"{root}\Directory\shell\Latheport")
    monkeypatch.setattr(gui, "_SFA", rf"{root}\SystemFileAssociations")
    monkeypatch.setattr(gui, "extract_extensions", lambda: ["zip", "rar"])
    monkeypatch.setattr(gui, "view_extensions", lambda: ["glb"])

    def value(key, name=""):
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            return winreg.QueryValueEx(k, name)[0]
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"{root}\SystemFileAssociations\.zip\shell\Other"):
            pass  # another app's verb on .zip: must survive our removal
        gui.install_context_menu()
        assert value(gui._STAR_KEY, "MUIVerb") == "Convert with Latheport"
        assert value(gui._STAR_KEY, "Version") == gui.__version__
        zip_verb = rf"{root}\SystemFileAssociations\.zip\shell\Latheport"
        assert value(zip_verb, "MUIVerb") == "Extract with Latheport"
        assert "--extract" in value(zip_verb + r"\command")
        view_verb = rf"{root}\SystemFileAssociations\.glb\shell\LatheportView"
        assert value(view_verb, "MUIVerb") == "View with Latheport"
        assert value(view_verb, "MultiSelectModel") == "Single"
        assert "--view" in value(view_verb + r"\command")
        assert not gui.context_menu_outdated()
        real = gui._launch_command
        monkeypatch.setattr(gui, "_launch_command", lambda *f: real(*f).replace("latheport", "moved"))
        assert gui.context_menu_outdated()  # the same version from another folder takes the menu over
        monkeypatch.setattr(gui, "__version__", "0.9.0")
        assert not gui.context_menu_outdated()  # ...but an older copy never does
        monkeypatch.setattr(gui, "__version__", "99.0.0")
        assert gui.context_menu_outdated()
        gui.remove_context_menu()
        assert not gui.context_menu_installed()
        with pytest.raises(OSError):
            winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, zip_verb))
        with pytest.raises(OSError):  # emptied keys we created are gone
            winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"{root}\SystemFileAssociations\.rar"))
        with pytest.raises(OSError):
            winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"{root}\SystemFileAssociations\.glb"))
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"{root}\SystemFileAssociations\.zip\shell\Other"))
    finally:
        import subprocess
        subprocess.run(["reg", "delete", rf"HKCU\{root}", "/f"], capture_output=True, check=False)


# ---- 3D preview and viewer


def _preview(window, app, path):
    window.set_files([path])
    wait_for_previews(app)
    return window.drop.preview


def test_dropping_a_model_previews_it(window, app, samples):
    panel = _preview(window, app, samples("glb"))
    assert panel is not None and not panel.isHidden() and panel.model is not None
    assert panel.model.triangle_count == 92 and "92 triangles" in window.drop.sub.text()
    assert not window.drop.expand_btn.isHidden()
    window.set_files([samples("png")])  # not a model: the preview goes away
    assert panel.isHidden() and panel.model is None
    assert window.drop.sub.text().startswith(gui.describe("png") + " · ")  # and its size


def test_cad_parts_preview_too(window, app, samples):
    panel = _preview(window, app, samples("step"))
    assert panel.model is not None and panel.model.units == ""


def test_several_files_get_no_preview(window, app, samples):
    window.set_files([samples("glb"), samples("stl")])
    assert window.drop.preview is None or window.drop.preview.isHidden()


def test_a_broken_model_says_so_in_the_preview(window, app, tmp_path):
    bad = tmp_path / "broken.glb"
    bad.write_bytes(b"glTF\x02\x00\x00\x00 cut short")
    panel = _preview(window, app, bad)
    assert panel.model is None and panel.message.text() and window.drop.expand_btn.isHidden()


def test_expanding_reuses_the_loaded_model(window, app, samples):
    from latheport import viewer
    panel = _preview(window, app, samples("glb"))
    window.drop.expand_btn.click()
    big = viewer._OPEN[-1]
    try:
        assert big.panel.model is panel.model and big.panel.job is None
        assert big.stats.text().startswith("2 parts · 92 triangles") and big.wire.isEnabled()
    finally:
        big.close()


def test_view_with_latheport_opens_models_and_passes_the_rest_on(window, app, samples):
    from latheport import viewer
    before = len(viewer._OPEN)
    window.open_viewers([samples("stl"), samples("png")])
    try:
        assert len(viewer._OPEN) == before + 1
        assert window.files == [samples("png")]  # not a model: opened for converting
    finally:
        wait_for_previews(app)
        viewer._OPEN[-1].close()


def test_view_menu_covers_models_and_cad():
    exts = gui.view_extensions()
    assert {"glb", "fbx", "stl", "obj", "3mf", "usdz", "blend", "step", "stp", "igs"} <= set(exts)
    assert not {"dxf", "png", "zip"} & set(exts)


def test_menu_offers_discord_and_about_links_to_it(window):
    menu_btn = next(b for b in window.findChildren(gui.IconButton) if b.icon == "menu")
    texts = [a.text() for a in menu_btn.menu().actions()]
    assert "Help and chat on Discord" in texts and gui.DISCORD == "https://discord.gg/dRMRGCG9Fk"
    box = window.about()
    try:
        assert gui.DISCORD in box.text()
        assert any(lbl.openExternalLinks() for lbl in box.findChildren(gui.QLabel) if gui.DISCORD in lbl.text())
    finally:
        box.close()


def test_icon_buttons_paint_in_both_themes(window):
    for palette in (gui.DARK, gui.LIGHT):
        gui.THEME.update(palette)
        for b in window.findChildren(gui.IconButton):
            assert not b.grab().isNull()


# ---- what a route can't keep, the checks, settings and presets

def _chip(popup, fmt):
    return next(c for c in popup.chips if c.property("fmt") == fmt)


def test_chip_tooltip_says_route_and_losses(window, samples):
    popup = _open(window, samples, "png")
    assert "transparency" in _chip(popup, "jpg").toolTip()
    assert "parts" not in _chip(popup, "webp").toolTip()


@needs("libreoffice")  # the Word sample is made by LibreOffice
def test_chip_tooltip_names_the_route_through_pdf(window, samples):
    window.set_files([samples("docx")])
    window.picker.click()
    tip = _chip(window.picker.popup, "png").toolTip()
    assert "(via PDF)" in tip and "150 dpi" in tip


def test_status_and_details_after_a_lossy_conversion(window, tmp_path):
    import json

    import trimesh
    a, b = trimesh.creation.box(), trimesh.creation.icosphere()
    b.apply_translation([3, 0, 0])
    glb = tmp_path / "two.glb"
    glb.write_bytes(trimesh.Scene({"box": a, "ball": b}).export(file_type="glb"))
    window.set_files([glb])
    window.picker.set_current("stl")
    assert run(window)
    assert window.status.property("state") == "ok"
    assert window.status.text() == "Saved two.stl" and "parts are merged" in window.note.text()
    assert "Parts: 2 → 1" in window.status.toolTip() and "Parts: 2 → 1" in window.note.toolTip()
    assert not window.details_btn.isHidden()
    dialog = window.show_details()
    texts = [w.text() for w in dialog.findChildren(gui.QLabel)]
    assert any("GLB → STL via trimesh" in t for t in texts) and "As noted" in texts
    report = json.loads(dialog.copy())
    assert report[0]["target"] == "stl" and report[0]["warnings"] == []
    assert json.loads(gui.QGuiApplication.clipboard().text()) == report
    dialog.close()


def test_a_check_that_fails_turns_the_status_amber(window, tmp_path, monkeypatch):
    from latheport.converters import images
    monkeypatch.setitem(images.OWN_WRITERS, "psd", lambda img, dst: dst.write_bytes(b"8BPS garbage"))
    src = tmp_path / "a.png"
    src.write_bytes((Samples(tmp_path / "s"))("png").read_bytes())
    window.set_files([src])
    window.picker.set_current("psd")
    assert run(window)  # the file is kept
    assert window.status.property("state") == "warn" and "doesn't open as PSD" in window.note.text()


def test_options_show_only_what_the_format_takes(window, samples):
    window.set_files([samples("png")])
    panel = window.options_panel
    window.picker.set_current("jpg")
    assert not panel.isHidden() and list(panel.widgets) == ["quality", "max_size", "upscale"]  # the effects are in the editor
    assert not panel.edit_row.isHidden()
    window.picker.set_current("bmp")
    assert list(panel.widgets) == ["max_size", "upscale"]
    window.picker.set_current("ico")
    assert not panel.edit_row.isHidden()  # an icon can be cropped and turned too
    window.picker.set_current("zip")
    assert panel.isHidden()  # nothing to set: no panel at all
    assert not panel.body.isVisible()  # collapsed until asked
    assert panel.head.text() == "Options" and panel.summary() == "Default"


def test_options_and_presets_reach_the_conversion(window, samples, tmp_path, monkeypatch):
    monkeypatch.setenv("LATHEPORT_PRESETS", str(tmp_path / "presets.json"))
    src = samples("png")
    window.set_files([src])
    window.picker.set_current("jpg")
    panel = window.options_panel
    panel.head.click()
    assert panel.body.isVisibleTo(window)
    assert panel.preset_names()[0] == "Default" and panel.pills["Default"].isChecked()
    panel.pills["Smaller for email and web"].click()
    assert panel.chosen()["quality"] == 75 and panel.widgets["quality"].value() == 75
    assert panel.widgets["max_size"].value() == 1920 and panel.widgets["max_size"].currentText() == "1920 px"
    assert panel.summary() == "Smaller for email and web"
    assert panel.pills["Smaller for email and web"].isChecked() and panel.save_pill is None
    jobs = []
    window.run_job = lambda job: jobs.append(job)
    window.start_convert()
    assert jobs[0].settings == {"quality": 75, "max_size": 1920}  # the preset's video and sound settings stay out
    box = panel.widgets["quality"]
    box.setValue(60)
    assert panel.widgets["quality"] is box  # typing in a box never rebuilds it under the cursor
    assert panel.current_preset() == "" and panel.save_pill is not None  # your own: offered for saving
    assert not any(p.isChecked() for p in panel.pills.values())
    monkeypatch.setattr(gui, "ask_text", lambda *a, **k: ("Mine", True))
    panel._save_as()
    assert gui.options.preset("Mine")["quality"] == 60 and panel.current_preset() == "Mine"
    assert not panel.delete_btn.isHidden()
    panel._delete()
    assert "Mine" not in gui.options.all_presets()
    panel._reset()
    assert panel.chosen() == {} and panel.widgets["quality"].value() is None  # "Auto"
    assert panel.widgets["max_size"].currentText() == "Original"
    # a quality kept from photos changes nothing for an ebook's PDF: the pills say Default, as the header does
    panel.widgets["quality"].setValue(60)
    window.set_files([samples("epub")])
    window.picker.set_current("pdf")
    assert set(panel.keys) == {"page_size", "text_size"} and panel.summary() == "Default"
    assert panel.pills["Default"].isChecked() and panel.save_pill is None


def test_colour_depth_is_offered_for_an_hdr_picture(window, samples):
    window.set_files([samples("exr")])
    panel = window.options_panel
    window.picker.set_current("png")
    assert list(panel.widgets) == ["max_size", "bit_depth"]
    box = panel.widgets["bit_depth"]
    assert [box.itemText(i) for i in range(box.count())] == ["As much as the picture has", "8 bits (smaller)", "16 bits"]
    box.setValue(box.itemData(1))  # as choosing it from the list does
    assert panel.chosen() == {"bit_depth": "8"}
    window.picker.set_current("jpg")  # a JPG has 8 bits whatever is chosen
    assert list(panel.widgets) == ["quality", "max_size"]


def test_number_settings_take_a_listed_or_typed_value(window, samples):
    window.set_files([samples("png")])
    window.picker.set_current("jpg")
    panel = window.options_panel
    box = panel.widgets["max_size"]
    assert box.itemText(0) == "Original" and "1920 px" in [box.itemText(i) for i in range(box.count())]
    box.lineEdit().setText("1500")
    box.lineEdit().editingFinished.emit()
    assert panel.chosen() == {"max_size": 1500} and box.currentText() == "1500 px"
    box.lineEdit().setText("12 parsecs")  # not a number in range: back to what it was
    box.lineEdit().editingFinished.emit()
    assert panel.chosen() == {"max_size": 1500} and box.currentText() == "1500 px"
    box.lineEdit().setText("")
    box.lineEdit().editingFinished.emit()
    assert panel.chosen() == {} and box.currentText() == "Original"
    slider = panel.widgets["quality"]
    slider.bar.setValue(80)
    assert panel.chosen() == {"quality": 80} and slider.label.text() == "80"
    slider.bar.setValue(0)
    assert panel.chosen() == {} and slider.label.text() == "Auto"
    assert slider.label.toolTip() == "Auto (92 for JPG)"


def test_settings_say_only_what_applies(window, samples):
    src = samples("mp4")
    if src is None:
        pytest.skip("needs FFmpeg to make a sample video")
    window.set_files([src])
    window.picker.set_current("webm")
    panel = window.options_panel
    fps, quality = panel.widgets["fps"], panel.widgets["video_quality"]
    assert fps.itemText(0) == "Original"  # not "(12 for GIF)": this is WebM
    assert [quality.itemText(i) for i in range(quality.count())] == ["Balanced", "Smaller", "Best"]
    window.picker.set_current("gif")
    assert panel.widgets["fps"].itemText(0) == "Original (12 for GIF)"


def test_the_window_never_gets_smaller_than_what_it_shows(window, samples):
    window.show()
    window.set_files([samples("mp4") or samples("png")])
    window.picker.set_current("gif" if "gif" in window.picker.formats() else "jpg")
    window.options_panel.head.click()
    window.resize(window.width(), 200)
    gui.QApplication.processEvents()
    lay = window.layout()
    widgets = [lay.itemAt(i).widget() or lay.itemAt(i).layout() for i in range(lay.count())]
    shown = [w.geometry() for w in widgets if w is not None and (not hasattr(w, "isVisible") or w.isVisible())]
    assert all(a.bottom() < b.top() for a, b in itertools.pairwise(shown))  # nothing overlaps
    assert window.height() >= window.minimumSizeHint().height()


def test_options_are_remembered(app, tmp_path, monkeypatch, samples):
    settings = QtCore.QSettings(str(tmp_path / "s.ini"), QtCore.QSettings.IniFormat)
    monkeypatch.setattr(gui, "QSettings", lambda *a: settings)
    first = gui.Window()
    first.set_files([samples("png")])
    first.picker.set_current("jpg")
    first.options_panel.widgets["quality"].setValue(70)
    first.close()
    second = gui.Window()
    assert second.options_panel.chosen() == {"quality": 70}
    second.close()


@pytest.mark.skipif(not gui.sys.platform.startswith("win"), reason="Windows shortcut")
def test_start_menu_shortcut(tmp_path, monkeypatch):
    lnk = tmp_path / "Programs" / "Latheport.lnk"
    monkeypatch.setattr(gui, "start_menu_path", lambda: lnk)
    assert not gui.start_menu_installed()
    gui.install_start_menu()
    assert gui.start_menu_installed() and lnk.read_bytes()[:4] == b"L\0\0\0"  # a real shell link
    gui.remove_start_menu()
    assert not gui.start_menu_installed()


def test_preset_is_judged_by_what_this_conversion_takes(window, samples, tmp_path, monkeypatch):
    """Quality 75 and max size 1920 are "Smaller for email and web" for a JPG, even though that
    preset also sets video and sound settings a picture doesn't take."""
    monkeypatch.setenv("LATHEPORT_PRESETS", str(tmp_path / "presets.json"))
    window.set_files([samples("png")])
    window.picker.set_current("jpg")
    panel = window.options_panel
    panel.widgets["quality"].setValue(75)
    panel.widgets["max_size"].setValue(1920)
    assert panel.current_preset() == "Smaller for email and web"
    panel.widgets["quality"].setValue(74)
    assert panel.current_preset() == ""


def test_installing_an_engine_refreshes_the_formats(window, samples, monkeypatch):
    window.set_files([samples("png")])
    filled = []
    monkeypatch.setattr(gui.EnginesDialog, "exec", lambda self: 0)
    monkeypatch.setattr(window, "fill_picker", lambda keep=None: filled.append(True))
    window.show_engines()
    assert filled == [True]


def test_start_menu_error_without_a_message(monkeypatch, tmp_path):
    monkeypatch.setattr(gui, "start_menu_path", lambda: tmp_path / "x.lnk")
    monkeypatch.setattr(gui.subprocess, "run", lambda *a, **k: gui.subprocess.CompletedProcess(a, 1, "", "  \n"))
    with pytest.raises(OSError, match="couldn't write the shortcut"):
        gui.install_start_menu()


# ---- Cancel, and closing the window while a conversion runs

def _slow_route(monkeypatch, tmp_path):
    """txt -> pdf through an engine that runs 30 s (unless killed); returns a function that
    gives the engine's pid once it has started."""
    import sys

    from latheport import engines, pipeline, registry
    pid_file = tmp_path / "engine.pid"
    script = tmp_path / "engine.py"
    script.write_text(f"import os, time\nopen({str(pid_file)!r}, 'w').write(str(os.getpid()))\ntime.sleep(30)\n")

    def slow(src, dst, a, b):
        engines.run([sys.executable, str(script)])
        dst.write_text("done")
    route = registry.Route("txt", "pdf", [registry.Edge("txt", "pdf", slow)])
    monkeypatch.setattr(pipeline, "find_route", lambda *a, **k: route)
    return lambda: int(pid_file.read_text()) if pid_file.exists() and pid_file.read_text() else None


def _pump(app, until, seconds=10):
    import time
    deadline = time.monotonic() + seconds
    while not until() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    return until()


def _start_slow_job(app, window, tmp_path, monkeypatch, files=1):
    pid = _slow_route(monkeypatch, tmp_path)
    srcs = []
    for i in range(files):
        src = tmp_path / f"in{i}.txt"
        src.write_text("x")
        srcs.append(src)
    window.show()
    window.files, window.fmts = srcs, ["txt"] * files
    job = gui.Job(srcs, "pdf", [])
    window.run_job(job)
    assert _pump(app, lambda: pid() is not None), "the engine never started"
    return job, pid(), srcs


def _engine_alive(pid):
    import subprocess
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, check=False)
    return str(pid) in out.stdout


@pytest.mark.skipif(os.name != "nt", reason="checks the engine with tasklist")
def test_closing_mid_conversion_stops_it_and_closes(app, window, tmp_path, monkeypatch):
    job, pid, _ = _start_slow_job(app, window, tmp_path, monkeypatch)
    assert window.convert_btn.text() == "Cancel" and window.convert_btn.isEnabled()
    monkeypatch.setattr(window, "confirm_stop", lambda: True)  # "Stop and close"
    import time
    asked = time.monotonic()
    window.close()
    assert not window.isVisible()  # gone from the screen at once
    assert _pump(app, lambda: window.job is None)
    assert time.monotonic() - asked < 2
    assert job.isFinished() or job.wait(2000)
    assert not _engine_alive(pid)
    assert not (tmp_path / "in0.pdf").exists()


@pytest.mark.skipif(os.name != "nt", reason="checks the engine with tasklist")
def test_keep_converting_leaves_the_job_running_then_cancel_stops_it(app, window, tmp_path, monkeypatch):
    job, pid, _ = _start_slow_job(app, window, tmp_path, monkeypatch, files=3)
    monkeypatch.setattr(window, "confirm_stop", lambda: False)  # "Keep converting"
    window.close()
    assert window.isVisible() and job.isRunning() and _engine_alive(pid)
    _pump(app, lambda: False, QApplication.doubleClickInterval() / 1000 + 0.1)  # not part of a double-click
    window.convert_clicked()  # the Convert button is Cancel while it runs
    assert window.convert_btn.text() == "Stopping…" and not window.convert_btn.isEnabled()
    assert _pump(app, lambda: window.job is None)
    assert not _engine_alive(pid)
    assert window.status.text() == "Stopped" and window.note.text() == "Nothing was saved"
    assert window.convert_btn.text() == "Convert"
    assert not list(tmp_path.glob("in*.pdf"))


def test_cancel_between_files_keeps_the_ones_done(window, tmp_path, monkeypatch):
    from latheport import pipeline
    srcs = []
    for i in range(3):
        src = tmp_path / f"in{i}.txt"
        src.write_text("x")
        srcs.append(src)
    job = gui.Job(srcs, "pdf", [])

    def convert(f, target, out_dir=None, report=None, options=None):
        out = f.with_suffix(".pdf")
        out.write_text("done")
        job.cancel()  # Cancel clicked while the first file was converting: it still finishes
        return out
    monkeypatch.setattr(pipeline, "convert", convert)
    window.files, window.fmts = srcs, ["txt"] * 3
    _run_now(window)
    window.run_job(job)
    assert [p.name for p in window.outputs] == ["in0.pdf"]
    assert window.status.text() == "Stopped"
    assert window.note.text() == "1 saved, 2 not converted"


def test_prompt_buttons_show_their_whole_label(app, window):
    # the stylesheet pads buttons 16 px a side, which a message box doesn't count when it sizes them:
    # "Stop and close" showed as "top and clos"
    old, style = app.styleSheet(), app.style().name()
    app.setStyle("Fusion")  # as the app runs
    gui.apply_theme(app)
    try:
        for box in (window.stop_box(), window.install_box(["ffmpeg"])):
            box.show()
            app.processEvents()
            for b in box.buttons():
                assert b.width() >= b.fontMetrics().horizontalAdvance(b.text()) + 32, b.text()
            box.hide()
    finally:
        app.setStyleSheet(old)
        app.setStyle(style)


@pytest.mark.skipif(os.name != "nt", reason="checks the engine with tasklist")
def test_double_clicking_convert_doesnt_cancel_it(app, window, tmp_path, monkeypatch):
    job, pid, _ = _start_slow_job(app, window, tmp_path, monkeypatch)
    assert window.convert_btn.property("cancel") is True  # Cancel looks like an ordinary button
    window.job_started = time.monotonic()  # as if Convert had just been clicked
    window.convert_clicked()  # the second click of the double-click
    assert job.isRunning() and not job.cancelled and window.convert_btn.text() == "Cancel"
    _pump(app, lambda: False, QApplication.doubleClickInterval() / 1000 + 0.1)
    window.convert_clicked()  # a click of its own: Cancel
    assert job.cancelled
    assert _pump(app, lambda: window.job is None)
    assert not _engine_alive(pid)
    assert window.convert_btn.text() == "Convert" and window.convert_btn.property("cancel") is False


@pytest.mark.skipif(os.name != "nt", reason="checks the engine with tasklist")
def test_stop_and_close_after_the_job_ended_closes_the_window(app, window, tmp_path, monkeypatch):
    # the conversion ends while "Stop converting and close?" is open; "Stop and close" then left the
    # window hidden for good, waiting for a job that had gone, and the app never quit
    from PySide6.QtWidgets import QMessageBox
    job, pid, _ = _start_slow_job(app, window, tmp_path, monkeypatch)

    def answer():
        job.cancel()  # the quick way to end it
        _pump(app, lambda: window.job is None)
        QApplication.activeModalWidget().button(QMessageBox.Yes).click()
    QtCore.QTimer.singleShot(200, answer)
    assert window.close()  # accepted: closed for real
    assert not window.isVisible() and window.job is None and not _engine_alive(pid)


# ---- the editor: crop, adjust and text, for all files or one, with undo

def _photo(tmp_path, name="photo.png", size=(800, 600)):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", size, (90, 150, 220))
    ImageDraw.Draw(img).ellipse([300, 200, 500, 400], fill=(240, 200, 160))
    path = tmp_path / name
    img.save(path)
    return path


def _editor(window, files, target="jpg"):
    window.set_files(files)
    window.picker.set_current(target)
    window.open_editor()
    e = window.editor
    e.resize(1240, 820)
    e.stage.resize(900, 640)
    return e


def test_edit_button_where_edits_are_taken_and_the_effects_left_the_card(window, tmp_path):
    window.set_files([_photo(tmp_path)])
    window.picker.set_current("jpg")
    panel = window.options_panel
    assert not panel.edit_row.isHidden() and not window.drop.edit_btn.isHidden()
    for key in ("area", "texts", "rotate", "crop", "enhance", "colour", "deepfry", "watermark"):
        assert key not in panel.widgets, key  # in the editor now: the card keeps the format's settings
    assert list(panel.widgets) == ["quality", "max_size", "upscale"]
    window.picker.set_current("zip")
    assert panel.isHidden() and window.drop.edit_btn.isHidden()


def test_editor_locks_snaps_and_names_the_shape(window, tmp_path):
    from PySide6.QtCore import QPointF, QRectF
    e = _editor(window, [_photo(tmp_path)])
    st = e.stage
    assert st.real == (800, 600)
    e.shapes.set((4, 5), emit=True)
    assert st.pixel_size() == (480, 600) and st.shape == "4:5"
    b = st.box_rect()
    st.drag = ("move", b.center(), b)
    st._move_box(b.translated(QPointF(5, 3)))  # a little off the middle: it snaps back, with the lines
    assert st.guides == {"v", "h"} and abs(st.box_rect().center().x() - st.image_rect().center().x()) < 0.01
    st._move_box(b.translated(QPointF(60, 0)))  # well off: no line
    assert "v" not in st.guides
    st.drag = None
    e.shapes.set(None, emit=True)
    st.reset_box()
    r = st.image_rect()
    st._resize("se", QRectF(r.left(), r.top(), r.height() * 1.01, r.height()).bottomRight(), st.box_rect())
    assert st.shape == "1:1" and st.pixel_size()[0] == st.pixel_size()[1]  # a free box near a shape becomes it
    e.shapes.set("original", emit=True)
    assert st.edit.lock == (800, 600) and st.shape == "4:3"
    st.reset_box()
    assert st.edit.box == (0.0, 0.0, 1.0, 1.0)


def test_editor_meme_captions_snap_and_reach_the_conversion(window, tmp_path, app):
    from PIL import Image
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtTest import QTest
    photo = _photo(tmp_path)
    e = _editor(window, [photo])
    e.shapes.set((1, 1), emit=True)
    e.choose_tool("text")
    e.meme_btn.click()
    st = e.stage
    assert sorted(t["y"] for t in st.edit.texts) == [0.09, 0.91] and all(t["meme"] for t in st.edit.texts)
    QTest.keyClicks(st, "hello there")  # typing replaces the top caption's words
    QTest.keyClick(st, Qt.Key_Return)
    top = next(t for t in st.edit.texts if t["y"] == 0.09)
    assert top["text"] == "hello there" and st.editing == -1
    st.sel = st.edit.texts.index(top)
    st._move_text(QPointF(st.box_rect().width() * 0.004, 0), (0.5, 0.09))  # a hair off the middle: snaps, line shows
    assert top["x"] == 0.5 and "v" in st.guides
    st.remove_text(next(i for i, t in enumerate(st.edit.texts) if t["y"] == 0.91))
    e.finish()
    assert window.by_hand[photo]["area"] == "12.5,0,75,100"
    assert window.options_panel.summary() == "Cropped, 1 text"
    outputs = run(window)
    with Image.open(outputs[0]) as img:
        assert img.size == (600, 600)  # the square, from the middle of 800 x 600


def test_editor_text_goes_where_you_click_and_you_type_it_there(window, tmp_path, app):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    from latheport import options
    photo = _photo(tmp_path)
    e = _editor(window, [photo])
    e.choose_tool("text")
    st = e.stage
    e.font_box.setCurrentIndex(e.font_box.findData("serif"))
    e.font_box.activated.emit(e.font_box.currentIndex())  # the bar sets how the next text looks
    b = st.box_rect()
    spot = QPoint(round(b.x() + b.width() * 0.25), round(b.y() + b.height() * 0.75))
    QTest.mouseClick(st, Qt.LeftButton, Qt.NoModifier, spot)
    assert len(st.edit.texts) == 1 and st.editing == 0
    t = st.edit.texts[0]
    assert abs(t["x"] - 0.25) < 0.01 and abs(t["y"] - 0.75) < 0.01 and t["font"] == "serif"
    QTest.keyClicks(st, "Hellp")
    QTest.keyClick(st, Qt.Key_Backspace)
    QTest.keyClicks(st, "o")
    assert t["text"] == "Hello" and st.blink.isActive()  # the caret blinks while typing
    e.edge_box.setCurrentIndex(e.edge_box.findData("outline"))
    e.edge_box.activated.emit(e.edge_box.currentIndex())  # a change in the bar: the chosen text changes too
    assert t["edge"] == "outline"
    QTest.keyClick(st, Qt.Key_Escape)
    assert st.editing == -1 and st.edit.texts[0]["text"] == "Hello" and not st.blink.isActive()
    e.edge_box.setCurrentIndex(e.edge_box.findData("none"))
    e.edge_box.activated.emit(e.edge_box.currentIndex())  # done typing: the bar is for the next text only
    assert t["edge"] == "outline" and st.style["edge"] == "none"
    empty_spot = QPoint(round(b.x() + b.width() * 0.75), round(b.y() + b.height() * 0.25))
    QTest.mouseClick(st, Qt.LeftButton, Qt.NoModifier, empty_spot)  # a new one...
    assert len(st.edit.texts) == 2 and st.editing == 1 and st.edit.texts[1]["edge"] == "none"
    QTest.mouseClick(st, Qt.LeftButton, Qt.NoModifier, spot)  # ...left empty: it goes
    assert len(st.edit.texts) == 1 and st.editing == 0 and st.sel == 0  # and the click picked "Hello"
    got = e.result()[photo]
    assert [x["text"] for x in options.boxes(got["texts"])] == ["Hello"] and "area" not in got
    e.close()


def test_editor_all_files_this_file_or_as_it_is(window, tmp_path):
    a, b, c = _photo(tmp_path, "a.png"), _photo(tmp_path, "b.png", (600, 800)), _photo(tmp_path, "c.png")
    e = _editor(window, [a, b, c])
    assert not e.scope.isHidden() and e.scope.value == "all"
    e.shapes.set((16, 9), emit=True)  # every file
    e.go(1)
    assert e.stage.shape == "16:9" and e.scope.value == "all"  # the shared crop, fitted to this one's shape
    e.scope.set("none", emit=True)  # b: left as it is
    e.go(2)
    e.scope.set("this", emit=True)  # c: its own, starting from the shared one
    e.colour.set("bw", emit=True)
    got = e.result()
    assert set(got) == {a, c} and got[a]["area"] == got[c]["area"] and "colour" not in got[a]
    assert got[c]["colour"] == "bw"
    w, h = (float(v) for v in got[a]["area"].split(",")[2:])
    assert abs(w * 800 / (h * 600) - 16 / 9) < 0.01
    assert e.edits.scope(b) == "none" and e.edits.scope(c) == "this"
    e.finish()
    assert window.options_panel.summary() == "2 of 3 files edited"


def test_editor_turn_carries_the_box_and_undo_redo_step_back_and_forth(window, tmp_path, app):
    e = _editor(window, [_photo(tmp_path)])
    st = e.stage
    st.edit.box = (0.0, 0.0, 0.5, 0.5)  # the top-left quarter
    e._record()
    e.turn(1)  # a quarter right: the top-left goes to the top-right
    assert st.edit.look["rotate"] == "right" and st.edit.box == (0.5, 0.0, 0.5, 0.5) and st.real == (600, 800)
    e.turn(0, mirror=True)
    assert st.edit.look["rotate"] == "mirror-left" and st.edit.box == (0.0, 0.0, 0.5, 0.5)
    assert e.undo_btn.isEnabled() and not e.redo_btn.isEnabled()
    from PySide6.QtGui import QKeySequence, QShortcut
    keys = {sc.key().toString() for sc in e.findChildren(QShortcut)}
    assert {QKeySequence(QKeySequence.Undo).toString(), QKeySequence(QKeySequence.Redo).toString()} <= keys
    e.undo_btn.click()
    assert e.stage.edit.look["rotate"] == "right"
    e.undo()
    assert "rotate" not in e.stage.edit.look and e.stage.edit.box == (0.0, 0.0, 0.5, 0.5) and e.stage.real == (800, 600)
    e.redo()
    assert e.stage.edit.look["rotate"] == "right" and e.redo_btn.isEnabled()
    e.close()


def test_editor_shows_the_look_and_the_conversion_gets_it(window, tmp_path, app):
    import numpy as np
    from PIL import Image
    photo = _photo(tmp_path)
    e = _editor(window, [photo])
    e.choose_tool("adjust")
    assert not e.inspector.isHidden()
    e.colour.set("bw", emit=True)
    _pump(app, lambda: not e.jobs)
    img = e.stage.picture.convertToFormat(e.stage.picture.Format.Format_RGB888)
    px = np.frombuffer(img.constBits(), dtype=np.uint8)[:img.width() * 3].reshape(-1, 3)
    assert (px[:, 0] == px[:, 1]).all() and (px[:, 1] == px[:, 2]).all()  # shown grey...
    e.finish()
    outputs = run(window)
    with Image.open(outputs[0]) as out:
        assert out.mode == "L"  # ...and converted grey


def test_editor_zooms_where_the_pointer_is(window, tmp_path):
    from PySide6.QtCore import QPointF
    e = _editor(window, [_photo(tmp_path)])
    st = e.stage
    r = st.image_rect()
    at = QPointF(r.left() + r.width() * 0.25, r.top() + r.height() * 0.25)
    st.zoom_to(3.0, at)
    r2 = st.image_rect()
    assert abs(r2.width() / r.width() - 3) < 0.01
    assert abs((at.x() - r2.left()) / r2.width() - 0.25) < 0.01  # the same spot stays under the pointer
    assert e.zoom_label.text() == f"{round(st.scale() * 100)}%"
    st.fit()
    assert st.image_rect() == r


def test_editor_watermark_is_kept_for_next_time(window, tmp_path):
    from latheport import options
    e = _editor(window, [_photo(tmp_path)])
    e.choose_tool("text")
    e.mark_field.setText("my-site.example")
    e._mark()
    e.mark_corner.setCurrentIndex(e.mark_corner.findData("top-left"))
    e._mark()
    e.finish()
    chosen = window.options_panel.chosen()
    assert chosen["watermark"] == "my-site.example" and chosen["watermark_at"] == "top-left"
    assert options.lasting(chosen)["watermark"] == "my-site.example"


def _drag(st, a, b, steps: int = 6, mods=None):
    """A real drag on the stage from a to b (shares of the picture), pressed, moved in steps, let go."""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication
    mods = mods or Qt.NoModifier
    r = st.image_rect()
    at = lambda x, y: QPointF(r.x() + x * r.width(), r.y() + y * r.height())
    p0 = at(*a)
    points = [p0] + [at(a[0] + (b[0] - a[0]) * k / steps, a[1] + (b[1] - a[1]) * k / steps) for k in range(1, steps + 1)]

    def send(kind, p, buttons):
        QApplication.sendEvent(st, QMouseEvent(kind, p, st.mapToGlobal(p), Qt.LeftButton, buttons, mods))
    send(QEvent.MouseButtonPress, p0, Qt.LeftButton)
    for p in points[1:]:
        send(QEvent.MouseMove, p, Qt.LeftButton)
    send(QEvent.MouseButtonRelease, points[-1], Qt.NoButton)


def _shown(st):
    """The stage's picture as numbers (what's on screen under the tools)."""
    import numpy as np
    img = st.picture.convertToFormat(st.picture.Format.Format_RGB888)
    return np.frombuffer(img.constBits(), dtype=np.uint8, count=img.bytesPerLine() * img.height()).reshape(
        img.height(), img.bytesPerLine())[:, :img.width() * 3].reshape(img.height(), img.width(), 3).astype(np.float32)


def test_editor_hides_what_is_dragged_over_and_the_conversion_hides_it(window, tmp_path, app):
    import numpy as np
    from PIL import Image
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from latheport import options
    photo = tmp_path / "detail.png"  # fine detail, as in a face or a number plate
    Image.fromarray(np.random.default_rng(3).integers(0, 256, (600, 800, 3), dtype=np.uint8)).save(photo)
    e = _editor(window, [photo], "png")  # kept exactly: only the hidden part may change
    e.choose_tool("hide")
    st = e.stage
    assert e.inspector.isHidden() and not e.hide_remove.isEnabled()
    _drag(st, (0.3, 0.3), (0.302, 0.301))  # a click, not a box: nothing made
    assert st.edit.hides == []
    _drag(st, (0.3, 0.3), (0.7, 0.6))
    (hide,) = st.edit.hides
    assert [round(hide[k], 2) for k in "xywh"] == [0.3, 0.3, 0.4, 0.3] and hide["how"] == "pixelate"
    assert st.chosen == 0 and e.hide_remove.isEnabled()
    _pump(app, lambda: not e.jobs and not e.render_timer.isActive())
    shown, before = _shown(st), np.asarray(Image.open(photo), dtype=np.float32)
    assert np.abs(shown[200:340, 260:540] - before[200:340, 260:540]).mean() > 30  # hidden on the stage...
    assert np.abs(shown[:150] - before[:150]).mean() < 1  # ...and nothing else
    e.hide_how.set("blur", emit=True)  # the bar changes the part chosen
    assert hide["how"] == "blur"
    _drag(st, (0.5, 0.45), (0.6, 0.45))  # picked up and moved
    assert round(hide["x"], 2) == 0.4 and len(st.edit.hides) == 1
    QTest.keyClick(st, Qt.Key_Delete)
    assert st.edit.hides == []
    e.undo()
    assert len(e.stage.edit.hides) == 1 and e.stage.edit.hides[0]["how"] == "blur"
    e.finish()
    got = options.items(window.by_hand[photo]["hides"])
    assert got[0]["how"] == "blur" and window.options_panel.summary() == "1 part hidden"
    with Image.open(run(window)[0]) as out:
        a = np.asarray(out.convert("RGB"), dtype=np.float32)
    assert np.abs(a[200:340, 340:620] - before[200:340, 340:620]).mean() > 30 and np.array_equal(a[:150], before[:150])


def test_editor_draws_snaps_picks_up_and_turns_the_drawings(window, tmp_path, app):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from latheport import options
    photo = _photo(tmp_path)
    e = _editor(window, [photo])
    e.choose_tool("draw")
    st = e.stage
    assert not e.inspector.isHidden() and e.draw_buttons["arrow"].isChecked()
    _drag(st, (0.2, 0.2), (0.5, 0.5))
    assert st.edit.marks[0]["kind"] == "arrow" and st.edit.marks[0]["points"] == [[0.2, 0.2], [0.5, 0.5]]
    e.draw_kind("line")
    _drag(st, (0.1, 0.8), (0.4, 0.83), mods=Qt.ShiftModifier)  # Shift: level
    line = st.edit.marks[1]["points"]
    assert abs(line[0][1] - line[1][1]) < 0.002 and line[1][0] > 0.39
    e.draw_kind("box")
    _drag(st, (0.6, 0.1), (0.7, 0.4), mods=Qt.ShiftModifier)  # Shift: square on screen
    (x0, y0), (x1, y1) = st.edit.marks[2]["points"]
    r = st.image_rect()
    assert abs((x1 - x0) * r.width() - (y1 - y0) * r.height()) < 2
    e.draw_kind("pen")
    _drag(st, (0.1, 0.1), (0.3, 0.15), steps=10)
    assert st.edit.marks[3]["kind"] == "pen" and len(st.edit.marks[3]["points"]) > 5
    e.draw_kind("highlight")
    assert st.mark_style["colour"] == "#ffd60a"  # the highlighter keeps its own colour
    e.draw_kind("arrow")
    assert st.mark_style["colour"] == "#ff3b30"
    e._sticker("number")
    for spot in ((0.8, 0.7), (0.9, 0.7)):
        _drag(st, spot, spot, steps=1)
    assert [m.get("n") for m in st.edit.marks[4:]] == [1, 2]  # numbered in order
    e._sticker("heart")  # the next sticker: the one just put down stays a number
    assert st.edit.marks[-1]["name"] == "number" and st.mark_style["name"] == "heart" and st.chosen == -1
    e.draw_kind("select")
    _drag(st, (0.9, 0.7), (0.9, 0.7), steps=1)
    e._sticker("star")  # with Select: the sticker picked up becomes a star
    assert st.edit.marks[-1]["name"] == "star"
    e.draw_kind("select")
    _drag(st, (0.35, 0.35), (0.35, 0.35), steps=1)  # a click on the arrow picks it up
    assert st.chosen == 0 and e.mark_remove.isEnabled()
    e.swatches["#0a84ff"].click()  # the panel changes the drawing chosen
    assert st.edit.marks[0]["colour"] == "#0a84ff"
    QTest.keyClick(st, Qt.Key_Delete)
    assert len(st.edit.marks) == 5 and st.edit.marks[0]["kind"] == "line"
    pts = [list(p) for p in st.edit.marks[0]["points"]]
    e.turn(1)  # a quarter right: every drawing goes with the picture
    assert st.edit.marks[0]["points"] == [[round(1 - y, 5), round(x, 5)] for x, y in pts]
    got = e.result()[photo]
    assert len(options.items(got["marks"])) == 5
    e.finish()
    assert window.options_panel.summary() == "Turned, 5 drawings"


def test_editor_adjust_sliders_and_straighten_show_and_reach_the_conversion(window, tmp_path, app):
    import numpy as np
    from PIL import Image
    photo = _photo(tmp_path)
    e = _editor(window, [photo])
    e.choose_tool("adjust")
    st = e.stage
    before = _shown(st).mean()
    e.adjust_sliders["brightness"][0].setValue(40)  # by a key, not dragged: a step for Undo at once
    assert st.edit.look["adjust"] == "brightness=40" and e.adjust_sliders["brightness"][1].text() == "+40"
    _pump(app, lambda: not e.jobs and not e.render_timer.isActive())
    assert _shown(st).mean() > before + 10  # brighter on the stage
    e._straightening(True)
    assert st.straightening  # the grid while it's dragged
    e.straighten.setValue(25)
    e._straightening(False)
    assert st.edit.look["straighten"] == "2.5" and e.straighten_value.text() == "2.5°" and not st.straightening
    e.turn(0, mirror=True)
    assert st.edit.look["straighten"] == "-2.5"  # mirrored: it leans the other way
    e.undo()
    assert e.stage.edit.look["straighten"] == "2.5" and e.straighten.value() == 25
    e.finish()
    assert window.options_panel.summary() == "Straightened, adjusted"
    with Image.open(run(window)[0]) as out:
        assert out.size == (800, 600)
        assert np.asarray(out.convert("L"), dtype=np.float32).mean() > np.asarray(Image.open(photo).convert("L"), dtype=np.float32).mean() + 10


def test_editor_watermark_picture_and_opacity_are_kept_and_said_when_gone(window, tmp_path, monkeypatch):
    from PIL import Image

    from latheport import options
    from latheport.editor import window as editor_window
    logo = tmp_path / "logo.png"
    Image.new("RGBA", (200, 80), (255, 0, 0, 255)).save(logo)
    monkeypatch.setattr(editor_window.QFileDialog, "getOpenFileName", lambda *a, **k: (str(logo), ""))
    e = _editor(window, [_photo(tmp_path)])
    e.choose_tool("text")
    assert e.mark_drop.isHidden() and e.mark_pick.text() == "Add a picture…"
    e.mark_pick.click()
    e.mark_opacity.setValue(40)
    assert e.mark_pick.text() == "logo.png" and not e.mark_drop.isHidden() and e.mark_opacity_value.text() == "40%"
    assert e.stage.mark["image"] == str(logo) and e.stage.mark["opacity"] == 40
    e.finish()
    chosen = window.options_panel.chosen()
    assert chosen["watermark_image"] == str(logo) and chosen["watermark_opacity"] == 40
    assert options.lasting(chosen)["watermark_opacity"] == 40  # kept for next time
    window.open_editor()  # open again: as it was left
    e = window.editor
    assert e.mark_opacity.value() == 40 and e.mark_pick.text() == "logo.png"
    e.close()
    logo.unlink()
    window.open_editor()
    assert "logo.png isn't there any more" in window.editor.mark_note.text()  # said, not a crash
    window.editor.mark_drop.click()
    window.editor.finish()
    assert "watermark_image" not in window.options_panel.chosen()


# ---- the editor with a video: playing, the timeline, trims, a frame saved

def _clip(tmp_path, name="clip.mp4", seconds=4, size="640x360"):
    import subprocess

    from latheport import engines
    path = tmp_path / name
    subprocess.run([engines.find("ffmpeg"), "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=25",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-t", str(seconds), "-c:v", "libx264", "-g", "25",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


def _frame_at(path, seconds, out):
    """FFmpeg's own frame at seconds, as numbers."""
    import subprocess

    import numpy as np
    from PIL import Image

    from latheport import engines
    subprocess.run([engines.find("ffmpeg"), "-y", "-v", "error", "-ss", f"{seconds:.3f}", "-i", str(path),
                    "-frames:v", "1", str(out)], check=True)
    return np.asarray(Image.open(out).convert("RGB"), dtype=np.float32)


@needs("ffmpeg")
def test_editor_plays_a_video_and_shows_ffmpegs_own_still_when_paused(window, tmp_path, app):
    import numpy as np
    clip = _clip(tmp_path)
    e = _editor(window, [clip], "mp4")
    assert not e.video_bar.isHidden() and e.rail_buttons["trim"].isEnabled() and e.playback is not None
    assert abs(e.timeline.duration - 4) < 0.1 and e.time_label.text() == "0:01.0 / 0:04.0"
    shown = []
    e.playback.frame.connect(lambda img, t: shown.append(t))
    e.toggle_play()
    assert e.play_btn.icon == "pause"
    assert _pump(app, lambda: e.video_time > 2.0, 10)  # it plays on from where the stage was (1.0 s)
    e.toggle_play()
    assert e.play_btn.icon == "play" and len(shown) > 15 and min(shown) >= 0.99
    _pump(app, lambda: not e.jobs and not e.still_timer.isActive(), 10)
    _pump(app, lambda: False, 0.3)
    still = _shown(e.stage)
    assert still.shape[:2] == (360, 640)  # the still at the video's own size, not the playing frame's
    exact = _frame_at(clip, e.video_time, tmp_path / "exact.png")
    assert np.abs(still - exact).mean() < 2, np.abs(still - exact).mean()  # what FFmpeg makes there
    e.colour.set("bw", emit=True)  # a look changed while paused: the still again, with it
    _pump(app, lambda: not e.jobs and not e.still_timer.isActive(), 10)
    _pump(app, lambda: False, 0.3)
    grey = _shown(e.stage)
    assert np.abs(grey[..., 0] - grey[..., 1]).max() < 3 and np.abs(grey[..., 1] - grey[..., 2]).max() < 3
    e.close()
    assert e.playback is None


@needs("ffmpeg")
def test_editor_trims_steps_and_paces_each_video_on_its_own(window, tmp_path, app):
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    from latheport.converters import media
    a, b = _clip(tmp_path, "a.mp4"), _clip(tmp_path, "b.mp4")
    e = _editor(window, [a, b], "mp4")
    tl = e.timeline
    tl.resize(600, tl.height())
    y = 8 + 22  # on the strip

    def drag(x0, x1):
        for kind, x, buttons in ((QEvent.MouseButtonPress, x0, Qt.LeftButton), (QEvent.MouseMove, x1, Qt.LeftButton),
                                 (QEvent.MouseButtonRelease, x1, Qt.NoButton)):
            QApplication.sendEvent(tl, QMouseEvent(kind, QPointF(x, y), tl.mapToGlobal(QPointF(x, y)), Qt.LeftButton,
                                                   buttons, Qt.NoModifier))
    drag(tl.x_of(0) - 4, tl.x_of(1.0))  # the start handle, to 1 s
    assert tl.start == 1.0 and e.edits.times[a] == {"start": "0:01"}
    assert abs(e.video_time - 1.0) < 0.01  # the picture followed the handle
    e._scrub(3.0)
    e.trim_here("end")  # O: it ends where it is
    assert e.edits.times[a] == {"start": "0:01", "end": "0:03"} and e.trim_note.text() == "From 0:01.0 to 0:03.0: 0:02.0 kept"
    e.step(1)
    assert _pump(app, lambda: abs(e.video_time - 3.04) < 0.005, 10)  # one frame on: 1/25 s
    e.speed_box.setCurrentIndex(e.speed_box.findData("2"))
    e.speed_box.activated.emit(e.speed_box.currentIndex())
    assert e.edits.times[a]["speed"] == "2"
    e.go(1)
    assert e.timeline.start is None and e.timeline.end is None and b not in e.edits.times  # b's own: untouched
    e.undo()  # the speed back off a
    assert e.edits.times[a] == {"start": "0:01", "end": "0:03"}
    e.go(0)
    e.speed_box.setCurrentIndex(e.speed_box.findData("2"))
    e.speed_box.activated.emit(e.speed_box.currentIndex())
    e.finish()
    assert window.options_panel.summary() == "1 of 2 files edited"
    assert window.by_hand[a]["start"] == "0:01" and window.by_hand[a]["speed"] == "2" and b not in window.by_hand
    outputs = run(window)
    lengths = sorted(round(media.probe(o)["duration"], 1) for o in outputs)
    assert abs(lengths[0] - 1.0) <= 0.15 and abs(lengths[1] - 4.0) <= 0.15  # a: 2 s kept, at 2x; b: as it was


@needs("ffmpeg")
def test_space_plays_and_pauses_a_video_but_space_drag_looks_around(window, tmp_path, app):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    e = _editor(window, [_clip(tmp_path)], "mp4")
    st = e.stage
    QTest.keyPress(st, Qt.Key_Space)
    QTest.keyRelease(st, Qt.Key_Space)
    assert e.playback.playing
    QTest.keyPress(st, Qt.Key_Space)
    QTest.keyRelease(st, Qt.Key_Space)
    assert not e.playback.playing
    st.zoom_to(3.0)
    QTest.keyPress(st, Qt.Key_Space)
    _drag(st, (0.5, 0.5), (0.4, 0.4))  # held to look around: no play
    QTest.keyRelease(st, Qt.Key_Space)
    assert not e.playback.playing
    e._scrub(1.0)
    e._trim(end=2.0)
    e.toggle_play()  # plays to the end of the part kept, and stops there
    assert _pump(app, lambda: e.video_time > 1.5, 10)
    assert _pump(app, lambda: not e.playback.playing, 10) and abs(e.video_time - 2.0) < 0.1
    e.toggle_play()  # again: from the start of the part kept
    assert _pump(app, lambda: e.playback.playing and e.video_time < 0.5, 10)
    e.close()


@needs("ffmpeg")
def test_save_this_frame_puts_a_picture_with_the_edits_next_to_the_video(window, tmp_path, app):
    import numpy as np
    from PIL import Image
    clip = _clip(tmp_path, "talk.mp4")
    e = _editor(window, [clip], "mp4")
    e.shapes.set((1, 1), emit=True)
    e.colour.set("bw", emit=True)
    e._scrub(2.0)
    e.choose_tool("trim")
    e.save_frame()
    assert _pump(app, lambda: e.frame_btn.isEnabled(), 20)
    out = tmp_path / "talk at 0-02.0.png"
    assert e.frame_note.text() == "Saved as talk at 0-02.0.png, next to the video."
    with Image.open(out) as img:
        assert img.size == (360, 360)  # cropped square
        px = np.asarray(img.convert("RGB"), dtype=np.int16)
        assert np.abs(px[..., 0] - px[..., 1]).max() < 3  # black and white
    e.save_frame()
    assert _pump(app, lambda: e.frame_btn.isEnabled(), 20)
    assert (tmp_path / "talk at 0-02.0 (1).png").exists()  # never over the first
    e.close()


def test_timeline_places_and_reads_times():
    from latheport.editor.timeline import Timeline, clock
    assert clock(7.44) == "0:07.4" and clock(3723) == "1:02:03.0" and clock(-1) == "0:00.0"
    tl = Timeline()
    tl.resize(520, 60)
    tl.set_video(10.0)
    assert abs(tl.time_at(tl.x_of(2.5)) - 2.5) < 1e-6 and tl.time_at(-50) == 0 and tl.time_at(9999) == 10
    tl.set_trim(2.0, None)
    assert tl.kept() == (2.0, 10.0)


def test_edit_summary_says_what_was_done():
    from latheport.gui import edit_summary
    assert edit_summary({}, 1) == ""
    assert edit_summary({"x": {"rotate": "right", "area": "1,1,50,50", "colour": "bw", "deepfry": 40}}, 1) == \
        "Turned, cropped, black and white, deep fried 40%"
    assert edit_summary({"x": {"straighten": "2", "adjust": "brightness=5", "marks": '[{"kind":"line"}]',
                               "hides": '[{"x":0},{"x":1}]'}}, 1) == "Straightened, adjusted, 1 drawing, 2 parts hidden"
    assert edit_summary({"x": {}, "y": {}}, 5) == "2 of 5 files edited"


# ---- no title bar: the header is the title bar

def test_the_header_is_the_title_bar_with_the_version(window, app):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtWidgets import QLabel

    from latheport import __version__, chrome
    assert window.windowFlags() & Qt.FramelessWindowHint
    assert any(lb.text() == __version__ for lb in window.header.findChildren(QLabel))  # the version, beside the name
    window.resize(460, 600)
    window.show()
    _pump(app, lambda: window.header.width() < 460, 3)  # laid out

    def at(widget, local):
        p = widget.mapTo(window, local)
        return window.hit(p.x(), p.y())
    header = window.header
    assert at(header, QPoint(header.width() // 2, header.height() // 2)) == chrome.HTCAPTION   # drag by its blank part
    assert at(header, QPoint(4, header.height() // 2)) == chrome.HTCAPTION                     # and by the name
    assert window.hit(window.width() // 2, 3) == chrome.HT_EDGES[(0, -1)]  # the very top resizes
    assert window.hit(window.width() // 2, 8) == chrome.HTCAPTION  # the margin above the header drags too
    assert at(window.caption.max, window.caption.max.rect().center()) == chrome.HTMAXBUTTON  # Windows 11 snap layouts
    assert at(window.caption.close, window.caption.close.rect().center()) == chrome.HTCLIENT
    assert at(window.convert_btn, window.convert_btn.rect().center()) == chrome.HTCLIENT
    assert window.hit(2, 300) == chrome.HT_EDGES[(-1, 0)] and window.hit(458, 598) == chrome.HT_EDGES[(1, 1)]


def test_caption_buttons_minimise_maximise_close(window, app):
    window.show()
    window.caption.max.click()
    _pump(app, lambda: window.isMaximized(), 3)
    assert window.isMaximized() and window.caption.max.toolTip() == "Restore"
    window.caption.max.click()
    _pump(app, lambda: not window.isMaximized(), 3)
    assert not window.isMaximized()
    window.caption.close.click()
    _pump(app, lambda: not window.isVisible(), 3)
    assert not window.isVisible()


def test_editor_has_no_title_bar_either(window, tmp_path, app):
    from PySide6.QtCore import QPoint, Qt

    from latheport import chrome
    e = _editor(window, [_photo(tmp_path)])
    e.show()
    _pump(app, lambda: e.bar.width() <= e.width(), 3)
    assert e.windowFlags() & Qt.FramelessWindowHint
    bar = e.bar
    blank = 0
    for x in range(10, bar.width() - 10, 4):  # across the bar: controls stay controls, everything else drags
        p = bar.mapTo(e, QPoint(x, bar.height() // 2))
        child = e.childAt(p)
        want = chrome.HTCLIENT if isinstance(child, chrome.INTERACTIVE) else chrome.HTCAPTION
        if child is e.caption.max:
            want = chrome.HTMAXBUTTON
        assert e.hit(p.x(), p.y()) == want, (x, type(child).__name__)
        blank += want == chrome.HTCAPTION
    assert blank > 20  # there's room to drag it by
    undo = e.undo_btn.mapTo(e, e.undo_btn.rect().center())
    assert e.hit(undo.x(), undo.y()) == chrome.HTCLIENT
    e.close()


def test_options_open_beside_the_column_never_making_the_window_taller(window, samples, app):
    window.show()
    window.move(40, 40)
    window.resize(460, 560)
    _pump(app, lambda: False, 0.2)
    window.set_files([samples("mp4")])
    window.picker.set_current("mp4")  # the most settings there are
    panel = window.options_panel
    panel.head.click()
    _pump(app, lambda: window.width() > 460, 2)
    assert window.height() == 560  # never taller...
    assert window.width() == min(460 + gui.SIDE + 16, window.screen().availableGeometry().width())  # ...wider
    assert panel.body.isVisible() and panel.body.geometry().left() > panel.geometry().right()  # beside the column
    assert panel.body.height() > 400 and window.geometry().bottom() <= window.screen().availableGeometry().bottom()
    headings = [w.text() for w in panel.rows if w.objectName() == "section"]
    assert headings[:3] == ["VIDEO", "SOUND", "CUT AND SIZE"]  # grouped
    if panel.scroll.widget().sizeHint().height() > panel.scroll.viewport().height():
        assert panel.scroll.verticalScrollBar().maximum() > 0  # what doesn't fit scrolls in the panel
    panel.head.click()
    _pump(app, lambda: window.width() == 460, 2)
    assert window.width() == 460 and window.height() == 560 and not panel.body.isVisible()


def test_rows_for_another_format_never_show_over_the_new_ones(window):
    panel = window.options_panel
    panel.show_for(["video_quality", "video_size", "fps", "bitrate"], "mp4")
    old = list(panel.rows)
    panel.show_for(["quality", "max_size"], "jpg")  # before the event loop deletes the old rows
    assert old and not any(w.isVisibleTo(panel.body) for w in old)
    assert all(w.isVisibleTo(panel.body) for w in panel.rows)


def test_a_damaged_file_says_so_in_plain_words(window, tmp_path, app):
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"\x89PNG\r\n\x1a\nnot a picture" * 20)
    window.set_files([broken])
    window.picker.set_current("jpg")
    run(window)
    shown = window.status.text() + " " + window.note.text()
    assert "broken.png doesn't open as a PNG picture" in shown
    assert "\\\\" not in shown and str(tmp_path) not in shown  # no paths, no doubled backslashes


# ---- what the 1.5 review found, each kept fixed

class Waiting(QtCore.QThread):
    """A thread that runs until it's told to stop (or for seconds), saying done(...) as the editor's jobs do."""
    done = QtCore.Signal(object, str)

    def __init__(self, seconds=30.0):
        super().__init__()
        self.seconds, self.go = seconds, threading.Event()

    def run(self):
        self.go.wait(self.seconds)
        self.done.emit("finished", "")


class SlowJob(gui.Job):
    """A conversion that runs until it's told to stop."""

    def __init__(self, files):
        super().__init__(files, "jpg", [])
        self.go = threading.Event()

    def _run(self):
        self.go.wait(30)
        self.done.emit([], [], [], [])


def _pdf(tmp_path, pages=2):
    from PIL import Image
    path = tmp_path / "doc.pdf"
    first, *rest = [Image.new("RGB", (200, 280), (255, 255, 255 - 40 * i)) for i in range(pages)]
    first.save(path, save_all=True, append_images=rest)
    return path


# ---- the Options rows

def test_a_setting_changed_keeps_the_format_and_its_rows(window, samples, monkeypatch):
    """A setting changed rebuilds the picker's tooltips: with a format other than the preselected one chosen,
    every Options row was deleted and made again (twice), so focus and a slider being dragged were lost."""
    window.set_files([samples("jpg")])
    other = next(f for f in ("webp", "avif", "png") if f in window.picker.formats() and f != window.picker.current())
    window.picker.set_current(other)
    rows = dict(window.options_panel.widgets)
    assert rows
    shown = []
    real = window.options_panel.show_for
    monkeypatch.setattr(window.options_panel, "show_for", lambda keys, target=None: (shown.append(target),
                                                                                     real(keys, target)))
    window.refresh_notes()
    assert window.picker.current() == other and shown == [other]
    assert all(window.options_panel.widgets[k] is w for k, w in rows.items())  # the same rows, not new ones


# ---- the editor's text

def _texted(window, tmp_path, app, typing=False):
    e = _editor(window, [_photo(tmp_path), _photo(tmp_path, "second.png")])
    e.show()
    e.choose_tool("text")
    st = e.stage
    b = st.box_rect()
    QTest.mouseClick(st, Qt.LeftButton, Qt.NoModifier, QPoint(round(b.center().x()), round(b.center().y())))
    QTest.keyClicks(st, "Hi")
    if not typing:
        QTest.keyClick(st, Qt.Key_Escape)
        st.sel = 0
        e._picked(0)
    app.processEvents()
    return e, st


def test_dragging_the_text_size_is_one_undo_step_and_keeps_the_keys(window, tmp_path, app):
    """Each value the Size slider passed through was its own Undo step (28 for one drag, pushing older ones out),
    and the slider moved the keyboard to the picture, so the next arrow key switched file."""
    e, st = _texted(window, tmp_path, app)
    steps = len(e.history.states)
    e.size_bar.setSliderDown(True)
    for v in range(4, 25):
        e.size_bar.setValue(v)
    e.size_bar.setSliderDown(False)
    assert st.edit.texts[0]["size"] == pytest.approx(0.24)
    assert len(e.history.states) == steps + 1  # the whole drag: one step
    e.size_bar.setFocus()
    QTest.keyClick(e.size_bar, Qt.Key_Right)  # a step by key: its own Undo step, the keys stay on the slider
    assert st.edit.texts[0]["size"] == pytest.approx(0.25) and len(e.history.states) == steps + 2
    assert e.focusWidget() is e.size_bar and e.index == 0
    e.close()


def test_arrow_keys_while_typing_stay_in_the_text_and_paste_types(window, tmp_path, app):
    """With several files open, Left or Right while typing a text switched to another file (and ended the text)."""
    e, st = _texted(window, tmp_path, app, typing=True)
    for key in (Qt.Key_Right, Qt.Key_Left, Qt.Key_PageDown, Qt.Key_Home):
        QTest.keyClick(st, key)
    assert e.index == 0 and st.editing == 0 and st.edit.texts[0]["text"] == "Hi"
    QApplication.clipboard().setText(" there\nfriend")
    QTest.keyClick(st, Qt.Key_V, Qt.ControlModifier)
    assert st.edit.texts[0]["text"] == "Hi there friend"  # pasted, on the one line a text box takes
    e.close()


# ---- threads that outlive their window

def test_closing_the_editor_waits_once_for_all_its_work(window, tmp_path, app):
    """It waited 3 s for each job in turn (four jobs: 12 s with the window frozen); now 3 s for all of them."""
    from latheport.editor import jobs
    e = _editor(window, [_photo(tmp_path)])
    work = [Waiting(6) for _ in range(3)]
    for job in work:
        e._keep(job)
        job.start()
    began = time.monotonic()
    e.close()
    took = time.monotonic() - began
    assert took < 4.5, took
    assert all(job in jobs.RUNNING and job.isRunning() for job in work)
    for job in work:
        job.go.set()
    assert _pump(app, lambda: not any(job in jobs.RUNNING for job in work))


def test_closing_the_pages_window_mid_save_keeps_its_thread(app, tmp_path):
    """Pages let a running thread go when it closed (3 s wait, then deleted): Python destroying a running QThread
    ends the program."""
    from latheport.editor import jobs
    from latheport.editor.pages import PagesWindow
    w = PagesWindow([_pdf(tmp_path)], False, None)
    started = w.jobs
    _pump(app, lambda: not started)  # its thumbnails
    heard = []
    job = Waiting(30)
    job.done.connect(lambda *a: heard.append(a))
    w._keep(job)
    job.start()
    w.close()
    del w
    gc.collect()
    app.processEvents()
    assert job in jobs.RUNNING and job.isRunning()
    job.go.set()
    assert _pump(app, lambda: job not in jobs.RUNNING)
    assert heard == []  # the closed window wasn't called


def test_quitting_counts_the_editors_threads(window, tmp_path, app):
    """The check on the way out (a running thread destroyed at exit is a crash) missed what the editor runs."""
    from latheport.editor import jobs
    e = _editor(window, [_photo(tmp_path)])
    _pump(app, lambda: not e.jobs)
    assert not gui.still_running(window)
    job = Waiting(30)
    e._keep(job)
    job.start()
    assert gui.still_running(window)
    job.go.set()
    assert _pump(app, lambda: job not in jobs.RUNNING)
    assert not gui.still_running(window)
    e.close()


# ---- the main window

def test_back_from_the_tray_closing_quits_again(window, tmp_path, monkeypatch, app):
    """After one close to the notification area, closing never quit again, even with the choice turned off."""
    folder = tmp_path / "watched"
    folder.mkdir()
    monkeypatch.setattr(gui.QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    window.settings.setValue("watch/tray", "true")
    window.set_watch_list([{"folder": str(folder), "target": "png"}])
    window.show()
    window.close()
    assert not window.isVisible() and not app.quitOnLastWindowClosed() and window.tray.isVisible()
    window._from_tray()
    window.settings.setValue("watch/tray", "false")
    window.close()
    assert app.quitOnLastWindowClosed() and not window.tray.isVisible()


def test_edit_waits_while_converting_and_a_second_click_raises_the_editor(window, tmp_path, app):
    """The drop zone's pencil stayed live during a conversion (for a PDF, the Pages window then waited on the
    converter's lock with the window frozen); and each click opened another editor."""
    photo = _photo(tmp_path)
    window.set_files([photo])
    window.picker.set_current("jpg")
    assert window.drop.edit_btn.isEnabled()
    job = SlowJob([photo])
    window.run_job(job)
    assert not window.drop.edit_btn.isEnabled()
    window.open_editor()
    assert window.editor is None
    job.go.set()
    assert _pump(app, lambda: window.job is None)
    assert window.drop.edit_btn.isEnabled()
    window.open_editor()
    first = window.editor
    window.open_editor()
    assert window.editor is first
    first.close()


def test_a_watched_folders_job_is_told_by_its_own_files(window, tmp_path):
    """A watched folder's file, stopped or failed, was described from the files in the window."""
    shown = [_photo(tmp_path, f"shown{i}.png") for i in range(3)]
    window.set_files(shown)
    watched = _photo(tmp_path, "watched.png")
    job = gui.Job([watched], "jpg", [], out_dir=tmp_path)
    job.cancel()
    window.job = job
    window.job_done([tmp_path / "watched.jpg"], [])
    assert window.status.text() == "Stopped" and window.note.text() == "1 saved, 0 not converted"
    window.job = gui.Job([watched], "jpg", [], out_dir=tmp_path)
    window.job_done([], ["watched.png: the file is damaged"])
    assert window.status.text() == "Couldn't convert watched.png" and window.note.text() == "the file is damaged"


def test_show_in_folder_quotes_a_name_with_a_comma(monkeypatch, tmp_path):
    """Explorer splits /select at commas: an unquoted C:\\out\\a,b.png picked the wrong thing."""
    started = []
    monkeypatch.setattr(gui.sys, "platform", "win32")
    monkeypatch.setattr(gui.subprocess, "Popen", lambda cmd, *a, **k: started.append(cmd))
    gui.reveal(Path(r"C:\out\a,b.png"))
    assert isinstance(started[0], str) and started[0].endswith(r'/select,"C:\out\a,b.png"')


def test_thumbnails_are_sharp_on_a_high_dpi_screen(window, tmp_path, app, monkeypatch):
    """At 200 % the drop zone's picture was made at 240 x 120 pixels and blown up to twice that."""
    photo = _photo(tmp_path, size=(1600, 800))
    monkeypatch.setattr(window.drop, "devicePixelRatioF", lambda: 2.0)
    window.set_files([photo])
    badge = window.drop.badge
    assert _pump(app, lambda: badge.picture is not None)
    assert badge.picture.width() == 480 and badge.picture.deviceIndependentSize().width() == 240


# ---- the editor's look and its jobs

def test_the_done_button_hovers_in_the_accent(window, tmp_path):
    """A lavender hover left from the old colours turned the copper Done button purple."""
    from latheport.editor import look
    sheet = look.stylesheet()
    assert "#9aa3fb" not in sheet.lower() and "primary:hover" in sheet


@pytest.mark.parametrize("job_name", ["StillJob", "StripJob"])
def test_a_missing_ffmpeg_ends_the_paused_still_and_the_strip_quietly(job_name, monkeypatch, tmp_path):
    """FFmpeg missing (or a look FFmpeg can't do) raised ConversionError, which these didn't catch: the thread died
    with a traceback and the window never heard."""
    from latheport import engines
    from latheport.editor import playback
    from latheport.errors import EngineMissingError
    monkeypatch.setattr(engines, "require", lambda key: (_ for _ in ()).throw(EngineMissingError(["ffmpeg"])))
    video = tmp_path / "v.mp4"
    video.write_bytes(b"")
    heard = []
    if job_name == "StillJob":
        job = playback.StillJob(1, video, 0.5, {}, (320, 240))
        job.done.connect(lambda *a: heard.append(a), Qt.DirectConnection)
    else:
        job = playback.StripJob(video, 2.0, (320, 240))
        job.got.connect(lambda *a: heard.append(a), Qt.DirectConnection)
    job.run()  # on this thread: an exception would come out here
    assert len(heard) == 1 and heard[0][-1] in (None, [])


def test_a_take_that_doesnt_move_in_time_plays_without_dividing_by_zero(app):
    """A take whose keys all sit at one moment (length 0) raised ZeroDivisionError on every tick."""
    import numpy as np
    from meshport import Animation, Channel, Node, Scene

    from latheport import viewer

    from .test_viewer import cube
    a, b = Node("a", mesh=cube()), Node("b", mesh=cube())
    scene = Scene([a, b], [Animation("A", [Channel(a, "translation", np.array([0.0, 1.0]),
                                                   np.array([[0, 0, 0], [1, 0, 0.0]]))]),
                           Animation("B", [Channel(b, "translation", np.array([0.5]), np.array([[0, 1, 0.0]]))])])
    win = viewer.ViewerWindow({"bg": "#202020"}, Path("two.glb"), "glb", viewer.flatten(scene))
    try:
        win.show()
        win._choose("B")
        player = win.player
        assert player.motion.duration == 0
        player.time = 0.7
        player._tick()
        win._moved(player.time)
        assert win.timeline.value() == 0 and player.time == pytest.approx(0.5)
    finally:
        win.close()
        app.processEvents()


def _see_through_stage(size=(4000, 3000)):
    from PySide6.QtGui import QColor, QImage

    from latheport import options
    from latheport.editor.model import Edit
    from latheport.editor.stage import Stage
    st = Stage()
    st.resize(1344, 844)
    pic = QImage(*size, QImage.Format_ARGB32)
    pic.fill(QColor(80, 140, 200, 0))  # all see-through: only the checkerboard shows
    edit = Edit()
    edit.texts.append(options.text_box("A caption", "bottom"))
    st.open_file(pic, pic, size, edit)
    return st


def test_the_stage_checkerboard_is_one_fill_and_the_same_squares(app):
    """The checkerboard was a Python fillRect per square over the whole picture: 1.1 s a paint at 8x zoom (every
    caret blink and drag move). Now one tiled fill of what's on the screen, the squares where they were."""
    from PySide6.QtGui import QImage
    st = _see_through_stage()
    st.edit.texts.clear()
    img = QImage(st.size(), QImage.Format_ARGB32)
    st.render(img)
    r = st.image_rect()
    for x, y in ((int(r.left()) + 3, int(r.top()) + 3), (600, 400), (605, 415), (int(r.right()) - 3, 420)):
        dark = (x // 10 + y // 10) % 2 == 0
        assert img.pixelColor(x, y).name() == ("#2e2f33" if dark else "#3a3b40"), (x, y)
    st.zoom_to(8.0)
    st.render(img)  # once, to have everything made
    began = time.perf_counter()
    st.render(img)
    assert time.perf_counter() - began < 0.3


def test_texts_are_made_at_the_pictures_own_size_when_zoomed_in(app):
    """Zoomed in 8 times, the texts' layer was made at 8 times the screen's share of the picture (hundreds of MB,
    100 ms and more each time it changed): no more pixels than the picture itself has."""
    st = _see_through_stage()
    st.zoom_to(8.0)
    w, h = st._layer_size(st.box_rect())
    assert (w, h) == pytest.approx(st.pixel_size(), abs=1)
    assert st._texts_image(st.box_rect()).size().width() == w
    st.fit()
    b = st.box_rect()
    assert st._layer_size(b) == (round(b.width()), round(b.height()))  # fitted: the screen's own pixels


def test_a_video_frame_reaches_the_window_as_the_same_picture(app):
    """The player's frame is copied into the QImage once (it was copied twice: 4.0 ms a 1080p frame, now 2.0)."""
    from latheport.editor import playback

    class Frame:
        def __init__(self, w, h):
            self.w, self.h, self.wall = w, h, 1.5
            self.buf = bytearray(os.urandom(w * h * 4))
            self.released = False

        def release(self):
            self.released = True
    bridge = type("Bridge", (), {"_arrived": type("S", (), {"emit": lambda self: None})()})()
    presenter = playback._presenter_class()(bridge)
    frame = Frame(64, 36)
    presenter.present(frame)
    image, wall = presenter.take()
    assert frame.released and wall == 1.5 and (image.width(), image.height()) == (64, 36)
    assert bytes(image.constBits())[:64 * 36 * 4] == bytes(frame.buf)


def test_files_can_be_added_converted_and_cancelled_from_the_keyboard(window, tmp_path, app, monkeypatch):
    """The main window had no keyboard way to add files: the drop zone took no focus and opened the file
    dialog on a mouse click only. Now Tab reaches it and Enter or Space opens it; Ctrl+O, Ctrl+Enter and Esc
    choose files, convert and cancel."""
    photo = _photo(tmp_path)
    asked = []
    monkeypatch.setattr(gui.QFileDialog, "getOpenFileNames",
                        staticmethod(lambda *a, **k: (asked.append(1), ([str(photo)], ""))[1]))
    assert window.drop.focusPolicy() & Qt.TabFocus
    QTest.keyClick(window.drop, Qt.Key_Return)
    assert asked == [1] and window.files == [photo]
    QTest.keyClick(window.drop, Qt.Key_Space)
    assert asked == [1, 1]
    window.show()
    window.activateWindow()
    assert _pump(app, lambda: QApplication.activeWindow() is window, 2)  # shortcuts work in the active window
    QTest.keyClick(window, Qt.Key_O, Qt.ControlModifier)
    assert asked == [1, 1, 1]
    started = []
    window.run_job = lambda job: (started.append(job), setattr(window, "job", job))
    window.picker.set_current("jpg")
    QTest.keyClick(window, Qt.Key_Return, Qt.ControlModifier)
    assert len(started) == 1 and started[0].target == "jpg"
    QTest.keyClick(window, Qt.Key_Return, Qt.ControlModifier)  # converting: the key never cancels
    assert len(started) == 1 and not started[0].cancelled
    QTest.keyClick(window, Qt.Key_Escape)
    assert started[0].cancelled
    window.job = None


def test_an_offline_check_is_tried_again_and_an_open_window_checks_each_day(window, monkeypatch, app):
    """The check was stamped before it asked (offline at start-up: no check that day) and ran once, 4 s after
    start-up, so a window left open for days never checked again."""
    from latheport import updates
    answers = []

    class Check(QtCore.QThread):
        found = QtCore.Signal(str, str)

        def __init__(self, parent=None):
            super().__init__(parent)
            answers.append(self)

        def run(self):
            self.release = None if len(answers) == 1 else object()  # offline the first time
    monkeypatch.setattr(gui, "UpdateJob", Check)
    day = updates.EVERY_S
    window.check_updates(now=10 * day)
    assert _pump(app, lambda: answers[-1].isFinished())
    app.processEvents()
    assert window.settings.value("update/checked", "0") in ("0", None)  # no answer: not today's check
    window.check_updates(now=10 * day + 3600)  # an hour later, by the window's timer
    assert len(answers) == 2 and _pump(app, lambda: answers[-1].isFinished())
    app.processEvents()
    assert float(window.settings.value("update/checked")) == 10 * day + 3600
    window.check_updates(now=10 * day + 7200)
    assert len(answers) == 2  # answered today
    window.check_updates(now=11 * day + 3600)
    assert len(answers) == 3  # the next day, the window still open
    assert window.update_timer.interval() == gui.UPDATE_TIMER_MS and window.update_timer.timeout
    answers[-1].wait()


def test_a_watched_folder_that_isnt_there_says_so_and_failures_reach_the_tray(window, tmp_path, monkeypatch):
    """A watched folder that went (renamed, a drive unplugged) was skipped without a word, and a watched file that
    failed while the window was in the notification area told no one."""
    here, gone = tmp_path / "here", tmp_path / "gone"
    here.mkdir()
    window.set_watch_list([{"folder": str(here), "target": "png"}, {"folder": str(gone), "target": "pdf"}])
    dialog = gui.WatchDialog(window)
    rows = [dialog.list.item(i).text() for i in range(dialog.list.count())]
    assert rows[0].endswith("PNG") and rows[1].startswith("Not there · ") and rows[1].endswith("PDF")
    assert "isn't there" in dialog.list.item(1).toolTip()
    dialog.close()
    told = []
    monkeypatch.setattr(gui.QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    window.settings.setValue("watch/tray", "true")
    window.show()
    window.close()  # to the notification area
    monkeypatch.setattr(window.tray, "showMessage", lambda *a: told.append(a[1]))
    watched = _photo(here, "new.png")
    window.job = gui.Job([watched], "pdf", [], out_dir=here / "Converted")
    window.job_done([], ["new.png: the file is damaged"])
    assert told == ["Couldn't convert new.png: the file is damaged"]
    window._quit_from_tray()


def test_details_say_the_editors_settings_in_words():
    """Details printed the editor's settings as kept: a crop as "10,5,80,60" and texts and drawings as JSON."""
    import json

    from latheport import options
    got = gui.settings_text(options.clean({
        "area": "10,5,80,60", "quality": 70, "colour": "bw", "start": "1",
        "texts": json.dumps([options.text_box("Hello there", "bottom")])}))
    assert "{" not in got and "10,5" not in got and "cropped, 1 text, black and white" in got
    assert gui.settings_text({"quality": 70}) == "quality 70"


# ---- where the text lands, measured in pixels from the window's own grab (tests/inkcheck.py)

@pytest.fixture(params=["light", "dark"])
def themed(request, app, tmp_path, monkeypatch):
    """A main window in the theme asked for (the next test's window gets the usual one back)."""
    settings = QtCore.QSettings(str(tmp_path / "settings.ini"), QtCore.QSettings.IniFormat)
    monkeypatch.setattr(gui, "QSettings", lambda *a: settings)
    monkeypatch.setattr(gui, "is_dark", lambda: request.param == "dark")
    gui.apply_theme(app)
    gc.collect()
    win = gui.Window()
    win.show()
    app.processEvents()
    yield win
    win.close()
    wait_for_previews(app)
    monkeypatch.undo()
    gui.apply_theme(app)


def test_editor_title_is_centred_over_the_tool_column_and_the_bar(themed, tmp_path, app):
    """'Edit' stood in the left of a box 12 px in from the edge: 11 px left of the middle of the tool column
    under it. Its ink is centred over that column and on the bar's line, and no two controls in the bar overlap."""
    from latheport.editor.look import RAIL

    from . import inkcheck
    e = _editor(themed, [_photo(tmp_path), _photo(tmp_path, "second.png")], "jpg")
    e.show()
    app.processEvents()
    found = inkcheck.measure(e.bar)
    title = next(i for i in found.values() if i.text == "Edit")
    assert abs((title.ink[0] + title.ink[2]) / 2 - RAIL / 2) <= 3, title.ink  # it was 11.5 out
    assert abs(title.cap_cy - e.bar.height() / 2) <= 2, title.cap_cy
    box = title.widget  # whatever font the ink is in: the title's box is the column's width, its words centred in it
    assert box.mapTo(e.bar, box.rect().center()).x() in (RAIL // 2 - 1, RAIL // 2) and box.alignment() & QtCore.Qt.AlignHCenter
    for tool in ("crop", "adjust", "text", "draw", "hide"):  # with two files the scope control takes room: a squeezed
        e.rail_buttons[tool].click()                        # Text bar had its slider under the Colour button
        app.processEvents()
        assert inkcheck.overlaps(e.bar) == [], tool
    e.close()


def test_main_header_version_is_centred_on_the_name(themed):
    """The small grey version sat low beside 'Latheport' (on its baseline): the middle of its digits is now level
    with the middle of the capital L, within 1 px, read from the pixels only."""
    from . import inkcheck
    found = inkcheck.measure(themed)
    title = next(i for i in found.values() if i.text == "Latheport")
    version = next(i for i in found.values() if i.text == gui.__version__)
    img = themed.grab().toImage()
    l_top, l_bottom = inkcheck.span(themed, title, img)
    v_top, v_bottom = inkcheck.span(themed, version, img, columns=version.ink[2] - version.ink[0])  # digits only
    assert abs((l_top + l_bottom) / 2 - (v_top + v_bottom) / 2) <= 1 * version.dpr
    assert version.ink[0] > title.ink[2]  # beside it, not on it
