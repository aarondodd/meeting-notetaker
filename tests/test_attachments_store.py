"""AttachmentsStore CRUD + sidecar I/O.

Pure-Python tests; no Qt. Uses the isolated_data_dir fixture so
session_dir() lands in tmp_path.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from meeting_notetaker.models.attachments import (
    ATTACHMENTS_SUBDIR,
    AttachmentRecord,
    AttachmentsStore,
    SIDECAR_NAME,
    SOURCE_CALENDAR,
    SOURCE_DROP,
    SOURCE_MANUAL,
    import_attachments,
    sanitize_basename,
)


def test_sanitize_basename_strips_unsafe_chars():
    assert sanitize_basename("a/b\\c:d*e?f.txt") == "a b c d e f.txt"


def test_sanitize_basename_collapses_whitespace():
    assert sanitize_basename("  many    spaces  .pdf") == "many spaces .pdf"


def test_sanitize_basename_falls_back_when_empty():
    assert sanitize_basename("") == "attachment"
    assert sanitize_basename("   ") == "attachment"
    assert sanitize_basename(None) == "attachment"  # type: ignore[arg-type]


def test_sanitize_basename_drops_trailing_dots():
    """Windows ignores trailing dots; sanitize trims them."""
    assert sanitize_basename("name...") == "name"


# ----------------------------------------------------------------------
# AttachmentsStore


@pytest.fixture
def src_file(tmp_path):
    """Stand-in source file outside the session dir."""
    p = tmp_path / "external" / "design-doc.pdf"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"fake pdf bytes")
    return p


def test_add_file_copies_into_attachments_dir(src_file):
    store = AttachmentsStore("sess-a")
    rec = store.add_file(src_file)
    assert rec.display_name == "design-doc.pdf"
    on_disk = store.attachments_dir / rec.stored_name
    assert on_disk.exists()
    # Source is untouched.
    assert src_file.exists()


def test_add_file_does_not_move_source(src_file):
    store = AttachmentsStore("sess-a")
    store.add_file(src_file)
    assert src_file.exists()
    assert src_file.read_bytes() == b"fake pdf bytes"


def test_add_file_records_size_and_mime(src_file):
    store = AttachmentsStore("sess-a")
    rec = store.add_file(src_file)
    assert rec.size == len(b"fake pdf bytes")
    # mimetypes.guess_type on a .pdf returns application/pdf.
    assert rec.mime == "application/pdf"


def test_add_file_collision_appends_counter(src_file, tmp_path):
    # Two adds of the same filename should both succeed; the second
    # gets a -2 counter inside its stored name.
    store = AttachmentsStore("sess-a")
    a = store.add_file(src_file)
    b = store.add_file(src_file)
    assert a.stored_name != b.stored_name
    # Both files exist on disk.
    assert (store.attachments_dir / a.stored_name).exists()
    assert (store.attachments_dir / b.stored_name).exists()


def test_list_returns_records_in_insertion_order(src_file, tmp_path):
    store = AttachmentsStore("sess-a")
    second = tmp_path / "external" / "agenda.docx"
    second.parent.mkdir(exist_ok=True)
    second.write_bytes(b"agenda content")
    a = store.add_file(src_file)
    b = store.add_file(second)
    records = store.list()
    assert [r.id for r in records] == [a.id, b.id]


def test_rename_changes_display_name_only(src_file):
    store = AttachmentsStore("sess-a")
    rec = store.add_file(src_file)
    on_disk_before = (store.attachments_dir / rec.stored_name).read_bytes()
    updated = store.rename(rec.id, "Final Design.pdf")
    assert updated is not None
    assert updated.display_name == "Final Design.pdf"
    # On-disk basename is the same.
    assert updated.stored_name == rec.stored_name
    on_disk_after = (store.attachments_dir / rec.stored_name).read_bytes()
    assert on_disk_after == on_disk_before


def test_rename_empty_name_raises(src_file):
    store = AttachmentsStore("sess-a")
    rec = store.add_file(src_file)
    with pytest.raises(ValueError):
        store.rename(rec.id, "")


def test_rename_unknown_id_returns_none(src_file):
    store = AttachmentsStore("sess-a")
    assert store.rename("not-a-real-id", "X") is None


def test_delete_removes_file_and_record(src_file):
    store = AttachmentsStore("sess-a")
    rec = store.add_file(src_file)
    assert store.delete(rec.id) is True
    assert not (store.attachments_dir / rec.stored_name).exists()
    assert store.list() == []


def test_delete_unknown_id_returns_false(src_file):
    store = AttachmentsStore("sess-a")
    store.add_file(src_file)
    assert store.delete("not-a-real-id") is False
    assert len(store.list()) == 1


def test_save_as_copies_to_destination(src_file, tmp_path):
    store = AttachmentsStore("sess-a")
    rec = store.add_file(src_file)
    dst = tmp_path / "out" / "exported.pdf"
    result = store.save_as(rec.id, dst)
    assert result == dst
    assert dst.exists()
    assert dst.read_bytes() == src_file.read_bytes()


def test_save_as_unknown_id_returns_none(tmp_path):
    store = AttachmentsStore("sess-a")
    assert store.save_as("not-a-real-id", tmp_path / "x.pdf") is None


def test_list_filters_out_missing_files(src_file):
    """An attachment whose on-disk file was rm'd outside our code
    should silently vanish from the list. Next save rewrites the
    sidecar without it."""
    store = AttachmentsStore("sess-a")
    rec = store.add_file(src_file)
    (store.attachments_dir / rec.stored_name).unlink()
    assert store.list() == []


def test_sidecar_written_on_each_mutation(src_file):
    store = AttachmentsStore("sess-a")
    store.add_file(src_file)
    sidecar = store.session_dir / SIDECAR_NAME
    assert sidecar.exists()
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert "attachments" in data
    assert len(data["attachments"]) == 1


def test_sidecar_garbage_recovers_cleanly(src_file):
    """A corrupt sidecar yields empty list, not a crash."""
    store = AttachmentsStore("sess-a")
    store.session_dir.mkdir(parents=True, exist_ok=True)
    (store.session_dir / SIDECAR_NAME).write_text("not json", encoding="utf-8")
    assert store.list() == []
    # And we can still add after that.
    rec = store.add_file(src_file)
    assert rec is not None
    assert len(store.list()) == 1


def test_source_value_validated(src_file):
    store = AttachmentsStore("sess-a")
    with pytest.raises(ValueError):
        store.add_file(src_file, source="not-a-real-source")


def test_source_metadata_preserved(src_file):
    store = AttachmentsStore("sess-a")
    rec = store.add_file(src_file, source=SOURCE_CALENDAR)
    assert rec.source == SOURCE_CALENDAR
    rec_back = store.get(rec.id)
    assert rec_back.source == SOURCE_CALENDAR


def test_file_path_returns_existing_path(src_file):
    store = AttachmentsStore("sess-a")
    rec = store.add_file(src_file)
    p = store.file_path(rec.id)
    assert p is not None
    assert p.exists()


def test_file_path_unknown_id_returns_none():
    store = AttachmentsStore("sess-a")
    assert store.file_path("missing") is None


def test_import_attachments_batch(src_file, tmp_path):
    """The bulk-add helper should add multiple files in one call."""
    a = tmp_path / "ext_a.txt"
    a.write_text("a")
    b = tmp_path / "ext_b.txt"
    b.write_text("b")
    records = import_attachments("sess-a", [a, b], source=SOURCE_DROP)
    assert len(records) == 2
    assert all(r.source == SOURCE_DROP for r in records)


def test_import_attachments_skips_missing_sources(tmp_path):
    """A non-existent path in the input list is silently skipped."""
    records = import_attachments("sess-a", [tmp_path / "ghost.txt"])
    assert records == []


def test_add_file_rejects_directory(src_file, tmp_path):
    store = AttachmentsStore("sess-a")
    with pytest.raises(FileNotFoundError):
        store.add_file(tmp_path / "external")  # the dir, not the file


def test_record_round_trips_through_sidecar(src_file):
    """A second AttachmentsStore opened on the same session dir
    should see the same records (sidecar is the persistence)."""
    store_a = AttachmentsStore("sess-a")
    a = store_a.add_file(src_file)
    store_b = AttachmentsStore("sess-a")  # fresh instance
    records = store_b.list()
    assert len(records) == 1
    assert records[0].id == a.id
    assert records[0].display_name == a.display_name
