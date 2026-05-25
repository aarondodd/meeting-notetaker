"""Deterministic topic extractor over synthesis text.

The extractor harvests three signal kinds (acronyms, backticked
strings, capitalized nouns >= N occurrences) and never makes an LLM
call. Tests pin the precision rules so a future tweak doesn't drift
into "extracts everything" or "extracts nothing" mode.
"""
from __future__ import annotations

from meeting_notetaker.utils.topic_extractor import (
    DEFAULT_MAX_TOPICS,
    extract_topics,
)


def test_extracts_acronym():
    body = """
# Meeting Notes

We discussed the MDM rollout timeline. The MDM strategy doc
needs review by Friday.
"""
    out = extract_topics(body)
    assert "MDM" in out


def test_extracts_backticked_project_name():
    body = """
The data flows through `Apache Kafka` then `Informatica`
before landing in `Snowflake`.
"""
    out = extract_topics(body)
    assert "Apache Kafka" in out
    assert "Informatica" in out
    assert "Snowflake" in out


def test_extracts_capitalized_noun_with_repeats():
    body = """
Informatica was the main topic today. Informatica's licensing
model came up. Informatica scaling concerns from last week.
"""
    out = extract_topics(body)
    assert "Informatica" in out


def test_does_not_extract_single_capitalized_noun():
    """A one-off proper noun isn't a topic (default min=2)."""
    body = "The meeting referenced Atlanta exactly once. Other content here."
    out = extract_topics(body)
    assert "Atlanta" not in out


def test_drops_common_stopwords_even_when_capitalized():
    """Day names and other generic capitalized strings are noise --
    'Monday Tuesday Tuesday' must not surface as a topic."""
    body = "Tuesday's planning meeting. We agreed Tuesday's followup."
    out = extract_topics(body)
    assert "Tuesday" not in out


def test_extracts_dedupes_case_collapse():
    """MDM and mdm collapse to the canonical first-seen form."""
    body = "Initial MDM design. mdm rollout. MDM strategy doc."
    out = extract_topics(body)
    cap_mdm = [t for t in out if t.upper() == "MDM"]
    assert len(cap_mdm) == 1
    assert cap_mdm[0] == "MDM"   # first-seen capitalization wins


def test_respects_max_topics_cap():
    body = "\n".join(
        f"`Project{i}` was discussed by Alice and Bob and Carol." for i in range(20)
    )
    out = extract_topics(body, max_topics=5)
    assert len(out) == 5


def test_respects_extra_stopwords_kwarg():
    """Attendee names already captured as People shouldn't double-
    surface as topics."""
    body = "Alice led the MDM discussion. Alice's MDM doc is shared."
    without_filter = extract_topics(body)
    assert "Alice" in without_filter
    with_filter = extract_topics(body, extra_stopwords=["Alice"])
    assert "Alice" not in with_filter
    # MDM still survives.
    assert "MDM" in with_filter


def test_empty_input_returns_empty():
    assert extract_topics("") == []


def test_fenced_code_block_contributes_topics():
    """Fenced code blocks are the LLM's verbatim convention; we
    treat them as strong signal."""
    body = """
The schema uses these columns:

```
ProjectID, ProjectName, OwnerEmail
```

Discussion focused on ProjectName and OwnerEmail.
"""
    out = extract_topics(body)
    # ProjectName + OwnerEmail appear in both the code block AND the
    # following prose -- they should rank high.
    assert "ProjectName" in out
    assert "OwnerEmail" in out


def test_blockquote_content_is_skipped():
    """Quoted attribution lines ('> source: original transcript')
    shouldn't generate topic suggestions."""
    body = """
> Source: original transcript line, do not treat as content.
> ImaginaryProject was mentioned here.
> AnotherImaginaryProject too.

Real content: MDM strategy.
"""
    out = extract_topics(body)
    assert "ImaginaryProject" not in out
    assert "AnotherImaginaryProject" not in out
    assert "MDM" in out


def test_default_max_topics_constant_sane():
    """Surface this number in case the issue acceptance criteria
    move around -- the chip row UX assumes <= ~6-8."""
    assert 4 <= DEFAULT_MAX_TOPICS <= 12


def test_backtick_wins_against_single_noun_repeat():
    """Backtick contributes weight 3 per occurrence; a CapitalizedNoun
    contributes weight 1 per occurrence. With matched counts, the
    backticked signal wins. Repeated capitalized nouns can still
    outrank a single backtick when they appear often enough (4+
    repeats), which matches the intent: explicit user marker beats
    one-off mention, repeated mention beats one-off marker."""
    body = """
`SignalKafka` came up briefly.

NoiseNoise NoiseNoise.
"""
    out = extract_topics(body)
    # SignalKafka (1 hit * weight 3 = 3) > NoiseNoise (2 hits * weight 1 = 2).
    assert "SignalKafka" in out and "NoiseNoise" in out
    assert out.index("SignalKafka") < out.index("NoiseNoise")


def test_extra_stopword_match_is_case_insensitive():
    """Aaron reported the extractor surfacing first names that
    MainApp had passed as stopwords -- root cause was the lower-
    case stopword "alice" not matching the upper-case "Alice" in
    the body. Case-insensitive comparison fixes it."""
    body = "Alice mentioned the project. Alice's plan is solid. Alice."
    out = extract_topics(body, extra_stopwords=["alice"])
    assert "Alice" not in out


def test_extra_stopword_match_is_case_insensitive_reverse():
    """And in the other direction -- a Title-case stopword
    suppresses a lowercase body mention."""
    body = "When mdm gets rolled out, mdm phase 3 starts. mdm."
    out = extract_topics(body, extra_stopwords=["MDM"])
    assert all(t.upper() != "MDM" for t in out)


def test_stopword_suppresses_first_name_even_with_many_mentions():
    """The headline behavior MainApp depends on for issue #24's
    name-noise fix -- many mentions of a known person's first name
    don't surface as a topic even though the count would normally
    qualify."""
    body = (
        "Bob said the MDM rollout is on track. Bob followed up "
        "with Bob's notes from the last meeting. Bob suggested..."
    )
    out = extract_topics(body, extra_stopwords=["Bob"])
    assert "Bob" not in out
    # And the real topic still survives.
    assert "MDM" in out


def test_min_noun_occurrences_configurable():
    """Use a word that isn't in the curated stopword list -- 'Topic'
    is, since meeting-note structure markers like '## Topic' would
    otherwise drown the real content."""
    body = "Cassandra came up. Cassandra was discussed twice."
    out = extract_topics(body, min_noun_occurrences=3)
    assert "Cassandra" not in out
    out2 = extract_topics(body, min_noun_occurrences=2)
    assert "Cassandra" in out2
