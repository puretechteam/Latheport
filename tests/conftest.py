import pytest
from PIL import Image

from convertall import engines


def pytest_addoption(parser):
    parser.addoption("--e2e", action="store_true", help="convert every sample to every format it can reach")


@pytest.fixture(autouse=True)
def no_installs(monkeypatch):
    """Tests must never install software on the machine."""
    def refuse(key, log=None):
        raise AssertionError(f"test tried to install {key}")
    monkeypatch.setattr(engines, "install", refuse)


SEVEN_ZIP_PROGRAMS = {"7z", "7z.exe", "7za", "7za.exe", "7zz", "7zr", "7zr.exe", "7zg.exe", "7zfm.exe"}


@pytest.fixture(autouse=True)
def no_7zip(monkeypatch):
    """ConvertAll doesn't use 7-Zip. CI machines have it installed, so a leftover call would pass
    unnoticed: any test that starts a 7-Zip program fails."""
    import os
    import subprocess
    real = subprocess.Popen

    class Guarded(real):
        def __init__(self, args, *rest, **kwargs):
            if isinstance(args, str):  # a command line: its first word, quoted or not
                first = args[1:].split('"', 1)[0] if args.startswith('"') else (args.split() or [""])[0]
            else:
                first = str(args[0]) if isinstance(args, (list, tuple)) and args else str(args)
            name = os.path.basename(first.replace("\\", "/"))
            if name.lower() in SEVEN_ZIP_PROGRAMS:
                raise AssertionError(f"a test started 7-Zip ({first})")
            super().__init__(args, *rest, **kwargs)
    monkeypatch.setattr(subprocess, "Popen", Guarded)


@pytest.fixture
def without_engine(monkeypatch):
    """Pretend an engine isn't installed: without_engine('ffmpeg')."""
    def hide(*keys):
        real = engines.find

        def fake(key):
            return None if key in keys else real(key)
        monkeypatch.setattr(engines, "find", fake)
    return hide


def needs(engine: str):
    return pytest.mark.skipif(not engines.available(engine), reason=f"{engine} not installed")


@pytest.fixture
def png(tmp_path):
    path = tmp_path / "pic.png"
    img = Image.new("RGBA", (120, 80), (30, 120, 220, 255))
    img.paste((255, 0, 0, 0), (0, 0, 40, 40))  # transparent corner
    img.save(path)
    return path
