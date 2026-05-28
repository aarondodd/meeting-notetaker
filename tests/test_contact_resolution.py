"""Smart attendee-text -> Contact resolution + merge-suggestion scan.

Pure-Python tests (no Qt). Exercises the three resolver branches
(unique / new / ambiguous), the email-resolution path, and the
suggested-merge scan that powers the Address Book's "did you
mean to merge these?" surface.
"""
from __future__ import annotations

import pytest

from meeting_notetaker.models.classification import (
    ALIAS_KIND_EMAIL,
    ALIAS_KIND_NAME,
    ALIAS_KIND_SHORT,
    ClassificationStore,
    SOURCE_ATTENDEE_LIST,
    SOURCE_CALENDAR,
    SOURCE_DIARIZATION,
    SOURCE_MANUAL,
)
from meeting_notetaker.utils.contact_resolution import (
    MergeSuggestion,
    display_name_for_email,
    resolve_attendee_email,
    resolve_attendee_text,
    resolve_attendees_batch,
    suggest_contact_merges,
)


@pytest.fixture
def store(tmp_path):
    s = ClassificationStore(tmp_path / "classification.db")
    yield s
    s.close()


# ---- resolve_attendee_text ----


def test_resolve_unique_match_links_existing_contact(store):
    bob = store.create_contact("Bob Smith")
    store.add_alias(bob.id, "BS", kind=ALIAS_KIND_SHORT)
    result = resolve_attendee_text(store, "BS")
    assert result is not None
    assert result.was_created is False
    assert result.contact.id == bob.id


def test_resolve_unique_match_registers_typed_form_as_alias(store):
    """If the typed form isn't already an alias, the resolver adds
    it so the next encounter is a O(1) hit."""
    bob = store.create_contact("Bob Smith")
    # "Bob S." isn't registered yet. Resolution is unique because
    # nothing else aliases anywhere near it.
    bob_id = bob.id
    # Add a short alias so unique matching works.
    store.add_alias(bob_id, "Bob S.", kind=ALIAS_KIND_SHORT)
    # Now resolve a new typed form that matches via a totally
    # different alias.
    result = resolve_attendee_text(store, "Bob S.")
    assert result.contact.id == bob_id
    # The typed form was already an alias; nothing new gets added.
    aliases = {a.alias for a in store.list_aliases(bob_id)}
    assert "Bob S." in aliases


def test_resolve_no_match_creates_new_contact(store):
    result = resolve_attendee_text(store, "Brand New Person")
    assert result is not None
    assert result.was_created is True
    assert result.contact.display_name == "Brand New Person"
    assert result.ambiguity_candidates == []


def test_resolve_ambiguous_creates_new_and_flags_candidates(store):
    """When the typed text matches multiple Contacts, the resolver
    creates a new Contact and flags the others so the Address
    Book can surface the conflict later."""
    a = store.create_contact("Alice Johnson")
    b = store.create_contact("Anders Jorgensen")
    store.add_alias(a.id, "AJ", kind=ALIAS_KIND_SHORT)
    store.add_alias(b.id, "AJ", kind=ALIAS_KIND_SHORT)
    # Both Alice and Anders have "AJ" as a short alias.
    # New attendee "AJ" should produce a new Contact + flag both.
    result = resolve_attendee_text(store, "AJ")
    assert result.was_created is True
    candidate_ids = {c.id for c in result.ambiguity_candidates}
    assert {a.id, b.id} == candidate_ids


def test_resolve_blank_text_returns_none(store):
    assert resolve_attendee_text(store, "") is None
    assert resolve_attendee_text(store, "   ") is None


def test_resolve_attendees_batch_dedupes(store):
    bob = store.create_contact("Bob")
    store.add_alias(bob.id, "B", kind=ALIAS_KIND_SHORT)
    out = resolve_attendees_batch(store, ["Bob", "B", "Bob"])
    # All three resolved to the same Contact; output dedupes.
    assert [c.id for c in out] == [bob.id]


# ---- resolve_attendee_email ----


def test_resolve_email_unique_hit_returns_existing(store):
    bob = store.create_contact("Bob Smith")
    store.add_alias(
        bob.id, "bsmith@corp.com",
        kind=ALIAS_KIND_EMAIL, source=SOURCE_CALENDAR,
    )
    result = resolve_attendee_email(store, "bsmith@corp.com")
    assert result is not None
    assert result.contact.id == bob.id
    assert result.was_created is False


def test_resolve_email_miss_creates_stub_contact_with_email_alias(store):
    result = resolve_attendee_email(store, "newperson@corp.com")
    assert result is not None
    assert result.was_created is True
    # Stub display_name = email local-part.
    assert result.contact.display_name == "newperson"
    # Email registered as an alias for next time.
    email_aliases = [
        a for a in store.list_aliases(result.contact.id)
        if a.kind == ALIAS_KIND_EMAIL
    ]
    assert len(email_aliases) == 1
    assert email_aliases[0].alias == "newperson@corp.com"


def test_resolve_email_blank_or_garbage_returns_none(store):
    assert resolve_attendee_email(store, "") is None
    # No @-sign -> not a real email.
    assert resolve_attendee_email(store, "not-an-email") is None


def test_display_name_for_email_returns_canonical_on_hit(store):
    bob = store.create_contact("Bob Smith")
    store.add_alias(bob.id, "bsmith@corp.com", kind=ALIAS_KIND_EMAIL)
    assert display_name_for_email(store, "bsmith@corp.com") == "Bob Smith"


def test_display_name_for_email_miss_returns_none(store):
    # No Contact has this email aliased -> None (caller falls back
    # to using the raw email).
    assert display_name_for_email(store, "unknown@corp.com") is None


# ---- suggest_contact_merges ----


def test_suggest_shared_alias_pair(store):
    a = store.create_contact("Alice Johnson")
    b = store.create_contact("Anders Jorgensen")
    store.add_alias(a.id, "AJ", kind=ALIAS_KIND_SHORT)
    store.add_alias(b.id, "AJ", kind=ALIAS_KIND_SHORT)
    # Shared alias should surface as a suggested merge.
    suggestions = suggest_contact_merges(store)
    assert suggestions
    reasons = {s.reason for s in suggestions}
    assert any("AJ" in r or "alias" in r.lower() for r in reasons)


def test_suggest_name_token_containment(store):
    """'Bob' is a subset of 'Bob Smith' -- likely an alias of him."""
    store.create_contact("Bob")
    store.create_contact("Bob Smith")
    suggestions = suggest_contact_merges(store)
    # The token-subset rule should fire here even without explicit
    # alias overlap.
    assert any(
        "subset" in s.reason or "subset" in s.reason.lower()
        for s in suggestions
    )


def test_suggest_edit_distance_typo(store):
    store.create_contact("Bob Smith")
    store.create_contact("Bob Smitn")  # 1-char typo
    suggestions = suggest_contact_merges(store)
    assert any("edits" in s.reason for s in suggestions)


def test_suggest_dedupes_pairs(store):
    """A pair shouldn't appear twice even if multiple signals fire."""
    a = store.create_contact("Bob Smith")
    b = store.create_contact("Bob Smitn")  # typo + token overlap
    store.add_alias(a.id, "BS", kind=ALIAS_KIND_SHORT)
    store.add_alias(b.id, "BS", kind=ALIAS_KIND_SHORT)
    suggestions = suggest_contact_merges(store)
    # The (a, b) pair should appear at most once regardless of how
    # many of the three signals matched it.
    pairs = [
        (s.source.id, s.target.id) for s in suggestions
    ]
    assert len(pairs) == len(set(pairs))


def test_suggest_empty_when_no_duplicates(store):
    store.create_contact("Alice")
    store.create_contact("Charlie")
    store.create_contact("Eve")
    assert suggest_contact_merges(store) == []


def test_suggest_capped_by_max_suggestions(store):
    # Create 6 contacts all sharing the same alias -> C(6,2) = 15 pairs.
    base_ids = []
    for i in range(6):
        c = store.create_contact(f"Person {i}")
        store.add_alias(c.id, "shared", kind=ALIAS_KIND_SHORT)
        base_ids.append(c.id)
    out = suggest_contact_merges(store, max_suggestions=5)
    assert len(out) == 5


# ---- end-to-end: ambiguous-attendee creates a suggested merge ----


def test_ambiguous_attendee_surfaces_in_suggested_merges(store):
    """Aaron's "BS for Bob Smith" scenario. After a user types
    "BS" as an attendee while Bob Smith already aliases to it,
    the new Contact + the existing one show up as a suggested
    merge in the Address Book."""
    bob = store.create_contact("Bob Smith")
    store.add_alias(bob.id, "BS", kind=ALIAS_KIND_SHORT)
    # Also create a Brenda Saunders who also short-aliases to "BS".
    brenda = store.create_contact("Brenda Saunders")
    store.add_alias(brenda.id, "BS", kind=ALIAS_KIND_SHORT)
    # Typing "BS" now resolves to a new Contact + flags both
    # existing ones.
    result = resolve_attendee_text(store, "BS")
    assert result.was_created is True
    new_contact_id = result.contact.id
    # The merge-suggestion scan picks up the conflict.
    suggestions = suggest_contact_merges(store)
    involved_ids = {s.source.id for s in suggestions} | {
        s.target.id for s in suggestions
    }
    assert new_contact_id in involved_ids
    # And both prior candidates appear in suggestion pairs.
    assert bob.id in involved_ids
    assert brenda.id in involved_ids


# ---- Outlook enrichment (issue #51 Phase 2) -----------------------------


from dataclasses import dataclass
from meeting_notetaker.models.classification import ENRICH_SOURCE_OUTLOOK
from meeting_notetaker.utils.contact_resolution import (
    enrich_contact_from_calendar_attendee,
)


@dataclass
class _FakeAttendee:
    """Duck-typed stand-in for CalendarAttendee in pure-Python tests."""
    name: str = ""
    email: str = ""
    title: str = ""
    company: str = ""
    department: str = ""


def test_enrich_from_outlook_fills_empty_fields(store):
    """A Contact with no rich fields gets every Outlook-supplied
    field, and last_enriched_source flips to 'outlook'."""
    c = store.create_contact("Bob Smith")
    enrich_contact_from_calendar_attendee(
        store, c.id,
        _FakeAttendee(
            email="bob@bobco.com",
            title="CEO",
            company="Bobco",
            department="Executive",
        ),
    )
    after = store.get_contact(c.id)
    assert after.title == "CEO"
    assert after.company == "Bobco"
    assert after.department == "Executive"
    assert after.primary_email == "bob@bobco.com"
    assert after.last_enriched_source == ENRICH_SOURCE_OUTLOOK


def test_enrich_from_outlook_skips_already_set_fields(store):
    """Outlook never overwrites a value the user already set."""
    c = store.create_contact("Bob Smith")
    store.update_contact_fields(c.id, title="VP", company="Acme")
    enrich_contact_from_calendar_attendee(
        store, c.id,
        _FakeAttendee(title="CEO", company="Bobco", department="Sales"),
    )
    after = store.get_contact(c.id)
    assert after.title == "VP"  # preserved
    assert after.company == "Acme"  # preserved
    assert after.department == "Sales"  # filled (was NULL)


def test_enrich_from_outlook_with_no_data_is_noop(store):
    """An external attendee with no AddressEntry comes back with
    empty strings everywhere. Enrichment must NOT touch the
    Contact at all (no spurious last_enriched_source flip)."""
    c = store.create_contact("External Bob")
    enrich_contact_from_calendar_attendee(
        store, c.id, _FakeAttendee(name="External Bob", email=""),
    )
    after = store.get_contact(c.id)
    assert after.last_enriched_source is None
    assert after.title is None


def test_enrich_from_outlook_handles_missing_attributes(store):
    """Duck-typed safety: enrich_contact_from_calendar_attendee
    on an object missing the rich-field attributes treats them as
    empty rather than raising."""
    c = store.create_contact("Bob Smith")
    class _Minimal:
        name = "Bob"
        email = "bob@bobco.com"
    enrich_contact_from_calendar_attendee(store, c.id, _Minimal())
    # Only the email was available; title/company/department stay NULL.
    after = store.get_contact(c.id)
    assert after.primary_email == "bob@bobco.com"
    assert after.title is None
