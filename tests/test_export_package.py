"""Export-package orchestrator (issue #30).

The heavy lifters (PDF rendering, MP3 + MP4 encoding) are tested
separately + exercised live on Windows in the release pipeline.
These tests cover:

* The suggested-filename formatter (`default_package_filename`).
* The orchestrator's input handling: highlights mode validation,
  attachments + screenshots branches, progress reporting weights.

We mock the heavy encoder + PDF render calls so the test runs
under a second without spawning ffmpeg / PyQt.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from meeting_notetaker.utils.export_package import (
    HIGHLIGHTS_MODE_BOTH,
    HIGHLIGHTS_MODE_FULL,
    HIGHLIGHTS_MODE_HIGHLIGHTS,
    PackageOptions,
    build_session_package,
    default_package_filename,
)


def test_default_filename_includes_timestamp_and_title(monkeypatch):
    import time
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset not available")
    monkeypatch.setenv("TZ", "Pacific/Honolulu")
    time.tzset()
    name = default_package_filename(
        "Platform Team Sync",
        "2026-05-24T21:00:00Z",  # UTC -> 11:00 HST
    )
    assert name == "2026-05-24_1100 - Platform Team Sync.zip"


def test_default_filename_handles_unsafe_chars():
    name = default_package_filename(
        "Q&A: Bob's Plan / Phase\\3?",
        "",
    )
    # Sanitized; no path separators or other unsafe chars.
    assert "/" not in name
    assert "\\" not in name
    assert "?" not in name
    assert name.endswith(".zip")


def test_default_filename_garbage_timestamp_drops_stamp():
    name = default_package_filename("Title", "not-a-date")
    assert name == "Title.zip"


def test_default_filename_empty_session_title_falls_back():
    name = default_package_filename("", "")
    assert name == "session.zip"


# ----------------------------------------------------------------------
# build_session_package -- mocked heavies


def _stub_options(tmp_path: Path, **overrides) -> PackageOptions:
    return PackageOptions(
        session_id="sess-a",
        session_title=overrides.get("session_title", "Test"),
        session_started_at_iso=overrides.get(
            "session_started_at_iso", "2026-05-24T10:00:00Z",
        ),
        mic_path=overrides.get("mic_path"),
        sys_path=overrides.get("sys_path"),
        screenshots=overrides.get("screenshots", []),
        transcript_text=overrides.get(
            "transcript_text", "[00:00:01] Speaker: hello.\n",
        ),
        notes_md=overrides.get("notes_md", "# Notes\nbody"),
        synthesis_md=overrides.get("synthesis_md", "# Synthesis\nbody"),
        attachments=overrides.get("attachments", []),
        highlights=overrides.get("highlights", []),
        highlights_mode=overrides.get(
            "highlights_mode", HIGHLIGHTS_MODE_FULL,
        ),
    )


@pytest.fixture
def patched_heavies():
    """Stub the encoder calls so the orchestrator focuses on its
    own glue logic, not on actually running PyAV / PyQt PDF write."""
    with patch(
        "meeting_notetaker.utils.export_package._render_markdown_to_pdf"
    ) as render_pdf, patch(
        "meeting_notetaker.utils.export_package._export_full_audio"
    ) as full_audio, patch(
        "meeting_notetaker.utils.export_package._export_highlights_audio"
    ) as hl_audio, patch(
        "meeting_notetaker.utils.export_package._export_full_video"
    ) as full_video, patch(
        "meeting_notetaker.utils.export_package._export_highlights_video"
    ) as hl_video:
        # Make the PDF stub actually write a placeholder file so
        # zipfile has something to compress.
        def _write_pdf_stub(body, dst, *, title):
            Path(dst).write_text(f"PDF placeholder for {title}", encoding="utf-8")
        render_pdf.side_effect = _write_pdf_stub

        def _write_audio_stub(mic, sys_, dst, progress):
            Path(dst).write_bytes(b"fake mp3")
            progress(100)
        full_audio.side_effect = _write_audio_stub

        def _write_hl_audio_stub(mic, sys_, highlights, dst, progress):
            Path(dst).write_bytes(b"fake hl mp3")
            progress(100)
        hl_audio.side_effect = _write_hl_audio_stub

        def _write_video_stub(mic, sys_, screenshots, transcript, dst, progress):
            Path(dst).write_bytes(b"fake mp4")
            progress(100)
        full_video.side_effect = _write_video_stub

        def _write_hl_video_stub(
            mic, sys_, screenshots, transcript, highlights, dst,
            *, session_title, session_started_at_iso, progress,
        ):
            Path(dst).write_bytes(b"fake hl mp4")
            progress(100)
        hl_video.side_effect = _write_hl_video_stub

        yield {
            "render_pdf": render_pdf,
            "full_audio": full_audio,
            "hl_audio": hl_audio,
            "full_video": full_video,
            "hl_video": hl_video,
        }


def test_build_zip_with_only_notes(tmp_path, patched_heavies):
    """Minimum-config session: notes + synthesis + transcript only.
    No audio, no screenshots, no attachments. Zip should still pack."""
    options = _stub_options(tmp_path)
    dst = tmp_path / "out.zip"
    build_session_package(options, dst)
    assert dst.exists()
    import zipfile
    with zipfile.ZipFile(dst) as zf:
        names = set(zf.namelist())
    assert "my-notes.pdf" in names
    assert "synthesis.pdf" in names
    assert "transcript.txt" in names
    # No audio / video / attachments / screenshots dirs.
    assert not any(n.startswith("audio/") for n in names)
    assert not any(n.startswith("attachments/") for n in names)
    assert not any(n.startswith("screenshots/") for n in names)


def test_build_zip_with_audio_and_screenshots(tmp_path, patched_heavies):
    """When mic + screenshots are present, audio.mp3 + video.mp4
    should both land in audio/."""
    mic = tmp_path / "mic.wav"
    mic.write_bytes(b"fake wav")
    shot = tmp_path / "0001.png"
    shot.write_bytes(b"fake png")
    options = _stub_options(
        tmp_path,
        mic_path=mic,
        screenshots=[(shot, 0)],
    )
    dst = tmp_path / "out.zip"
    build_session_package(options, dst)
    import zipfile
    with zipfile.ZipFile(dst) as zf:
        names = set(zf.namelist())
    assert "audio/recording.mp3" in names
    assert "audio/recording.mp4" in names
    assert "screenshots/0001.png" in names


def test_build_zip_skips_video_when_no_screenshots(tmp_path, patched_heavies):
    mic = tmp_path / "mic.wav"
    mic.write_bytes(b"fake wav")
    options = _stub_options(tmp_path, mic_path=mic, screenshots=[])
    dst = tmp_path / "out.zip"
    build_session_package(options, dst)
    import zipfile
    with zipfile.ZipFile(dst) as zf:
        names = set(zf.namelist())
    assert "audio/recording.mp3" in names
    assert "audio/recording.mp4" not in names


def test_highlights_only_mode_skips_full(tmp_path, patched_heavies):
    """Highlights-only: no full recording, only highlight artifacts."""
    mic = tmp_path / "mic.wav"
    mic.write_bytes(b"fake wav")
    shot = tmp_path / "0001.png"
    shot.write_bytes(b"fake png")
    # Use a stand-in highlight object -- the orchestrator passes it
    # through to the (stubbed) encoder; the real Highlight class
    # carries start_ms/end_ms which the stub ignores.
    from meeting_notetaker.models.highlights import Highlight
    options = _stub_options(
        tmp_path,
        mic_path=mic,
        screenshots=[(shot, 0)],
        highlights=[Highlight(0, 1000, "Decision")],
        highlights_mode=HIGHLIGHTS_MODE_HIGHLIGHTS,
    )
    dst = tmp_path / "out.zip"
    build_session_package(options, dst)
    import zipfile
    with zipfile.ZipFile(dst) as zf:
        names = set(zf.namelist())
    assert "audio/highlights.mp3" in names
    assert "audio/highlights.mp4" in names
    # No full recording in highlights-only mode.
    assert "audio/recording.mp3" not in names
    assert "audio/recording.mp4" not in names


def test_both_mode_emits_full_and_highlights(tmp_path, patched_heavies):
    mic = tmp_path / "mic.wav"
    mic.write_bytes(b"fake wav")
    shot = tmp_path / "0001.png"
    shot.write_bytes(b"fake png")
    from meeting_notetaker.models.highlights import Highlight
    options = _stub_options(
        tmp_path,
        mic_path=mic,
        screenshots=[(shot, 0)],
        highlights=[Highlight(0, 1000, "Decision")],
        highlights_mode=HIGHLIGHTS_MODE_BOTH,
    )
    dst = tmp_path / "out.zip"
    build_session_package(options, dst)
    import zipfile
    with zipfile.ZipFile(dst) as zf:
        names = set(zf.namelist())
    assert "audio/recording.mp3" in names
    assert "audio/recording.mp4" in names
    assert "audio/highlights.mp3" in names
    assert "audio/highlights.mp4" in names


def test_attachments_land_under_attachments_dir(tmp_path, patched_heavies):
    """Attachment input is [(Path, display_name), ...]. The package
    should write each into attachments/ with the display name
    sanitized."""
    src = tmp_path / "design-doc.pdf"
    src.write_bytes(b"design content")
    options = _stub_options(
        tmp_path,
        attachments=[(src, "Final Design Doc.pdf")],
    )
    dst = tmp_path / "out.zip"
    build_session_package(options, dst)
    import zipfile
    with zipfile.ZipFile(dst) as zf:
        names = zf.namelist()
    assert any(
        n.startswith("attachments/") and "Final Design Doc.pdf" in n
        for n in names
    )


def test_unknown_highlights_mode_raises(tmp_path):
    options = _stub_options(tmp_path, highlights_mode="bogus")
    with pytest.raises(ValueError, match="unknown highlights_mode"):
        build_session_package(options, tmp_path / "out.zip")


def test_progress_reaches_100(tmp_path, patched_heavies):
    seen: list[int] = []
    options = _stub_options(tmp_path)
    build_session_package(
        options, tmp_path / "out.zip",
        progress=seen.append,
    )
    assert 100 in seen
    # And progress is monotonically non-decreasing.
    assert all(a <= b for a, b in zip(seen, seen[1:]))


def test_partial_archive_removed_on_failure(tmp_path, monkeypatch):
    """If something raises mid-pipeline, the dst zip (if any was
    written) is deleted so the user doesn't see a junk archive."""
    options = _stub_options(tmp_path)
    dst = tmp_path / "out.zip"

    def _boom(*_a, **_kw):
        raise RuntimeError("intentional test failure")
    monkeypatch.setattr(
        "meeting_notetaker.utils.export_package._render_markdown_to_pdf",
        _boom,
    )
    with pytest.raises(RuntimeError):
        build_session_package(options, dst)
    assert not dst.exists()
