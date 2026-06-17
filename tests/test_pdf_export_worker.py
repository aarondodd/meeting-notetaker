"""Background PDF export worker (#109).

Calls ``run()`` directly to exercise the worker logic synchronously
without spawning an OS thread -- the signals still fire from the
caller's thread, which is all we need for unit testing. Pure Qt
offscreen platform; no Word COM access (Linux test runtime).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.ui.session_view import _PdfExportWorker  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _make_worker(tmp_path: Path, **overrides) -> _PdfExportWorker:
    target = tmp_path / "out.pdf"
    sess_dir = tmp_path / "session"
    sess_dir.mkdir()
    defaults = dict(
        target_path=target,
        session_dir=sess_dir,
        session_title="Test Session",
        tab_label="My Notes",
        printable_markdown="# Title\n\nSome content paragraph.\n",
        body_markdown_for_anchors="",
        use_word=False,
        export_toc=False,
        export_heading_numbering=False,
        export_toc_max_depth=3,
    )
    defaults.update(overrides)
    return _PdfExportWorker(**defaults)


def test_worker_qt_path_writes_pdf_and_emits_success(qt_app, tmp_path):
    worker = _make_worker(tmp_path)
    fires: list[tuple] = []
    worker.finished_with_result.connect(
        lambda success, path_str, detail: fires.append(
            (success, path_str, detail),
        ),
    )
    worker.run()
    assert len(fires) == 1
    success, path_str, detail = fires[0]
    assert success is True
    target = Path(path_str)
    assert target.exists()
    assert target.stat().st_size > 0  # actual PDF bytes were written
    assert detail == ""  # Qt path -> empty detail


def test_worker_emits_progress_messages(qt_app, tmp_path):
    worker = _make_worker(tmp_path)
    msgs: list[str] = []
    worker.progress_message.connect(msgs.append)
    worker.run()
    # At least the 'Rendering...' message fires before finish.
    assert any("Rendering" in m for m in msgs), msgs


def test_worker_runs_navigation_post_process_when_toc_set(
    qt_app, tmp_path, monkeypatch,
):
    calls: list[tuple] = []

    def fake_add(target, body, *, toc_max_depth):
        calls.append((target, body, toc_max_depth))

    monkeypatch.setattr(
        "meeting_notetaker.utils.pdf_post_process.add_pdf_navigation",
        fake_add,
    )
    worker = _make_worker(
        tmp_path,
        body_markdown_for_anchors="# H1\n\n## H2\n\nbody\n",
        export_toc=True,
        export_toc_max_depth=2,
    )
    worker.run()
    assert len(calls) == 1
    target, body, toc_max_depth = calls[0]
    assert body == "# H1\n\n## H2\n\nbody\n"
    assert toc_max_depth == 2


def test_worker_skips_navigation_when_toc_off(qt_app, tmp_path, monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(
        "meeting_notetaker.utils.pdf_post_process.add_pdf_navigation",
        lambda *a, **k: calls.append((a, k)),
    )
    worker = _make_worker(
        tmp_path,
        body_markdown_for_anchors="# H1",
        export_toc=False,
        export_heading_numbering=False,
    )
    worker.run()
    # Add-navigation only runs when one of the outline opts is on.
    assert calls == []


def test_worker_failure_emits_failure_signal(qt_app, tmp_path, monkeypatch):
    """Render failure surfaces via finished_with_result(success=False)."""

    def fake_html(*_a, **_k):
        raise RuntimeError("rendering broke")

    monkeypatch.setattr(
        "meeting_notetaker.utils.print_html.markdown_to_print_html",
        fake_html,
    )
    worker = _make_worker(tmp_path)
    fires: list[tuple] = []
    worker.finished_with_result.connect(
        lambda success, path_str, detail: fires.append(
            (success, path_str, detail),
        ),
    )
    worker.run()
    assert len(fires) == 1
    success, _path, detail = fires[0]
    assert success is False
    assert "rendering broke" in detail


def test_worker_use_word_falls_back_to_qt_on_non_windows(qt_app, tmp_path):
    """On Linux (no pythoncom + no Word COM), use_word=True must
    cleanly fall back to the Qt backend, not silently drop the
    export."""
    worker = _make_worker(tmp_path, use_word=True)
    fires: list[tuple] = []
    worker.finished_with_result.connect(
        lambda success, path_str, detail: fires.append(
            (success, path_str, detail),
        ),
    )
    worker.run()
    assert len(fires) == 1
    success, path_str, detail = fires[0]
    assert success is True
    assert Path(path_str).exists()
    # detail == "" -> Qt path took over; would be "via Word" if Word
    # actually rendered, which can't happen on this runtime.
    assert detail == ""


def test_worker_object_name_set(qt_app, tmp_path):
    """objectName() drives Qt's thread debug labels; check it lands."""
    worker = _make_worker(tmp_path)
    assert worker.objectName() == "PdfExportWorker"
