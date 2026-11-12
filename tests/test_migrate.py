"""ConvertAll is now Latheport: what the old name kept comes along on the first launch (migrate.py). Everything here
runs on scratch data: scratch APPDATA and LOCALAPPDATA folders, ini-file settings, and a registry branch of its own
under HKEY_CURRENT_USER\\Software\\LatheportTests (removed afterwards), never the real right-click menu."""

from __future__ import annotations

import sys
import uuid

import pytest
from PySide6 import QtCore

from latheport import gui, migrate


@pytest.fixture
def homes(tmp_path, monkeypatch):
    roam, local = tmp_path / "Roaming", tmp_path / "Local"
    roam.mkdir()
    local.mkdir()
    monkeypatch.setenv("APPDATA", str(roam))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.delenv("LATHEPORT_PRESETS", raising=False)
    return roam, local


def _old_downloads(local):
    (local / "ConvertAll" / "engines" / "gmsh").mkdir(parents=True)
    (local / "ConvertAll" / "engines" / "gmsh" / "gmsh.py").write_text("# gmsh", encoding="utf-8")
    (local / "ConvertAll" / "models" / "ocr").mkdir(parents=True)
    (local / "ConvertAll" / "models" / "ocr" / "read.onnx").write_bytes(b"\x08model" * 1000)


def test_presets_and_downloads_come_along_once(homes):
    roam, local = homes
    (roam / "ConvertAll").mkdir()
    (roam / "ConvertAll" / "presets.json").write_text('{"Web": {"format": "webp"}}', encoding="utf-8")
    _old_downloads(local)
    assert sorted(migrate.carry_files()) == ["engines", "models", "presets"]
    assert (roam / "Latheport" / "presets.json").read_text(encoding="utf-8") == '{"Web": {"format": "webp"}}'
    assert (roam / "ConvertAll" / "presets.json").exists()  # copied: ConvertAll keeps its own
    assert (local / "Latheport" / "engines" / "gmsh" / "gmsh.py").read_text(encoding="utf-8") == "# gmsh"
    assert (local / "Latheport" / "models" / "ocr" / "read.onnx").read_bytes() == b"\x08model" * 1000
    assert not (local / "ConvertAll" / "engines").exists()  # moved, not copied: hundreds of MB
    assert migrate.carry_files() == []  # the second time there's nothing to do


def test_nothing_of_latheports_own_is_overwritten(homes):
    roam, local = homes
    for name in ("ConvertAll", "Latheport"):
        (roam / name).mkdir()
        (roam / name / "presets.json").write_text(name, encoding="utf-8")
    _old_downloads(local)
    (local / "Latheport" / "engines").mkdir(parents=True)
    assert migrate.carry_files() == ["models"]
    assert (roam / "Latheport" / "presets.json").read_text(encoding="utf-8") == "Latheport"
    assert (local / "ConvertAll" / "engines" / "gmsh" / "gmsh.py").exists()  # left where it was


def test_a_run_from_source_or_a_test_moves_nothing(homes):
    _, local = homes
    _old_downloads(local)
    assert not migrate.installed()
    assert migrate.at_start() == []
    assert (local / "ConvertAll" / "engines" / "gmsh" / "gmsh.py").exists()


def test_settings_come_along_into_an_empty_store(tmp_path):
    old = QtCore.QSettings(str(tmp_path / "old.ini"), QtCore.QSettings.IniFormat)
    for k, v in (("watch/list", '[{"folder": "C:/In", "format": "png"}]'), ("update/skip", "1.4.0"),
                 ("last/png", "webp")):
        old.setValue(k, v)
    old.sync()
    new = QtCore.QSettings(str(tmp_path / "new.ini"), QtCore.QSettings.IniFormat)
    assert migrate.carry_settings(new, old) == 3
    assert new.value("watch/list") == '[{"folder": "C:/In", "format": "png"}]' and new.value("last/png") == "webp"
    assert new.value(migrate.CARRIED) == "ConvertAll"
    old.setValue("update/skip", "9.9.9")
    assert migrate.carry_settings(new, old) == 0 and new.value("update/skip") == "1.4.0"  # only ever once
    fresh = QtCore.QSettings(str(tmp_path / "used.ini"), QtCore.QSettings.IniFormat)
    fresh.setValue("theme", "dark")
    assert migrate.carry_settings(fresh, old) == 0 and fresh.value("watch/list") is None  # Latheport's own stays


@pytest.mark.skipif(sys.platform != "win32", reason="the right-click menu is Windows'")
def test_the_right_click_menu_and_start_menu_move_to_the_new_name(tmp_path, monkeypatch):
    import winreg
    base = rf"Software\LatheportTests\{uuid.uuid4().hex}"
    classes = base + r"\Classes"
    monkeypatch.setattr(gui, "_STAR_KEY", classes + r"\*\shell\Latheport")
    monkeypatch.setattr(gui, "_DIR_KEY", classes + r"\Directory\shell\Latheport")
    monkeypatch.setattr(gui, "_SFA", classes + r"\SystemFileAssociations")
    monkeypatch.setattr(gui, "extract_extensions", lambda: ["zip"])
    monkeypatch.setattr(gui, "view_extensions", lambda: ["stl"])
    monkeypatch.setattr(gui, "_launch_command", lambda args="": f'"C:\\Apps\\Latheport\\Latheport.exe" {args}"%1"')
    links = tmp_path / "Programs"
    links.mkdir()
    monkeypatch.setattr(gui, "start_menu_path", lambda: links / "Latheport.lnk")
    monkeypatch.setattr(gui, "install_start_menu", lambda: (links / "Latheport.lnk").write_text("link"))
    old_cmd = '"C:\\Old\\ConvertAll.exe" "%1"'
    try:
        for key in (classes + r"\*\shell\ConvertAll", classes + r"\Directory\shell\ConvertAll",
                    classes + r"\SystemFileAssociations\.zip\shell\ConvertAll",
                    classes + r"\SystemFileAssociations\.stl\shell\ConvertAllView",
                    classes + r"\SystemFileAssociations\.zip\shell\OtherApp"):
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key + r"\command") as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, old_cmd)
        (links / "ConvertAll.lnk").write_text("old link")
        assert migrate.carry_shell() == ["menu", "start menu"]
        assert not gui.key_exists(classes + r"\*\shell\ConvertAll")
        assert not gui.key_exists(classes + r"\Directory\shell\ConvertAll")
        assert not gui.key_exists(classes + r"\SystemFileAssociations\.zip\shell\ConvertAll")
        assert not gui.key_exists(classes + r"\SystemFileAssociations\.stl\shell\ConvertAllView")
        assert gui.key_exists(classes + r"\SystemFileAssociations\.zip\shell\OtherApp")  # another app's stays
        assert gui.registered_command().startswith('"C:\\Apps\\Latheport\\Latheport.exe"')
        assert gui.key_exists(classes + r"\SystemFileAssociations\.zip\shell\Latheport\command")
        assert gui.key_exists(classes + r"\SystemFileAssociations\.stl\shell\LatheportView\command")
        assert (links / "Latheport.lnk").exists() and not (links / "ConvertAll.lnk").exists()
        assert migrate.carry_shell() == []  # nothing of ConvertAll's is left to move
    finally:
        _delete_tree(winreg, base)
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, r"Software\LatheportTests")  # only goes if it's empty
        except OSError:
            pass
    assert not gui.key_exists(base)


def _delete_tree(winreg, key: str) -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            subs = [winreg.EnumKey(k, i) for i in range(winreg.QueryInfoKey(k)[0])]
    except FileNotFoundError:
        return
    for s in subs:
        _delete_tree(winreg, key + "\\" + s)
    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
