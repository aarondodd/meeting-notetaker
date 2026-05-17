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
