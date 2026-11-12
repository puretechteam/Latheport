import os
import tempfile
from pathlib import Path

import pytest
from PIL import Image

from latheport import engines, smart

# the smart tools' models for the tests that run them: downloaded once (checked against their pinned
# SHA-256s) into a folder of their own, never the one the app uses; CI keeps it between runs
TEST_MODELS = Path(os.environ.get("LATHEPORT_TEST_MODELS") or Path(tempfile.gettempdir()) / "latheport-test-models")


def pytest_addoption(parser):
    parser.addoption("--e2e", action="store_true", help="convert every sample to every format it can reach")


@pytest.fixture(autouse=True)
def no_installs(monkeypatch):
    """Tests must never install software on the machine."""
    def refuse(key, log=None):
        raise AssertionError(f"test tried to install {key}")
    monkeypatch.setattr(engines, "install", refuse)


@pytest.fixture(autouse=True)
def no_models(monkeypatch, tmp_path_factory):
    """No smart tool is installed unless a test asks for them (smart_models): the app's own downloads on this
    computer are never read, so every test sees the same thing on any machine."""
    monkeypatch.setattr(smart, "MODELS", str(tmp_path_factory.getbasetemp() / "no-models"))
    for key in smart.TOOLS:
        engines._cache.pop(key, None)


@pytest.fixture(scope="session")
def model_cache():
    """TEST_MODELS with every smart tool in it, downloaded the first time; skipped without the network or
    ONNX Runtime."""
    if not smart.runtime_found():
        pytest.skip("ONNX Runtime isn't installed (pip install onnxruntime)")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(smart, "MODELS", str(TEST_MODELS))
        for key in smart.TOOLS:
            if not smart.found(key):
                try:
                    smart.download(key)
                except Exception as exc:  # noqa: BLE001 - offline: these tests can't run
                    pytest.skip(f"couldn't download the {key} model: {exc}")
    return TEST_MODELS


@pytest.fixture
def smart_models(monkeypatch, model_cache):
    """Every smart tool installed (from the test cache) for this test."""
    monkeypatch.setattr(smart, "MODELS", str(model_cache))
    engines.refresh()
    yield model_cache
    engines.refresh()


SEVEN_ZIP_PROGRAMS = {"7z", "7z.exe", "7za", "7za.exe", "7zz", "7zr", "7zr.exe", "7zg.exe", "7zfm.exe"}


@pytest.fixture(autouse=True)
def no_7zip(monkeypatch):
    """Latheport doesn't use 7-Zip. CI machines have it installed, so a leftover call would pass
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
