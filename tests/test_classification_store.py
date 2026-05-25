"""ClassificationStore -- CRUD over series, people, topics + their
many-to-many session links.

The store is pure SQLite; tests use an in-tmp file and exercise the
patterns MainApp drives: get-or-create with case-insensitive dedup,
rename + merge for both series and topics, attendee-list sync that
preserves manual links, suggestion-replace that preserves accepted
topics, and the cross-DB cleanup hook for session deletion.
"""
from __future__ import annotations

import pytest

from meeting_notetaker.models.classification import (
    ClassificationStore,
    SOURCE_ATTENDEE_LIST,
    SOURCE_AUTO,
    SOURCE_DIARIZATION,
    SOURCE_MANUAL,
    _normalize_title,
)


@pytest.fixture
def store(tmp_path):
    s = ClassificationStore(tmp_path / "classification.db")
    yield s
    s.close()


# ----------------------------------------------------------------------
# Series


def test_get_or_create_series_returns_same_id_for_case_insensitive_match(store):
    a = store.get_or_create_series("Platform Team Sync")
    b = store.get_or_create_series("platform team sync")
    assert a.id == b.id
    # Canonical form is the first inserted -- not silently overwritten
    # by the lower-case lookup.
    assert b.name == "Platform Team Sync"


def test_get_or_create_series_rejects_empty(store):
    with pytest.raises(ValueError):
        store.get_or_create_series("   ")


def test_assign_and_lookup_series_for_session(store):
    series = store.get_or_create_series("Sync")
    store.assign_series("s1", series.id)
    found = store.series_for_session("s1")
    assert found is not None and found.id == series.id


def test_assign_none_clears_series(store):
    series = store.get_or_create_series("Sync")
    store.assign_series("s1", series.id)
    store.assign_series("s1", None)
    assert store.series_for_session("s1") is None


def test_assign_series_overwrites_prior_assignment(store):
    s1 = store.get_or_create_series("Sync A")
    s2 = store.get_or_create_series("Sync B")
    store.assign_series("sess", s1.id)
    store.assign_series("sess", s2.id)
    assert store.series_for_session("sess").id == s2.id


def test_rename_series_propagates_to_lookups(store):
    series = store.get_or_create_series("Old Name")
    store.rename_series(series.id, "New Name")
    refreshed = store.find_series_by_name("New Name")
    assert refreshed is not None and refreshed.id == series.id
    assert store.find_series_by_name("Old Name") is None


def test_merge_series_moves_sessions_then_drops_source(store):
    src = store.get_or_create_series("Old")
    dst = store.get_or_create_series("New")
    store.assign_series("s1", src.id)
    store.assign_series("s2", src.id)
    store.assign_series("s3", dst.id)
    store.merge_series(src.id, dst.id)
    # Source series is gone; dst now owns all three sessions.
    assert store.find_series_by_name("Old") is None
    assert set(store.session_ids_for_series(dst.id)) == {"s1", "s2", "s3"}


def test_merge_series_noop_when_source_equals_target(store):
    src = store.get_or_create_series("Same")
    store.assign_series("s1", src.id)
    store.merge_series(src.id, src.id)
    assert store.find_series_by_name("Same") is not None
    assert store.session_ids_for_series(src.id) == ["s1"]


def test_delete_series_cascades_to_session_series(store):
    series = store.get_or_create_series("Drop me")
    store.assign_series("s1", series.id)
    store.delete_series(series.id)
    assert store.series_for_session("s1") is None


# Series auto-detection via fuzzy title


def test_find_series_for_title_matches_recurring_pattern(store):
    """Sessions named with a date suffix match an existing series of
    the un-dated form. Pattern issue #24 calls out explicitly."""
    series = store.get_or_create_series("Platform Team Sync")
    found = store.find_series_for_title("Platform Team Sync 2026-05-24")
    assert found is not None and found.id == series.id


def test_find_series_for_title_matches_weekday_suffix(store):
    series = store.get_or_create_series("Standup")
    assert store.find_series_for_title("Standup -- Tuesday").id == series.id


def test_find_series_for_title_returns_none_for_one_off(store):
    store.get_or_create_series("Platform Team Sync")
    assert store.find_series_for_title("Quarterly Roadmap Review") is None


def test_normalize_title_strips_dates_and_weekdays():
    assert _normalize_title("Platform Team Sync 2026-05-24") == "platform team sync"
    assert _normalize_title("Standup -- Tuesday") == "standup"
    assert _normalize_title("Weekly Sync") == "sync"  # weekly stripped


# ----------------------------------------------------------------------
# People


def test_get_or_create_person_case_insensitive(store):
    a = store.get_or_create_person("Alice Smith")
    b = store.get_or_create_person("alice smith")
    assert a.id == b.id
    assert b.display_name == "Alice Smith"


def test_add_session_person_then_lookup(store):
    alice = store.get_or_create_person("Alice")
    store.add_session_person("s1", alice.id, source=SOURCE_MANUAL)
    people = store.people_for_session("s1")
    assert len(people) == 1 and people[0].person.display_name == "Alice"
    assert people[0].source == SOURCE_MANUAL


def test_add_session_person_idempotent(store):
    alice = store.get_or_create_person("Alice")
    store.add_session_person("s1", alice.id)
    store.add_session_person("s1", alice.id)
    assert len(store.people_for_session("s1")) == 1


def test_remove_session_person_drops_link(store):
    alice = store.get_or_create_person("Alice")
    store.add_session_person("s1", alice.id)
    store.remove_session_person("s1", alice.id)
    assert store.people_for_session("s1") == []


def test_rename_person_propagates_via_id(store):
    alice = store.get_or_create_person("Alice")
    store.add_session_person("s1", alice.id)
    store.rename_person(alice.id, "Alice Smith")
    people = store.people_for_session("s1")
    assert people[0].person.display_name == "Alice Smith"


def test_sync_session_people_preserves_other_source_links(store):
    """Diarization-derived and manually-added people must survive
    a # Attendees list sync (which only owns the attendee_list
    source rows)."""
    a = store.get_or_create_person("Alice")
    b = store.get_or_create_person("Bob")
    c = store.get_or_create_person("Carol")
    store.add_session_person("s1", a.id, source=SOURCE_ATTENDEE_LIST)
    store.add_session_person("s1", b.id, source=SOURCE_DIARIZATION)
    store.add_session_person("s1", c.id, source=SOURCE_MANUAL)
    # User edits # Attendees to ["Dave"] -- Alice goes away,
    # Bob+Carol stay (different source).
    store.sync_session_people("s1", ["Dave"])
    names = {p.person.display_name for p in store.people_for_session("s1")}
    assert names == {"Bob", "Carol", "Dave"}


def test_sync_session_people_dedups_within_attendees(store):
    store.sync_session_people("s1", ["Alice", "alice", "ALICE"])
    assert len(store.people_for_session("s1")) == 1


def test_session_ids_for_person_lists_sessions(store):
    alice = store.get_or_create_person("Alice")
    store.add_session_person("a", alice.id)
    store.add_session_person("b", alice.id)
    assert sorted(store.session_ids_for_person(alice.id)) == ["a", "b"]


# ----------------------------------------------------------------------
# Topics


def test_get_or_create_topic_case_insensitive(store):
    a = store.get_or_create_topic("MDM")
    b = store.get_or_create_topic("mdm")
    assert a.id == b.id
    assert b.name == "MDM"   # canonical preserved


def test_session_ids_for_person_lists_sessions_dedup(store):
    """Adding the same person to multiple sessions, in_use filters
    rely on this method via the navigator's session-list filter."""
    bob = store.get_or_create_person("Bob")
    store.add_session_person("a", bob.id)
    store.add_session_person("b", bob.id)
    store.add_session_person("a", bob.id)  # idempotent dup
    assert sorted(store.session_ids_for_person(bob.id)) == ["a", "b"]


def test_add_session_topic_then_lookup(store):
    mdm = store.get_or_create_topic("MDM")
    store.add_session_topic("s1", mdm.id, source=SOURCE_MANUAL, accepted=True)
    topics = store.topics_for_session("s1")
    assert len(topics) == 1
    assert topics[0].topic.name == "MDM"
    assert topics[0].accepted is True


def test_add_session_topic_accepted_max_keeps_user_acceptance(store):
    """Re-adding a topic with accepted=False must NOT downgrade a
    previously-accepted association. MAX(accepted, ...) handles this."""
    mdm = store.get_or_create_topic("MDM")
    store.add_session_topic("s1", mdm.id, accepted=True)
    store.add_session_topic("s1", mdm.id, source=SOURCE_AUTO, accepted=False)
    topic = store.topics_for_session("s1")[0]
    assert topic.accepted is True


def test_set_topic_accepted_explicit_downgrade(store):
    """The explicit setter allows accepted=False (user clicks
    'reject after accepting'); add_session_topic uses MAX to
    avoid silent downgrades."""
    mdm = store.get_or_create_topic("MDM")
    store.add_session_topic("s1", mdm.id, accepted=True)
    store.set_topic_accepted("s1", mdm.id, False)
    topic = store.topics_for_session("s1")[0]
    assert topic.accepted is False


def test_topics_for_session_accepted_only_filter(store):
    a = store.get_or_create_topic("Accepted")
    b = store.get_or_create_topic("Suggested")
    store.add_session_topic("s1", a.id, accepted=True)
    store.add_session_topic("s1", b.id, source=SOURCE_AUTO, accepted=False)
    all_topics = store.topics_for_session("s1", accepted_only=False)
    accepted_only = store.topics_for_session("s1", accepted_only=True)
    assert {t.topic.name for t in all_topics} == {"Accepted", "Suggested"}
    assert {t.topic.name for t in accepted_only} == {"Accepted"}


def test_replace_session_topic_suggestions_preserves_accepted(store):
    """A fresh extraction shouldn't blow away topics the user already
    accepted from a prior pass."""
    kept = store.get_or_create_topic("Kept")
    discarded = store.get_or_create_topic("Discarded")
    store.add_session_topic("s1", kept.id, accepted=True)
    store.add_session_topic("s1", discarded.id, source=SOURCE_AUTO, accepted=False)
    store.replace_session_topic_suggestions("s1", ["FreshSuggestion"])
    names = {t.topic.name for t in store.topics_for_session("s1")}
    assert names == {"Kept", "FreshSuggestion"}


def test_session_ids_for_topic_only_returns_accepted(store):
    """The navigator's "By Topic" view should only surface accepted
    associations, not auto-suggestions still in the suggestion
    bucket."""
    mdm = store.get_or_create_topic("MDM")
    store.add_session_topic("yes", mdm.id, accepted=True)
    store.add_session_topic("no", mdm.id, source=SOURCE_AUTO, accepted=False)
    assert store.session_ids_for_topic(mdm.id) == ["yes"]


def test_rename_topic_preserves_associations(store):
    mdm = store.get_or_create_topic("MDM")
    store.add_session_topic("s1", mdm.id, accepted=True)
    store.rename_topic(mdm.id, "Master Data Management")
    topics = store.topics_for_session("s1")
    assert topics[0].topic.name == "Master Data Management"


def test_merge_topics_unions_session_links(store):
    src = store.get_or_create_topic("MDM")
    dst = store.get_or_create_topic("MasterData")
    store.add_session_topic("a", src.id, accepted=True)
    store.add_session_topic("b", dst.id, accepted=True)
    store.add_session_topic("c", src.id, accepted=True)
    store.add_session_topic("c", dst.id, accepted=True)  # session c has both
    store.merge_topics(src.id, dst.id)
    assert sorted(store.session_ids_for_topic(dst.id)) == ["a", "b", "c"]
    # Source topic gone.
    assert all(t.name != "MDM" for t in store.list_topics())


def test_remove_session_drops_all_associations(store):
    """Session deletion cleanup -- the FK doesn't cascade across
    databases, so the explicit hook has to do it."""
    series = store.get_or_create_series("S")
    alice = store.get_or_create_person("Alice")
    topic = store.get_or_create_topic("MDM")
    store.assign_series("s1", series.id)
    store.add_session_person("s1", alice.id)
    store.add_session_topic("s1", topic.id, accepted=True)
    store.remove_session("s1")
    assert store.series_for_session("s1") is None
    assert store.people_for_session("s1") == []
    assert store.topics_for_session("s1") == []


# ----------------------------------------------------------------------
# In-use list variants (v0.7.0 follow-up). The navigator only offers
# values that have at least one session association -- empty options
# would just waste clicks.


def test_list_series_in_use_excludes_orphans(store):
    used = store.get_or_create_series("Used")
    store.get_or_create_series("Orphan")  # never assigned
    store.assign_series("s1", used.id)
    names = [s.name for s in store.list_series_in_use()]
    assert "Used" in names
    assert "Orphan" not in names
    # Full list still carries both -- chips bar uses that for
    # re-link affordances.
    assert {s.name for s in store.list_series()} == {"Used", "Orphan"}


def test_list_series_in_use_recovers_after_unassign(store):
    series = store.get_or_create_series("Sometimes")
    store.assign_series("s1", series.id)
    assert any(s.name == "Sometimes" for s in store.list_series_in_use())
    store.assign_series("s1", None)
    assert not any(s.name == "Sometimes" for s in store.list_series_in_use())


def test_list_people_in_use_excludes_unlinked_people(store):
    alice = store.get_or_create_person("Alice")
    store.get_or_create_person("Bob")  # no session_people row
    store.add_session_person("s1", alice.id)
    names = [p.display_name for p in store.list_people_in_use()]
    assert "Alice" in names
    assert "Bob" not in names


def test_list_topics_in_use_excludes_orphans_and_suggestion_only(store):
    """Three states matter:
       - Topic with accepted=1 association  -> in use
       - Topic with only accepted=0 (suggestion) associations -> NOT in use
         (session_ids_for_topic only returns accepted rows -> filter
         would always return empty)
       - Topic with no associations at all -> NOT in use
    """
    accepted = store.get_or_create_topic("Accepted")
    suggestion_only = store.get_or_create_topic("SuggestionOnly")
    orphan = store.get_or_create_topic("Orphan")
    store.add_session_topic("s1", accepted.id, accepted=True)
    store.add_session_topic("s1", suggestion_only.id, source="auto", accepted=False)
    # orphan has no add_session_topic call
    names = [t.name for t in store.list_topics_in_use()]
    assert "Accepted" in names
    assert "SuggestionOnly" not in names
    assert "Orphan" not in names


def test_list_topics_in_use_promotes_after_user_accepts(store):
    """A suggestion-only topic becomes 'in use' once the user accepts
    it (set_topic_accepted(True))."""
    topic = store.get_or_create_topic("Pending")
    store.add_session_topic("s1", topic.id, source="auto", accepted=False)
    assert "Pending" not in {t.name for t in store.list_topics_in_use()}
    store.set_topic_accepted("s1", topic.id, True)
    assert "Pending" in {t.name for t in store.list_topics_in_use()}


def test_classification_for_session_aggregates_all_three(store):
    series = store.get_or_create_series("Sync")
    alice = store.get_or_create_person("Alice")
    topic = store.get_or_create_topic("MDM")
    store.assign_series("s1", series.id)
    store.add_session_person("s1", alice.id)
    store.add_session_topic("s1", topic.id, accepted=True)
    cls = store.classification_for_session("s1")
    assert cls.series.name == "Sync"
    assert [p.person.display_name for p in cls.people] == ["Alice"]
    assert [t.topic.name for t in cls.topics] == ["MDM"]
