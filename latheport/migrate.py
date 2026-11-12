"""ConvertAll is now Latheport: the first time Latheport starts, what the old name kept comes along.

- Files (any launch, the window or the terminal): presets.json is copied, and the downloads (engines such as gmsh,
  the smart tools' runtime and models: hundreds of MB) are moved by renaming their folders, not copied.
- Settings (the window, before it reads them): watch folders, the last formats, update choices... copied from
  ConvertAll's store into Latheport's while Latheport's is still empty. ConvertAll's is left as it was.
- Windows (the installed app only, never a run from source): ConvertAll's right-click entries are taken out and
  Latheport's put in their place, pointing at this copy; the Start menu shortcut likewise.

Each step is safe to run again: it does nothing once its job is done, and nothing is deleted that isn't recreated
under the new name first."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

OLD = "ConvertAll"
NEW = "Latheport"
CARRIED = "carried/from"  # in the new settings: set once they've been carried over (or there was nothing)


def _appdata() -> Path:
    return Path(os.environ.get("APPDATA") or Path.home() / ".config")


def _local() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home())


def installed() -> bool:
    """The built app: only it carries anything over (a run from source, or the tests, never moves your files)."""
    return bool(getattr(sys, "frozen", False))


def at_start() -> list[str]:
    return carry_files() if installed() else []


def carry_files() -> list[str]:
    """presets.json and the downloads. Returns what came along (for the log)."""
    done = []
    if not os.environ.get("LATHEPORT_PRESETS"):  # (a test or portable setup names its own file)
        from .options import presets_file
        old, new = _appdata() / OLD / "presets.json", presets_file()
        if old.is_file() and not new.exists():
            try:
                new.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(old, new)
                done.append("presets")
            except OSError:
                pass
    for name in ("engines", "models"):
        old, new = _local() / OLD / name, _local() / NEW / name
        if old.is_dir() and not new.exists():
            try:
                new.parent.mkdir(parents=True, exist_ok=True)
                os.replace(old, new)  # same drive: a rename, instant whatever the size
                done.append(name)
            except OSError:  # in use (ConvertAll open with gmsh loaded): next launch tries again
                pass
    return done


def carry_settings(new, old) -> int:
    """Copy every setting from old into new (QSettings), once and only into an empty store. Returns how many."""
    if new.value(CARRIED) is not None or new.allKeys():
        return 0
    keys = old.allKeys()
    for k in keys:
        new.setValue(k, old.value(k))
    new.setValue(CARRIED, OLD)
    new.sync()
    return len(keys)


def carry_shell() -> list[str]:
    """The right-click menu and the Start menu shortcut, from ConvertAll's names to Latheport's (installed app)."""
    from . import gui
    done = []
    if sys.platform != "win32":
        return done
    def old(key: str) -> str:  # the same key with ConvertAll's name in its last part
        head, _, last = key.rpartition("\\")
        return f"{head}\\{last.replace(NEW, OLD)}"
    old_keys = {k: old(v) for k, v in (("star", gui._STAR_KEY), ("dir", gui._DIR_KEY), ("verb", gui._VERB),
                                       ("view", gui._VIEW_VERB))}
    if gui.key_exists(old_keys["star"]) or gui.key_exists(old_keys["dir"]):
        try:
            gui.install_context_menu()  # Latheport's entries first, then ConvertAll's go
            gui.remove_menu(old_keys["star"], old_keys["dir"], old_keys["verb"], old_keys["view"])
            done.append("menu")
        except OSError:
            pass
    old_link = gui.start_menu_path().with_name(f"{OLD}.lnk")
    if old_link.exists():
        try:
            if not gui.start_menu_installed():
                gui.install_start_menu()
            old_link.unlink()
            done.append("start menu")
        except OSError:
            pass
    return done
