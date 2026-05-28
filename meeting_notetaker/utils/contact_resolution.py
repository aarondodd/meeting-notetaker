"""Smart attendee-text -> Contact resolution.

The attendee-sync hot path (`_on_live_notes_changed`) fires on
every keystroke in My Notes. We can't pop a modal there asking
"which Bob did you mean?" -- that would make typing a misery.

Approach: resolve EVERY typed form silently:

* Unique alias hit (any kind) -> link to that Contact. If the
  typed form wasn't already an alias of it, add it as a `name`-
  kind alias (so future typed variations of the same form are
  recognized without re-walking the matcher).
* No alias hits -> create a new Contact with the typed text as
  its display_name + initial `name`-kind alias.
* Multiple alias hits ("BS" matches both Bob Smith and Brenda
  Saunders) -> create a new Contact, flag the ambiguity. The
  Address Book's suggested-merges surface re-discovers the
  collision later and lets the user resolve it deliberately
  rather than as a modal interruption mid-typing.

Email resolution is the same shape, scoped to `email`-kind aliases
only. Used by calendar-invite seeding so an attendee with email
`bob@corp.com` resolves to the Bob Smith Contact when an email
alias matches.

Suggested-merge detection lives in `suggest_contact_merges` --
the Address Book reads from it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from ..models.classification import (
    ALIAS_KIND_EMAIL,
    ALIAS_KIND_NAME,
    ENRICH_SOURCE_OUTLOOK,
    SOURCE_ATTENDEE_LIST,
    SOURCE_CALENDAR,
    ClassificationStore,
    Contact,
)


@dataclass
class ResolutionResult:
    """Outcome of resolving one attendee text -> Contact."""
    contact: Contact
    was_created: bool
    # When the typed form had ambiguous matches and we chose
    # "create new" over picking one, the conflicting candidates
    # land here. The Address Book uses them to render a suggested
    # merge alongside the newly-created Contact.
    ambiguity_candidates: list[Contact] = field(default_factory=list)


def resolve_attendee_text(
    store: ClassificationStore,
    text: str,
    *,
    source: str = SOURCE_ATTENDEE_LIST,
) -> Optional[ResolutionResult]:
    """Resolve a free-form attendee text token to a Contact.

    See module docstring for the three branches. Returns None
    only when `text` is empty/blank (the caller skips it).
    """
    text = (text or "").strip()
    if not text:
        return None
    matches = store.find_contacts_by_alias(text)
    if len(matches) == 1:
        contact = matches[0]
        # If the typed form isn't already an alias on this
        # contact, register it so future occurrences match
        # cheaply. The add_alias call no-ops silently when the
        # alias already exists on this contact.
        try:
            store.add_alias(
                contact.id, text,
                kind=ALIAS_KIND_NAME, source=source,
            )
        except ValueError:
            # Cross-contact collision shouldn't happen here (we
            # already proved the alias is unique), but the API
            # raises ValueError for that case -- swallow as a
            # defensive measure.
            pass
        return ResolutionResult(contact=contact, was_created=False)
    if not matches:
        contact = store.create_contact(text, initial_alias_source=source)
        return ResolutionResult(contact=contact, was_created=True)
    # Multiple matches -- ambiguous. Create a new Contact rather
    # than silently picking a wrong one; flag the candidates for
    # the Address Book's suggested-merges surface.
    contact = store.create_contact(text, initial_alias_source=source)
    return ResolutionResult(
        contact=contact, was_created=True, ambiguity_candidates=matches,
    )


def resolve_attendee_email(
    store: ClassificationStore,
    email: str,
    *,
    source: str = SOURCE_CALENDAR,
) -> Optional[ResolutionResult]:
    """Resolve an email-only attendee (calendar import) to a Contact.

    Email lookup -> use the matched Contact. Miss -> stub Contact
    with the email's local-part as display_name + the email
    registered as an `email`-kind alias.
    """
    email = (email or "").strip()
    if not email or "@" not in email:
        return None
    matches = store.find_contacts_by_alias(email, kind=ALIAS_KIND_EMAIL)
    if len(matches) == 1:
        return ResolutionResult(contact=matches[0], was_created=False)
    if not matches:
        local = email.split("@", 1)[0] or email
        contact = store.create_contact(local, initial_alias_source=source)
        try:
            store.add_alias(
                contact.id, email,
                kind=ALIAS_KIND_EMAIL, source=source,
            )
        except ValueError:
            pass
        return ResolutionResult(contact=contact, was_created=True)
    # Multiple email matches shouldn't happen (UNIQUE (alias, kind))
    # but handle gracefully.
    return ResolutionResult(contact=matches[0], was_created=False)


def resolve_attendees_batch(
    store: ClassificationStore,
    names: list[str],
    *,
    source: str = SOURCE_ATTENDEE_LIST,
) -> list[Contact]:
    """Bulk resolve typed names. Filters duplicates implicitly via
    the per-name resolver -- callers don't need to dedupe input."""
    seen_contact_ids: set[int] = set()
    out: list[Contact] = []
    for name in names:
        result = resolve_attendee_text(store, name, source=source)
        if result is None:
            continue
        if result.contact.id in seen_contact_ids:
            continue
        seen_contact_ids.add(result.contact.id)
        out.append(result.contact)
    return out


def enrich_contact_from_calendar_attendee(
    store: ClassificationStore,
    contact_id: int,
    attendee,
) -> None:
    """Fill missing Contact rich fields from a calendar attendee.

    Issue #51 Phase 2. ``attendee`` is duck-typed: any object with
    ``title`` / ``company`` / ``department`` / ``email`` string-ish
    attributes works. Anything else is silently treated as empty.

    Uses ``update_contact_fields(fill_empty_only=True,
    source=outlook)`` so:
    * Existing user-set values are never overwritten.
    * The Contact's ``last_enriched_source`` flips to ``outlook``
      only when at least one field was actually filled.

    Idempotent + safe to call on every resolution -- a Contact with
    every field set is a no-op.
    """
    title = (getattr(attendee, "title", "") or "").strip()
    company = (getattr(attendee, "company", "") or "").strip()
    department = (getattr(attendee, "department", "") or "").strip()
    email = (getattr(attendee, "email", "") or "").strip()
    fields: dict[str, Optional[str]] = {}
    if title:
        fields["title"] = title
    if company:
        fields["company"] = company
    if department:
        fields["department"] = department
    if email:
        fields["primary_email"] = email
    if not fields:
        return
    store.update_contact_fields(
        contact_id,
        source=ENRICH_SOURCE_OUTLOOK,
        fill_empty_only=True,
        **fields,
    )


def display_name_for_email(
    store: ClassificationStore,
    email: str,
) -> Optional[str]:
    """Calendar-seed helper. Returns the Contact's display_name if
    the email resolves uniquely; otherwise None so the caller
    falls back to using the raw email."""
    email = (email or "").strip()
    if not email:
        return None
    matches = store.find_contacts_by_alias(email, kind=ALIAS_KIND_EMAIL)
    if len(matches) == 1:
        return matches[0].display_name
    return None


# ----------------------------------------------------------------------
# Suggested-merge detection
# ----------------------------------------------------------------------


@dataclass
class MergeSuggestion:
    """One Address Book "did you mean to merge these?" suggestion."""
    source: Contact
    target: Contact
    reason: str   # human-readable: "shared alias 'BS'" / "name prefix" / "edit distance"


_WORD_TOKEN_RE = re.compile(r"\b[\w'-]+\b", re.UNICODE)


def _normalize(s: str) -> str:
    return s.strip().casefold()


def _tokens(s: str) -> set[str]:
    return {t.casefold() for t in _WORD_TOKEN_RE.findall(s or "") if len(t) >= 2}


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein distance, simple two-row implementation. Small
    enough not to need a numpy / external dep."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            curr.append(min(
                curr[-1] + 1,
                prev[j] + 1,
                prev[j - 1] + (0 if ca == cb else 1),
            ))
        prev = curr
    return prev[-1]


def suggest_contact_merges(
    store: ClassificationStore,
    *,
    max_suggestions: int = 50,
    max_edit_distance: int = 2,
) -> list[MergeSuggestion]:
    """Scan all Contacts for likely-duplicate pairs.

    Three signals (each MergeSuggestion records which one fired):

    1. Two Contacts share an exact alias (case-insensitive). Strong
       signal -- one of them got the alias added later despite the
       other already owning it.
    2. One Contact's display_name appears as a token in another's
       display_name. "Bob" vs "Bob Smith" -- the short form is
       likely an alias of the long form that wasn't registered.
    3. Edit distance <= max_edit_distance between display_names.
       Catches typos ("Bob Smitn" -> "Bob Smith").

    De-duplicated: each (a, b) pair appears at most once,
    direction-canonicalized so the lower id is `source` for stable
    sort order. Capped at `max_suggestions`.
    """
    contacts = store.list_contacts()
    if len(contacts) < 2:
        return []
    out: list[MergeSuggestion] = []
    seen_pairs: set[tuple[int, int]] = set()

    # Pre-compute aliases-by-text and tokens-by-contact to keep
    # the O(N^2) pass cheap.
    aliases_by_alias: dict[str, list[int]] = {}
    tokens_by_id: dict[int, set[str]] = {}
    for c in contacts:
        tokens_by_id[c.id] = _tokens(c.display_name)
        for a in store.list_aliases(c.id):
            aliases_by_alias.setdefault(_normalize(a.alias), []).append(c.id)

    by_id = {c.id: c for c in contacts}

    # Signal 1: shared aliases.
    for alias_text, contact_ids in aliases_by_alias.items():
        if len(contact_ids) < 2:
            continue
        for i in range(len(contact_ids)):
            for j in range(i + 1, len(contact_ids)):
                a_id, b_id = sorted((contact_ids[i], contact_ids[j]))
                if (a_id, b_id) in seen_pairs:
                    continue
                seen_pairs.add((a_id, b_id))
                out.append(MergeSuggestion(
                    source=by_id[a_id], target=by_id[b_id],
                    reason=f"both carry the alias '{alias_text}'",
                ))
                if len(out) >= max_suggestions:
                    return out

    # Signal 2: name-token containment.
    for i, a in enumerate(contacts):
        a_tokens = tokens_by_id[a.id]
        if not a_tokens:
            continue
        for b in contacts[i + 1:]:
            b_tokens = tokens_by_id[b.id]
            if not b_tokens:
                continue
            pair = tuple(sorted((a.id, b.id)))
            if pair in seen_pairs:
                continue
            # Token-subset OR alias-of-other check.
            if a_tokens <= b_tokens or b_tokens <= a_tokens:
                seen_pairs.add(pair)
                out.append(MergeSuggestion(
                    source=by_id[pair[0]], target=by_id[pair[1]],
                    reason="one display name's tokens are a subset of the other",
                ))
                if len(out) >= max_suggestions:
                    return out

    # Signal 3: edit distance on display_name (case-insensitive).
    # O(N^2) over typically <500 contacts -- ~250k comparisons of
    # 2-row Levenshtein, well under a second.
    for i, a in enumerate(contacts):
        a_name = _normalize(a.display_name)
        if not a_name:
            continue
        for b in contacts[i + 1:]:
            b_name = _normalize(b.display_name)
            if not b_name:
                continue
            pair = tuple(sorted((a.id, b.id)))
            if pair in seen_pairs:
                continue
            # Length gate: if they're too different in length, skip
            # the edit-distance call entirely.
            if abs(len(a_name) - len(b_name)) > max_edit_distance:
                continue
            if _edit_distance(a_name, b_name) <= max_edit_distance:
                seen_pairs.add(pair)
                out.append(MergeSuggestion(
                    source=by_id[pair[0]], target=by_id[pair[1]],
                    reason=(
                        f"display names within {max_edit_distance} edits"
                    ),
                ))
                if len(out) >= max_suggestions:
                    return out
    return out


def _count_rich_fields(contact: Contact) -> int:
    """Score a Contact by how many rich fields it has populated.

    Used to pick a canonical merge target when multiple Contact rows
    refer to the same person -- prefer the one with the most data so
    auto-merge never loses fields. Tie-break is left to the caller
    (usually lowest id = oldest).
    """
    return sum(
        1 for v in (
            contact.title, contact.company, contact.department,
            contact.primary_email, contact.phone, contact.notes,
        ) if v
    )


def _copy_rich_fields_if_empty(
    store: ClassificationStore,
    source: Contact,
    target_id: int,
) -> None:
    """Copy non-empty rich fields from `source` into target where target's
    field is empty. Uses update_contact_fields(fill_empty_only=True) so
    target's user-set values are preserved. Each per-field source flag
    on the target inherits source's last_enriched_source.
    """
    fields: dict[str, Optional[str]] = {}
    if source.title:
        fields["title"] = source.title
    if source.company:
        fields["company"] = source.company
    if source.department:
        fields["department"] = source.department
    if source.primary_email:
        fields["primary_email"] = source.primary_email
    if source.phone:
        fields["phone"] = source.phone
    if source.notes:
        fields["notes"] = source.notes
    if not fields:
        return
    src_tag = source.last_enriched_source or ENRICH_SOURCE_OUTLOOK
    store.update_contact_fields(
        target_id,
        source=src_tag,
        fill_empty_only=True,
        **fields,
    )


def resolve_calendar_attendee(
    store: ClassificationStore,
    *,
    name: str,
    email: str,
) -> Optional[ResolutionResult]:
    """Resolve a calendar attendee to a SINGLE canonical Contact.

    Auto-merges duplicates. The bug this fixes: separate code paths
    create separate Contact rows for the same person (email path
    creates a stub named after the local-part; attendee-by-name sync
    creates a bare row from the friendly name). Calling this from the
    calendar enrichment step BEFORE attendee-sync runs guarantees a
    single canonical row, and the friendly-name lookup downstream
    finds the same Contact.

    Algorithm:
      1. Find candidates by email-kind alias match (0 or 1).
      2. Find candidates by any-kind alias match for the friendly name
         (0..N).
      3. Union the candidate ids.
      4. If 0 candidates: create a fresh Contact with both email and
         name registered as aliases.
      5. If >= 1 candidates: pick the one with the most rich fields
         (tie-break = lowest id) as canonical. Copy each duplicate's
         rich fields into canonical (fill_empty_only) so no data is
         lost. Merge each duplicate into canonical via merge_contacts.
         Promote canonical's display_name to the friendly form if it
         currently looks like the email local-part. Ensure the email
         + the friendly name are both registered as aliases.

    Returns ResolutionResult or None if neither name nor email was
    provided.
    """
    name = (name or "").strip()
    email = (email or "").strip()
    if not name and not email:
        return None
    candidate_ids: list[int] = []
    seen: set[int] = set()

    def _add(contacts):
        for c in contacts:
            if c.id not in seen:
                candidate_ids.append(c.id)
                seen.add(c.id)

    if email and "@" in email:
        _add(store.find_contacts_by_alias(email, kind=ALIAS_KIND_EMAIL))
    if name:
        _add(store.find_contacts_by_alias(name))

    if not candidate_ids:
        # Create fresh.
        contact = store.create_contact(
            name or email.split("@", 1)[0],
            initial_alias_source=SOURCE_CALENDAR,
        )
        if email and "@" in email:
            try:
                store.add_alias(
                    contact.id, email,
                    kind=ALIAS_KIND_EMAIL, source=SOURCE_CALENDAR,
                )
            except ValueError:
                pass
        # Re-fetch so display_name reflects current row.
        refreshed = store.get_contact(contact.id) or contact
        return ResolutionResult(contact=refreshed, was_created=True)

    # Pick canonical: most rich fields, then lowest id.
    candidates = [store.get_contact(cid) for cid in candidate_ids]
    candidates = [c for c in candidates if c is not None]
    if not candidates:
        return None
    candidates.sort(key=lambda c: (-_count_rich_fields(c), c.id))
    canonical = candidates[0]
    duplicates = candidates[1:]

    # Copy rich fields from duplicates so merge_contacts doesn't drop them.
    for dup in duplicates:
        _copy_rich_fields_if_empty(store, dup, canonical.id)
    for dup in duplicates:
        try:
            store.merge_contacts(dup.id, canonical.id)
        except Exception:
            # Best-effort -- if a merge fails, leave both rows.
            continue

    # Promote display_name to friendly form if it looks like a stub.
    canonical = store.get_contact(canonical.id) or canonical
    if name and email and "@" in email:
        local_part = email.split("@", 1)[0].lower()
        current = (canonical.display_name or "").strip()
        if current.lower() == local_part and name != current:
            try:
                store.rename_contact(canonical.id, name)
            except Exception:
                pass

    # Ensure name + email aliases exist on canonical.
    if name:
        try:
            store.add_alias(
                canonical.id, name,
                kind=ALIAS_KIND_NAME, source=SOURCE_CALENDAR,
            )
        except (ValueError, Exception):
            pass
    if email and "@" in email:
        try:
            store.add_alias(
                canonical.id, email,
                kind=ALIAS_KIND_EMAIL, source=SOURCE_CALENDAR,
            )
        except (ValueError, Exception):
            pass

    refreshed = store.get_contact(canonical.id) or canonical
    return ResolutionResult(contact=refreshed, was_created=False)
