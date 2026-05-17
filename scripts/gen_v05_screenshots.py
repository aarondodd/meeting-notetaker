"""Generate v0.5 screenshots for docs/.

Usage:
    DISPLAY=:10 .venv/bin/python scripts/gen_v05_screenshots.py

Uses an X display (xvfb or a real one) so the OS-native style matches
the earlier screenshots in docs/screenshots/. Falls back to
QT_QPA_PLATFORM=offscreen if DISPLAY isn't set, but offscreen renders
controls a little differently so prefer a real display when possible.

Covers the v0.5 additions:
- Speaker walker (label + review modes)
- Manage Speakers dialog
- Settings dialog (with new Speakers section)
- Session view with Review Speakers... button
- Status bar with speakers indicator
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import numpy as np

_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


def _ensure_qt_platform() -> None:
    if not os.environ.get("DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


_ensure_qt_platform()

from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.diarization.store import SpeakerStore  # noqa: E402
from meeting_notetaker.ui.speaker_walker_dialog import (  # noqa: E402
    SpeakerWalkerDialog,
    SpeakerWalkerEntry,
)
from meeting_notetaker.ui.speakers_manage_dialog import SpeakersManageDialog  # noqa: E402


OUT_DIR = Path(__file__).resolve().parent.parent / "docs" / "screenshots"


def _unit_vec(*c: float) -> np.ndarray:
    arr = np.asarray(c, dtype=np.float32)
    return arr / max(np.linalg.norm(arr), 1e-8)


def _grab(widget, name: str) -> None:
    widget.adjustSize()
    pixmap = widget.grab()
    out = OUT_DIR / name
    pixmap.save(str(out), "PNG")
    print(f"wrote {out}")


def shot_label_dialog():
    entries = [
        SpeakerWalkerEntry(
            cluster_id=0,
            current_name=None,
            example_lines=[
                "[00:02:14] Speaker 1: thanks for joining everyone, let's start with the platform updates",
                "[00:05:42] Speaker 1: I think we need to revisit the API contract on the read path",
                "[00:11:08] Speaker 1: from my side I'd push for an end-of-month timeline on this",
            ],
            centroid=_unit_vec(1, 0, 0),
            match_similarity=None,
            suggestions=["Alice", "Carol", "Bob"],
        ),
        SpeakerWalkerEntry(
            cluster_id=1,
            current_name=None,
            example_lines=[
                "[00:03:21] Speaker 2: agreed. We should also flag the dependency on the migration",
                "[00:09:47] Speaker 2: yeah, the integration team caught that one last sprint",
            ],
            centroid=_unit_vec(0, 1, 0),
            match_similarity=None,
            suggestions=["Alice", "Carol", "Bob"],
        ),
    ]
    dlg = SpeakerWalkerDialog(entries, mode="label", session_title="Platform sync 2026-05-17")
    dlg.show()
    QApplication.processEvents()
    _grab(dlg, "12-dialog-label-unknown-speakers.png")
    dlg.close()


def shot_review_dialog():
    entries = [
        SpeakerWalkerEntry(
            cluster_id=0,
            current_name="Alice",
            example_lines=[
                "[00:02:14] Alice: thanks for joining everyone, let's start with the platform updates",
                "[00:11:08] Alice: from my side I'd push for an end-of-month timeline on this",
            ],
            centroid=_unit_vec(1, 0, 0),
            match_similarity=0.86,
            suggestions=["Alice", "Bob", "Carol"],
        ),
        SpeakerWalkerEntry(
            cluster_id=1,
            current_name="Bob",
            example_lines=[
                "[00:03:21] Bob: agreed. We should also flag the dependency on the migration",
            ],
            centroid=_unit_vec(0, 1, 0),
            match_similarity=0.78,
            suggestions=["Alice", "Bob", "Carol"],
        ),
        SpeakerWalkerEntry(
            cluster_id=2,
            current_name=None,
            example_lines=[
                "[00:18:05] Speaker 3: I have one more thing if there's time at the end",
            ],
            centroid=_unit_vec(0, 0, 1),
            match_similarity=None,
            suggestions=["Alice", "Bob", "Carol"],
        ),
    ]
    dlg = SpeakerWalkerDialog(entries, mode="review", session_title="Platform sync 2026-05-17")
    dlg.show()
    QApplication.processEvents()
    _grab(dlg, "13-dialog-review-speakers.png")
    dlg.close()


def shot_manage_dialog():
    with tempfile.TemporaryDirectory() as td:
        store = SpeakerStore(Path(td) / "speakers.db")
        try:
            store.upsert("Alice", _unit_vec(1, 0, 0), sample_count=7)
            store.upsert("Bob", _unit_vec(0, 1, 0), sample_count=3)
            store.upsert("Carol", _unit_vec(0, 0, 1), sample_count=12)
            store.upsert("Dan", _unit_vec(0.5, 0.5, 0), sample_count=2)
            dlg = SpeakersManageDialog(store)
            dlg.show()
            QApplication.processEvents()
            _grab(dlg, "14-dialog-manage-speakers.png")
            dlg.close()
        finally:
            store.close()


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    shot_label_dialog()
    shot_review_dialog()
    shot_manage_dialog()
    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
