"""Vocabulary parsing + seeding."""
from __future__ import annotations

from meeting_notetaker.utils.paths import vocabulary_path
from meeting_notetaker.utils.vocabulary import (
    join_hotwords,
    load_vocabulary,
    parse_vocabulary,
    seed_vocabulary_file,
)


def test_parse_strips_comments_and_blank_lines():
    text = """# Header comment
Snowflake Cortex
# inline comment line

   Plantronics Voyager
"""
    assert parse_vocabulary(text) == ["Snowflake Cortex", "Plantronics Voyager"]


def test_parse_dedupes_case_insensitive():
    text = "Anthropic\nanthropic\nANTHROPIC\nClaude Code\n"
    assert parse_vocabulary(text) == ["Anthropic", "Claude Code"]


def test_parse_empty_returns_empty_list():
    assert parse_vocabulary("") == []
    assert parse_vocabulary("# only comments\n# nothing real\n") == []


def test_join_hotwords_strips_and_concats():
    assert join_hotwords(["  foo  ", "bar baz"]) == "foo bar baz"
    assert join_hotwords([]) == ""
    assert join_hotwords(["", "   "]) == ""


def test_seed_creates_file_with_template(isolated_data_dir):
    path = seed_vocabulary_file()
    assert path == vocabulary_path()
    assert path.exists()
    body = path.read_text(encoding="utf-8")
    assert body.startswith("#")
    # All seeded content is comments -> parsing yields no entries.
    assert parse_vocabulary(body) == []


def test_seed_idempotent_does_not_overwrite_user_edits(isolated_data_dir):
    path = seed_vocabulary_file()
    path.write_text("# my custom header\nMyCompany\n", encoding="utf-8")
    seed_vocabulary_file()
    assert path.read_text(encoding="utf-8") == "# my custom header\nMyCompany\n"


def test_load_vocabulary_missing_returns_empty(isolated_data_dir):
    assert load_vocabulary() == []


def test_load_vocabulary_round_trip(isolated_data_dir):
    path = vocabulary_path()
    path.write_text(
        "# hotwords\nFooBar\nbaz qux\n# trailing comment\n",
        encoding="utf-8",
    )
    assert load_vocabulary() == ["FooBar", "baz qux"]


# ---- per-session derivation ------------------------------------------------


def test_extract_proper_nouns_multi_word():
    from meeting_notetaker.utils.vocabulary import extract_proper_nouns

    text = "We will review Snowflake Cortex and the Customer Combine pipeline next."
    out = extract_proper_nouns(text)
    assert "Snowflake Cortex" in out
    assert "Customer Combine" in out


def test_extract_proper_nouns_all_caps_tokens():
    from meeting_notetaker.utils.vocabulary import extract_proper_nouns

    text = "Ticket EDAPA-737 hits AWS quota; ETA is unclear."
    out = extract_proper_nouns(text)
    assert "EDAPA-737" in out
    assert "AWS" in out
    # Sentence-initial "Ticket" should not be picked up as a proper-noun
    # phrase (single capitalized word) -- it's only a single token, not
    # multi-word or all-caps.
    assert "Ticket" not in out


def test_extract_proper_nouns_dedupes_case_insensitive():
    from meeting_notetaker.utils.vocabulary import extract_proper_nouns

    text = "AWS quota issue. AWS again. aws is fine."
    out = extract_proper_nouns(text)
    assert out == ["AWS"]


def test_extract_proper_nouns_empty():
    from meeting_notetaker.utils.vocabulary import extract_proper_nouns

    assert extract_proper_nouns("") == []
    assert extract_proper_nouns("nothing to see") == []


def test_derive_session_hotwords_order_global_first():
    from meeting_notetaker.utils.vocabulary import derive_session_hotwords

    out = derive_session_hotwords(
        ["Anthropic", "Snowflake Cortex"],
        attendees=["Alice", "Bob"],
        agenda="Discuss EDAPA-737 with the Customer Combine team.",
    )
    # Global vocab first, then attendees, then agenda-derived proper nouns.
    assert out[0] == "Anthropic"
    assert out[1] == "Snowflake Cortex"
    assert "Alice" in out
    assert "Bob" in out
    assert "EDAPA-737" in out
    assert "Customer Combine" in out


def test_derive_session_hotwords_dedupes_across_sources():
    from meeting_notetaker.utils.vocabulary import derive_session_hotwords

    out = derive_session_hotwords(
        ["EDAPA-737"],
        attendees=["EDAPA-737"],  # silly attendee, but exercises dedup
        agenda="EDAPA-737 status",
    )
    assert out.count("EDAPA-737") == 1


def test_derive_session_hotwords_handles_no_extras():
    from meeting_notetaker.utils.vocabulary import derive_session_hotwords

    out = derive_session_hotwords(["FooBar"])
    assert out == ["FooBar"]


def test_extract_section_basic():
    from meeting_notetaker.utils.live_notes import extract_section

    body = """# Attendees
- Alice
- Bob

# Agenda
Discuss Snowflake Cortex rollout.
Review the Customer Combine timeline.

# Notes
"""
    out = extract_section(body, "Agenda")
    assert "Snowflake Cortex" in out
    assert "Customer Combine" in out
    assert "Alice" not in out


def test_extract_section_missing_returns_empty():
    from meeting_notetaker.utils.live_notes import extract_section

    body = "# Attendees\n- Alice\n\n# Notes\n"
    assert extract_section(body, "Agenda") == ""


def test_extract_section_case_insensitive_heading():
    from meeting_notetaker.utils.live_notes import extract_section

    body = "# AGENDA\nThe Goal\n\n# Notes\n"
    out = extract_section(body, "Agenda")
    assert "The Goal" in out
