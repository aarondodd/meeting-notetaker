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
