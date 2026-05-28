"""Rich Contact fields + migration + fill-empty-only semantics (#51 Phase 1).

The Contact model gained title/company/department/primary_email/phone
+ a last_enriched_source tag in v0.7.2. Backwards-compat is critical
-- existing classification.db files have a contacts table without
the new columns, and the migration must add them via ALTER TABLE on
first open of the new schema. The fill-empty-only flag is the
load-bearing piece for the automatic Outlook + LLM enrichment paths;
they must never overwrite values the user (or a prior enrichment)
already set.
"""
from __future__ import annotations

import sqlite3

import pytest

from meeting_notetaker.models.classification import (
    ClassificationStore,
    Contact,
    ENRICH_SOURCE_LLM,
    ENRICH_SOURCE_MANUAL,
    ENRICH_SOURCE_OUTLOOK,
    SOURCE_MANUAL,
)


@pytest.fixture
def store(tmp_path):
    s = ClassificationStore(tmp_path / "classification.db")
    yield s
    s.close()


# ---- migration --------------------------------------------------------------


def test_fresh_db_has_all_rich_columns(tmp_path):
    """A new DB created at v0.7.2 has every rich column. Sanity check
    that the SCHEMA includes them so we don't rely on the migration
    helper to add them on every fresh install."""
    db = ClassificationStore(tmp_path / "fresh.db")
    cols = {
        row["name"]
        for row in db._conn.execute("PRAGMA table_info(contacts)")  # noqa: SLF001
    }
    db.close()
    expected = {
        "id", "display_name", "notes", "title", "company", "department",
        "primary_email", "phone", "last_enriched_source", "created_at",
        # Per-field source columns (2026-05-28).
        "title_source", "company_source", "department_source",
        "primary_email_source", "phone_source", "notes_source",
    }
    assert expected.issubset(cols), f"missing columns: {expected - cols}"


def test_legacy_db_without_rich_columns_gets_migrated(tmp_path):
    """A pre-v0.7.2 classification.db has a contacts table without
    the new columns. ClassificationStore.__init__ must apply additive
    ALTER TABLE migrations transparently on first open."""
    db_file = tmp_path / "legacy.db"
    # Create a legacy-shape DB manually (no title/company/etc).
    conn = sqlite3.connect(str(db_file))
    conn.executescript("""
        CREATE TABLE contacts (
            id INTEGER PRIMARY KEY,
            display_name TEXT NOT NULL,
            notes TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );
        INSERT INTO contacts (id, display_name, notes, created_at)
        VALUES (1, 'Legacy Contact', '', '2025-01-01T00:00:00Z');
    """)
    conn.commit()
    conn.close()
    # Open via ClassificationStore -- should migrate cleanly.
    store = ClassificationStore(db_file)
    try:
        cols = {
            row["name"]
            for row in store._conn.execute("PRAGMA table_info(contacts)")  # noqa: SLF001
        }
        for col in (
            "title", "company", "department",
            "primary_email", "phone", "last_enriched_source",
            "title_source", "company_source", "department_source",
            "primary_email_source", "phone_source", "notes_source",
        ):
            assert col in cols, f"migration missed {col}"
        # Existing row still readable; new fields all NULL.
        contact = store.get_contact(1)
        assert contact is not None
        assert contact.display_name == "Legacy Contact"
        assert contact.title is None
        assert contact.company is None
        assert contact.last_enriched_source is None
    finally:
        store.close()


def test_migration_is_idempotent(tmp_path):
    """Opening a freshly-migrated DB a second time must not re-ADD
    columns (which would fail with 'duplicate column name')."""
    db_file = tmp_path / "twice.db"
    first = ClassificationStore(db_file)
    first.close()
    # Second open exercises the same migration path; should not error.
    second = ClassificationStore(db_file)
    second.close()


# ---- update_contact_fields -------------------------------------------------


def _make_contact(store, name="Test Contact") -> Contact:
    return store.create_contact(name)


def test_update_contact_fields_writes_all_columns(store):
    c = _make_contact(store)
    store.update_contact_fields(
        c.id,
        title="VP of Engineering",
        company="Acme Corp",
        department="Engineering",
        primary_email="vp@acme.com",
        phone="+1-555-0100",
    )
    updated = store.get_contact(c.id)
    assert updated is not None
    assert updated.title == "VP of Engineering"
    assert updated.company == "Acme Corp"
    assert updated.department == "Engineering"
    assert updated.primary_email == "vp@acme.com"
    assert updated.phone == "+1-555-0100"
    # Source defaults to manual when not specified.
    assert updated.last_enriched_source == ENRICH_SOURCE_MANUAL


def test_update_contact_fields_records_source(store):
    """The source kwarg drives last_enriched_source; Outlook + LLM
    paths use this to mark fields they touched."""
    c = _make_contact(store)
    store.update_contact_fields(
        c.id, title="CEO", source=ENRICH_SOURCE_OUTLOOK,
    )
    assert store.get_contact(c.id).last_enriched_source == ENRICH_SOURCE_OUTLOOK
    store.update_contact_fields(
        c.id, company="NewCo", source=ENRICH_SOURCE_LLM,
    )
    # Most recent enrichment wins -- the column tracks the LAST source.
    assert store.get_contact(c.id).last_enriched_source == ENRICH_SOURCE_LLM


def test_update_contact_fields_rejects_unknown_field(store):
    c = _make_contact(store)
    with pytest.raises(ValueError, match="unknown contact field"):
        store.update_contact_fields(c.id, made_up_field="x")


def test_update_contact_fields_with_no_args_is_noop(store):
    c = _make_contact(store)
    store.update_contact_fields(c.id)
    # No exception, no field changes.
    assert store.get_contact(c.id).title is None


# ---- fill_empty_only -------------------------------------------------------


def test_fill_empty_only_populates_null_fields(store):
    """Outlook + LLM enrichment paths use fill_empty_only=True to
    populate NULL fields without touching user-set values."""
    c = _make_contact(store)
    # No fields set yet -- fill_empty_only fills them.
    store.update_contact_fields(
        c.id,
        title="CEO",
        company="Bobco",
        source=ENRICH_SOURCE_OUTLOOK,
        fill_empty_only=True,
    )
    after = store.get_contact(c.id)
    assert after.title == "CEO"
    assert after.company == "Bobco"
    assert after.last_enriched_source == ENRICH_SOURCE_OUTLOOK


def test_fill_empty_only_skips_already_populated_fields(store):
    """The load-bearing invariant: a user-set value is never
    overwritten by an automatic enrichment pass."""
    c = _make_contact(store)
    store.update_contact_fields(c.id, title="VP", company="Acme")
    # Now an Outlook enrichment tries to set different values.
    store.update_contact_fields(
        c.id,
        title="CEO (from Outlook)",
        company="Acme Corp (from Outlook)",
        source=ENRICH_SOURCE_OUTLOOK,
        fill_empty_only=True,
    )
    after = store.get_contact(c.id)
    # Original values preserved.
    assert after.title == "VP"
    assert after.company == "Acme"
    # last_enriched_source should NOT have changed -- the enrichment
    # found nothing to fill, so the noop must not look like a touch.
    assert after.last_enriched_source == ENRICH_SOURCE_MANUAL


def test_fill_empty_only_fills_one_field_skips_another(store):
    """Partial fill: title is set, company is NULL. Enrichment fills
    company, leaves title alone."""
    c = _make_contact(store)
    store.update_contact_fields(c.id, title="VP")
    # company is NULL after the above; phone is NULL too.
    store.update_contact_fields(
        c.id,
        title="CEO",
        company="Acme",
        phone="+1-555-0100",
        source=ENRICH_SOURCE_LLM,
        fill_empty_only=True,
    )
    after = store.get_contact(c.id)
    assert after.title == "VP"  # preserved
    assert after.company == "Acme"  # filled
    assert after.phone == "+1-555-0100"  # filled
    assert after.last_enriched_source == ENRICH_SOURCE_LLM


def test_fill_empty_only_treats_empty_string_as_empty(store):
    """A user who clears a field via the form leaves an empty string,
    not NULL. The enrichment paths should treat empty strings as
    'empty' too -- a cleared field is still 'empty' for filling
    purposes."""
    c = _make_contact(store)
    store.update_contact_fields(c.id, title="")  # explicit clear
    store.update_contact_fields(
        c.id,
        title="VP",
        source=ENRICH_SOURCE_OUTLOOK,
        fill_empty_only=True,
    )
    assert store.get_contact(c.id).title == "VP"


def test_fill_empty_only_ignores_empty_incoming_values(store):
    """If the enrichment source has no value for a field (None or
    empty string), the existing value is preserved -- we don't
    overwrite 'VP' with ''."""
    c = _make_contact(store)
    store.update_contact_fields(c.id, title="VP")
    store.update_contact_fields(
        c.id,
        title="",  # outlook has no title for this person
        source=ENRICH_SOURCE_OUTLOOK,
        fill_empty_only=True,
    )
    assert store.get_contact(c.id).title == "VP"


# ---- per-field source tracking (2026-05-28) ---------------------------------


def test_per_field_source_records_outlook_write(store):
    """An Outlook enrichment that fills title sets title_source = outlook."""
    c = _make_contact(store)
    store.update_contact_fields(
        c.id, title="VP", source=ENRICH_SOURCE_OUTLOOK,
    )
    refreshed = store.get_contact(c.id)
    assert refreshed.title == "VP"
    assert refreshed.title_source == ENRICH_SOURCE_OUTLOOK
    # Untouched fields' sources stay None.
    assert refreshed.company_source is None
    assert refreshed.phone_source is None


def test_per_field_source_independent_across_fields(store):
    """Different fields can carry different sources on the same row."""
    c = _make_contact(store)
    store.update_contact_fields(c.id, title="VP", source=ENRICH_SOURCE_OUTLOOK)
    store.update_contact_fields(
        c.id, department="Engineering", source=ENRICH_SOURCE_LLM,
    )
    store.update_contact_fields(c.id, phone="555-0100")  # default = manual
    refreshed = store.get_contact(c.id)
    assert refreshed.title_source == ENRICH_SOURCE_OUTLOOK
    assert refreshed.department_source == ENRICH_SOURCE_LLM
    assert refreshed.phone_source == ENRICH_SOURCE_MANUAL


def test_per_field_source_overwritten_by_subsequent_write(store):
    """Manual edit after an Outlook fill flips title_source to manual."""
    c = _make_contact(store)
    store.update_contact_fields(
        c.id, title="VP", source=ENRICH_SOURCE_OUTLOOK,
    )
    assert store.get_contact(c.id).title_source == ENRICH_SOURCE_OUTLOOK
    store.update_contact_fields(c.id, title="VP of Eng")  # manual default
    refreshed = store.get_contact(c.id)
    assert refreshed.title == "VP of Eng"
    assert refreshed.title_source == ENRICH_SOURCE_MANUAL


def test_per_field_source_cleared_when_value_emptied(store):
    """Clearing a field clears its source too -- no source for no value."""
    c = _make_contact(store)
    store.update_contact_fields(
        c.id, title="VP", source=ENRICH_SOURCE_OUTLOOK,
    )
    assert store.get_contact(c.id).title_source == ENRICH_SOURCE_OUTLOOK
    store.update_contact_fields(c.id, title="")  # user cleared it
    refreshed = store.get_contact(c.id)
    assert (refreshed.title or "") == ""
    assert refreshed.title_source is None


def test_per_field_source_not_set_when_fill_empty_only_skips(store):
    """fill_empty_only refuses to overwrite, so the source on a
    pre-filled field stays at the original value, not the would-be
    new source."""
    c = _make_contact(store)
    store.update_contact_fields(
        c.id, title="VP", source=ENRICH_SOURCE_MANUAL,
    )
    # Outlook enrichment tries to set title but fill_empty_only blocks it.
    store.update_contact_fields(
        c.id, title="Director",
        source=ENRICH_SOURCE_OUTLOOK, fill_empty_only=True,
    )
    refreshed = store.get_contact(c.id)
    assert refreshed.title == "VP"
    assert refreshed.title_source == ENRICH_SOURCE_MANUAL


def test_legacy_row_per_field_source_columns_are_null(tmp_path):
    """A row that existed before the per-field migration has NULL on
    every *_source column -- we can't reconstruct historical provenance."""
    db_file = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_file))
    conn.executescript("""
        CREATE TABLE contacts (
            id INTEGER PRIMARY KEY,
            display_name TEXT NOT NULL,
            notes TEXT DEFAULT '',
            title TEXT,
            created_at TEXT NOT NULL
        );
        INSERT INTO contacts (id, display_name, notes, title, created_at)
        VALUES (1, 'Old Bob', '', 'VP', '2025-01-01T00:00:00Z');
    """)
    conn.commit()
    conn.close()
    store = ClassificationStore(db_file)
    try:
        c = store.get_contact(1)
        assert c.title == "VP"
        # Pre-existing values lose their provenance through migration; all None.
        assert c.title_source is None
        assert c.company_source is None
        assert c.notes_source is None
    finally:
        store.close()
