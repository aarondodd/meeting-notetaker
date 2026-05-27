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


def test_write_segments_is_atomic(isolated_data_dir):
    """Issue #38: write_segments must not leave a partial file on
    disk. The .tmp dance + Path.replace gives us atomic rewrite, so a
    concurrent reader either sees the prior version or the new one --
    never a half-written file.

    We can't easily simulate a concurrent read here, but we can pin
    the contract: after write_segments returns, no .tmp sibling
    should be sitting next to the real file, and the real file's
    contents must match the rendered segments exactly."""
    store = TranscriptStore("atomic1")
    store.write_segments([
        _seg(MIC, "first", 0.0, 1.0),
        _seg(MIC, "second", 1.0, 2.0),
    ])
    tmp_sibling = store.transcript_path.with_name(
        store.transcript_path.name + ".tmp",
    )
    assert not tmp_sibling.exists(), (
        "atomic-write tmp must be replaced into the real path, not left over"
    )
    assert store.transcript_path.read_text(encoding="utf-8") == (
        "[00:00:00] Me: first\n[00:00:01] Me: second\n"
    )


def test_save_live_notes_is_atomic(isolated_data_dir):
    """live_notes.md is overwritten on every debounced keystroke; an
    in-place rewrite could truncate if the app crashes mid-write."""
    store = TranscriptStore("atomic2")
    store.save_live_notes("# Attendees\n- Aaron\n\n# Notes\n- one\n")
    tmp_sibling = store.live_notes_path.with_name(
        store.live_notes_path.name + ".tmp",
    )
    assert not tmp_sibling.exists()
    assert "Aaron" in store.live_notes_path.read_text(encoding="utf-8")
    # Second write replaces cleanly (no .tmp residue).
    store.save_live_notes("# Attendees\n- Aaron\n- Beth\n")
    assert not tmp_sibling.exists()
    assert "Beth" in store.live_notes_path.read_text(encoding="utf-8")


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


def test_restore_previous_notes_round_trips(isolated_data_dir):
    """Restoring an archive must swap it into notes.md AND archive the
    current notes.md first (so the user can roll back the rollback)."""
    store = TranscriptStore("s_restore")
    store.save_notes("v1: first pass")
    store.save_notes("v2: second pass")
    archives = store.list_previous_notes()
    assert len(archives) == 1
    v1_archive = archives[0]
    assert v1_archive.read_text(encoding="utf-8") == "v1: first pass"

    # Restore v1.
    new_archive = store.restore_previous_notes(v1_archive)
    assert new_archive is not None  # current notes was archived
    assert new_archive.read_text(encoding="utf-8") == "v2: second pass"
    assert store.read_notes() == "v1: first pass"


def test_restore_previous_notes_rejects_path_outside_session(isolated_data_dir, tmp_path):
    store = TranscriptStore("s_restore_safe")
    stray = tmp_path / "elsewhere.md"
    stray.write_text("not in session dir")
    import pytest
    with pytest.raises(ValueError, match="not in session dir"):
        store.restore_previous_notes(stray)


def test_delete_previous_notes_removes_archive(isolated_data_dir):
    store = TranscriptStore("s_del")
    store.save_notes("v1")
    store.save_notes("v2")
    archives = store.list_previous_notes()
    assert len(archives) == 1
    store.delete_previous_notes(archives[0])
    assert store.list_previous_notes() == []
    # notes.md still intact.
    assert store.read_notes() == "v2"


def test_delete_previous_notes_refuses_live_file(isolated_data_dir):
    store = TranscriptStore("s_del_safe")
    store.save_notes("only version")
    import pytest
    with pytest.raises(ValueError, match="live notes"):
        store.delete_previous_notes(store.notes_path)


def test_prompt_template_name_round_trip(isolated_data_dir):
    """Per-session prompt template persists in metadata.json so the
    user's choice survives app restarts and session reloads."""
    store = TranscriptStore("s_prompt")
    # Default is empty (use the bundled default at render time).
    assert store.read_prompt_template_name() == ""

    store.write_prompt_template_name("standup")
    assert store.read_prompt_template_name() == "standup"

    # Round-trip through a fresh TranscriptStore instance (simulates
    # an app restart -- only on-disk state should drive the result).
    fresh = TranscriptStore("s_prompt")
    assert fresh.read_prompt_template_name() == "standup"


def test_prompt_template_name_clear(isolated_data_dir):
    """Writing an empty string clears the override (back to default)."""
    store = TranscriptStore("s_prompt_clear")
    store.write_prompt_template_name("one-on-one")
    assert store.read_prompt_template_name() == "one-on-one"
    store.write_prompt_template_name("")
    assert store.read_prompt_template_name() == ""


def test_prompt_template_name_preserves_other_metadata(isolated_data_dir):
    """Setting the template must not stomp other metadata fields."""
    store = TranscriptStore("s_prompt_preserve")
    store.write_metadata({"title": "Quarterly review", "model": "small.en"})
    store.write_prompt_template_name("standup")
    meta = store.read_metadata()
    assert meta["title"] == "Quarterly review"
    assert meta["model"] == "small.en"
    assert meta["prompt_template_name"] == "standup"


def test_delete_previous_notes_refuses_non_archive(isolated_data_dir, tmp_path):
    """Refuse to delete files that don't match the notes-*.md pattern,
    even if they're in the session dir."""
    store = TranscriptStore("s_del_pattern")
    rogue = store.session_dir / "live_notes.md"
    rogue.write_text("live notes content")
    import pytest
    with pytest.raises(ValueError, match="doesn't match"):
        store.delete_previous_notes(rogue)
    # The file still exists.
    assert rogue.exists()


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
