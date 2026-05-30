"""Parser tests for the LLM-emitted Suggested Topics appendix (#57).

The synthesis system prompt instructs the LLM to emit a final
``## Suggested Topics (auto-extracted)`` section with a JSON code
block of short topic strings. parse_topic_appendix is tolerant of
malformed input -- nothing here raises; missing / malformed input
just returns an empty list, the same posture as the attendee
appendix parser.
"""
from __future__ import annotations

import pytest

from meeting_notetaker.utils.topic_appendix import (
    find_appendix_span,
    parse_topic_appendix,
    strip_appendix,
)


def test_parse_extracts_topics():
    md = """# TL;DR
Body content.

## Suggested Topics (auto-extracted)

```json
["Q3 hiring", "Backend migration", "Customer onboarding"]
```
"""
    topics = parse_topic_appendix(md)
    assert topics == ["Q3 hiring", "Backend migration", "Customer onboarding"]


def test_parse_missing_section_returns_empty():
    md = "# TL;DR\nNo topics here."
    assert parse_topic_appendix(md) == []


def test_parse_missing_json_block_returns_empty():
    md = """## Suggested Topics (auto-extracted)

The LLM forgot the JSON.
"""
    assert parse_topic_appendix(md) == []


def test_parse_malformed_json_returns_empty():
    md = """## Suggested Topics (auto-extracted)

```json
[this isn't valid]
```
"""
    assert parse_topic_appendix(md) == []


def test_parse_non_array_returns_empty():
    md = """## Suggested Topics (auto-extracted)

```json
{"topics": ["Q3"]}
```
"""
    assert parse_topic_appendix(md) == []


def test_parse_skips_non_strings_and_empties():
    md = """## Suggested Topics (auto-extracted)

```json
["Real topic", "", "  ", 42, null, "Another"]
```
"""
    topics = parse_topic_appendix(md)
    assert topics == ["Real topic", "Another"]


def test_parse_dedupes_case_insensitively():
    """First occurrence's casing wins; later duplicates are dropped."""
    md = """## Suggested Topics (auto-extracted)

```json
["Q3 Hiring", "q3 hiring", "Backend Migration", "BACKEND MIGRATION"]
```
"""
    topics = parse_topic_appendix(md)
    assert topics == ["Q3 Hiring", "Backend Migration"]


def test_parse_handles_appendix_followed_by_attendees_section():
    """Both appendices may coexist; the topics span stops at the next
    ## heading rather than running to end-of-string."""
    md = """# TL;DR
body

## Suggested Topics (auto-extracted)

```json
["Topic A"]
```

## Attendee Details (auto-extracted)

```json
[{"name": "Bob"}]
```
"""
    topics = parse_topic_appendix(md)
    assert topics == ["Topic A"]


def test_parse_case_insensitive_heading():
    """Match the heading regardless of case normalization the LLM
    might apply."""
    md = """## SUGGESTED TOPICS (AUTO-EXTRACTED)

```json
["A topic"]
```
"""
    assert parse_topic_appendix(md) == ["A topic"]


def test_parse_no_language_tag_on_code_block():
    """Some models emit ``` instead of ```json."""
    md = """## Suggested Topics (auto-extracted)

```
["Topic A"]
```
"""
    assert parse_topic_appendix(md) == ["Topic A"]


def test_strip_removes_section_cleanly():
    md = """# TL;DR
Body.

## Suggested Topics (auto-extracted)

```json
["A"]
```
"""
    out = strip_appendix(md)
    assert "Suggested Topics" not in out
    assert "Body." in out


def test_strip_preserves_following_attendee_section():
    """Topics is stripped but the Attendee Details appendix that
    follows it survives, since its stripper runs separately."""
    md = """# TL;DR
body

## Suggested Topics (auto-extracted)

```json
["Topic A"]
```

## Attendee Details (auto-extracted)

```json
[{"name": "Bob"}]
```
"""
    out = strip_appendix(md)
    assert "Suggested Topics" not in out
    assert "Attendee Details" in out
    assert '{"name": "Bob"}' in out


def test_find_appendix_span_returns_offsets():
    md = "# H1\n\n## Suggested Topics (auto-extracted)\n\nstuff"
    span = find_appendix_span(md)
    assert span is not None
    start, end = span
    assert md[start:end].startswith("## Suggested Topics")


def test_find_appendix_span_none_when_missing():
    assert find_appendix_span("# TL;DR\nbody") is None
