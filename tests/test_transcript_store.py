"""TranscriptStore I/O, notes archive, metadata round-trip."""
from __future__ import annotations

from meeting_notetaker.models.transcript import (
    MIC,
    SYS,
    TranscriptSegment,
    TranscriptStore,
    format_segment,
)


def _seg(source, text, t_start, t_end, provisional=False):
    return TranscriptSegment(source=source, text=text, t_start=t_start, t_end=t_end, is_provisional=provisional)


def test_format_segment_uses_label_and_hms():
    seg = _seg(MIC, "hello", 65.0, 67.0)
    assert format_segment(seg) == "[00:01:05] Me: hello"
    seg2 = _seg(SYS, "  spaced  ", 3725.5, 3728.0)
    assert format_segment(seg2) == "[01:02:05] Them: spaced"


def test_append_and_round_trip(isolated_data_dir):
    store = TranscriptStore("s1")
    store.append_segments([_seg(MIC, "one", 0.0, 1.0), _seg(SYS, "two", 1.0, 2.0)])
    store.append_segments([_seg(MIC, "three", 2.0, 3.0)])
    body = store.read_transcript()
    assert body == "[00:00:00] Me: one\n[00:00:01] Them: two\n[00:00:02] Me: three\n"


def test_provisional_segments_skipped_on_append(isolated_data_dir):
    store = TranscriptStore("s_prov")
    store.append_segments([_seg(MIC, "real", 0.0, 1.0), _seg(MIC, "prov", 1.0, 2.0, provisional=True)])
    assert store.read_transcript() == "[00:00:00] Me: real\n"


def test_write_segments_sorts_by_start(isolated_data_dir):
    store = TranscriptStore("s2")
    store.write_segments([
        _seg(SYS, "second", 5.0, 6.0),
        _seg(MIC, "first", 0.0, 1.0),
        _seg(MIC, "third", 10.0, 11.0),
    ])
    body = store.read_transcript()
    expected = "[00:00:00] Me: first\n[00:00:05] Them: second\n[00:00:10] Me: third\n"
    assert body == expected


def test_save_notes_archives_existing(isolated_data_dir):
    store = TranscriptStore("s3")
    archive_path = store.save_notes("first pass notes")
    assert archive_path is None  # nothing pre-existing to archive
    archive_path = store.save_notes("second pass notes")
    assert archive_path is not None
    assert archive_path.exists()
    assert archive_path.read_text(encoding="utf-8") == "first pass notes"
    assert store.read_notes() == "second pass notes"
    assert len(store.list_previous_notes()) == 1


def test_save_notes_archive_disabled(isolated_data_dir):
    store = TranscriptStore("s4")
    store.save_notes("first")
    archive_path = store.save_notes("second", archive_existing=False)
    assert archive_path is None
    assert store.list_previous_notes() == []
    assert store.read_notes() == "second"


def test_metadata_round_trip(isolated_data_dir):
    store = TranscriptStore("s_meta")
    assert store.read_metadata() == {}
    store.write_metadata({"title": "x", "model": "small.en"})
    assert store.read_metadata() == {"title": "x", "model": "small.en"}
