"""Big extractions say how far they've got: the bytes written so far against what the archive
says it holds, in the status line ("Extracting · big.rar · 123 of 640 MB"), at most a few times
a second, for archives over 50 MB."""

import pytest

pytest.importorskip("PySide6")

from latheport import cancel, gui, pipeline
from latheport.safety import Budget, copy_capped

MB = 1 << 20


class _Zeros:
    """A readable of n zero bytes, made as they're read."""

    def __init__(self, n: int):
        self.left = n

    def read(self, k: int) -> bytes:
        k = min(k, self.left)
        self.left -= k
        return bytes(k)


def test_copy_reports_bytes_against_the_declared_total(tmp_path, monkeypatch):
    clock = iter(range(10_000))
    monkeypatch.setattr(cancel.time, "monotonic", lambda: next(clock))  # a second passes at every look
    seen = []
    token = cancel.Token()
    token.listener = lambda done, total: seen.append((done, total))
    with cancel.using(token):
        budget = Budget(tmp_path, "big.bin")
        budget.check(12 * MB)
        copy_capped(_Zeros(12 * MB), tmp_path / "big.bin", 12 * MB, budget, "big.bin")
    assert seen and seen[-1] == (12 * MB, 12 * MB)
    assert [d for d, _ in seen] == sorted(d for d, _ in seen)
    assert all(t == 12 * MB for _, t in seen)


def test_the_status_line_shows_mb_done_for_big_archives(tmp_path, monkeypatch):
    from latheport.qtdraw import app as qt_app
    qt_app()  # kept for the whole session
    (tmp_path / "big.zip").write_bytes(b"x")
    (tmp_path / "small.zip").write_bytes(b"x")

    def fake_extract(src, notes=None):
        size = 640 * MB if src.name == "big.zip" else 10 * MB
        Budget(tmp_path, src.name).check(size)
        cancel.moved(size // 5)
        cancel.moved(size - size // 5)
        return tmp_path

    monkeypatch.setattr(pipeline, "extract", fake_extract)
    monkeypatch.setattr(cancel.time, "monotonic", iter(range(0, 100_000, 5)).__next__)
    lines = []
    job = gui.Job([tmp_path / "big.zip", tmp_path / "small.zip"], None, [])
    job.progress.connect(lines.append)
    job.run()  # on this thread: the signal reaches lines directly
    assert "Extracting 1 of 2 · big.zip · 128 of 640 MB" in lines
    assert "Extracting 1 of 2 · big.zip · 640 of 640 MB" in lines
    assert not any("small.zip ·" in line and "MB" in line for line in lines)  # 10 MB: no count
