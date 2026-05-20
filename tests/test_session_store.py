"""SessionStore CRUD + WAL + orphan detection."""
from __future__ import annotations

from meeting_notetaker.models.session import (
    STATE_COMPLETE,
    STATE_NEW,
    STATE_PAUSED,
    STATE_PROCESSING,
    STATE_RECORDING,
    SessionStore,
)


def test_wal_mode_enabled(isolated_data_dir):
    with SessionStore() as store:
        mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"


def test_session_create_get_list_update_delete(isolated_data_dir):
    with SessionStore() as store:
        s = store.create_session("Test Call", retain_audio=True)
        assert s.title == "Test Call"
        assert s.state == STATE_NEW
        assert s.retain_audio is True

        fetched = store.get_session(s.id)
        assert fetched is not None
        assert fetched.title == "Test Call"
        assert fetched.retain_audio is True

        all_sessions = store.list_sessions()
        assert len(all_sessions) == 1

        store.update_session(s.id, state=STATE_RECORDING, model_used="small.en", has_audio=True)
        s2 = store.get_session(s.id)
        assert s2.state == STATE_RECORDING
        assert s2.model_used == "small.en"
        assert s2.has_audio is True

        store.delete_session(s.id)
        assert store.get_session(s.id) is None


def test_rename_session_via_update(isolated_data_dir):
    """`update_session(title=...)` is the path the rename UX uses; round-trip
    must reflect the new title on subsequent get_session calls."""
    with SessionStore() as store:
        s = store.create_session("Old Title")
        store.update_session(s.id, title="New Title")
        refreshed = store.get_session(s.id)
        assert refreshed is not None
        assert refreshed.title == "New Title"


def test_edit_timestamp_via_update(isolated_data_dir):
    """`update_session(created_at=...)` is the path the calendar-alignment
    and Edit Timestamp flows use; round-trip must reflect the new value
    and the sessions list must re-sort by it."""
    with SessionStore() as store:
        a = store.create_session("alpha")
        b = store.create_session("beta")
        # Bump alpha into the future so it sorts above beta.
        store.update_session(a.id, created_at="2099-01-01T12:00:00Z")
        refreshed = store.get_session(a.id)
        assert refreshed is not None
        assert refreshed.created_at == "2099-01-01T12:00:00Z"
        ordered = [s.id for s in store.list_sessions()]
        assert ordered[0] == a.id and ordered[1] == b.id


def test_folder_create_assign_delete_unfiles_sessions(isolated_data_dir):
    with SessionStore() as store:
        folder = store.create_folder("Customers")
        s = store.create_session("Acme call", folder_id=folder.id)
        assert store.get_session(s.id).folder_id == folder.id

        # list_sessions(folder_id=...) scopes results correctly
        assert len(store.list_sessions(folder_id=folder.id)) == 1
        assert len(store.list_sessions(folder_id=None)) == 0

        # Deleting the folder un-files (not deletes) its sessions
        store.delete_folder(folder.id)
        assert store.get_session(s.id).folder_id is None


def test_unknown_field_rejected(isolated_data_dir):
    import pytest
    with SessionStore() as store:
        s = store.create_session("x")
        with pytest.raises(ValueError):
            store.update_session(s.id, not_a_real_field=1)


def test_bulk_delete(isolated_data_dir):
    with SessionStore() as store:
        ids = [store.create_session(f"s{i}").id for i in range(5)]
        removed = store.delete_sessions(ids[:3])
        assert removed == 3
        remaining = store.list_sessions()
        assert sorted(s.id for s in remaining) == sorted(ids[3:])


def test_orphan_recovery(isolated_data_dir):
    with SessionStore() as store:
        a = store.create_session("clean")
        b = store.create_session("crashed_recording")
        c = store.create_session("crashed_processing")
        d = store.create_session("done")
        store.update_session(a.id, state=STATE_NEW)
        store.update_session(b.id, state=STATE_RECORDING)
        store.update_session(c.id, state=STATE_PROCESSING)
        store.update_session(d.id, state=STATE_COMPLETE)
        orphans = {s.id for s in store.find_orphans()}
        assert orphans == {b.id, c.id}
