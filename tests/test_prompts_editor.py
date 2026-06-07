"""Tests for the in-app prompt editor's CRUD + archive API (#89).

Covers the API surface the editor dialog drives:

  * validate_prompt_name -- reject empty, overly long, unsafe names.
  * save_prompt -- writes new body atomically, archives prior.
  * create_prompt -- empty + body, refuses overwrite.
  * duplicate_prompt -- copies source body to fresh dest.
  * delete_prompt -- removes active file, optionally archives first.
  * list_archived_versions -- newest-first list with bodies.
  * restore_archived_version -- reverts, archives the just-replaced
    body so the operation is itself reversible.
  * is_bundled_prompt -- True for default/one-on-one/standup.

Pure-Python; uses the isolated_data_dir conftest fixture to redirect
%APPDATA% to tmp so writes never touch the user's real data dir.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from meeting_notetaker.utils.prompts import (
    ArchivedPrompt,
    PromptError,
    create_prompt,
    delete_prompt,
    duplicate_prompt,
    get_template,
    is_bundled_prompt,
    list_archived_versions,
    list_templates,
    restore_archived_version,
    save_prompt,
    validate_prompt_name,
)


# ---- validate_prompt_name ------------------------------------------------

def test_validate_accepts_simple_alphanumeric():
    assert validate_prompt_name("standup") == "standup"
    assert validate_prompt_name("daily-recap") == "daily-recap"
    assert validate_prompt_name("project_review") == "project_review"
    assert validate_prompt_name("a") == "a"


def test_validate_strips_leading_trailing_whitespace():
    assert validate_prompt_name("  standup  ") == "standup"


def test_validate_rejects_empty():
    with pytest.raises(PromptError):
        validate_prompt_name("")
    with pytest.raises(PromptError):
        validate_prompt_name("   ")


def test_validate_rejects_unsafe_characters():
    for bad in ("../etc/passwd", "name.with.dots", "name with space",
                "name/with/slash", "name\\with\\backslash", "name:colon"):
        with pytest.raises(PromptError):
            validate_prompt_name(bad)


def test_validate_rejects_leading_dot_or_underscore():
    """Underscore prefix is reserved for the _archive subdir + future
    internal conventions. Dot prefix is hidden-file convention; we
    avoid it."""
    with pytest.raises(PromptError):
        validate_prompt_name("_archive")
    with pytest.raises(PromptError):
        validate_prompt_name(".hidden")


def test_validate_rejects_overlong():
    with pytest.raises(PromptError):
        validate_prompt_name("x" * 65)


# ---- create_prompt -------------------------------------------------------

def test_create_prompt_writes_body(isolated_data_dir):
    path = create_prompt("custom", body="Hello {{transcript}}")
    assert path.exists()
    assert path.read_text(encoding="utf-8") == "Hello {{transcript}}"
    assert path.name == "custom.md"


def test_create_prompt_empty_body_allowed(isolated_data_dir):
    path = create_prompt("blank")
    assert path.read_text(encoding="utf-8") == ""


def test_create_prompt_refuses_overwrite(isolated_data_dir):
    create_prompt("custom", body="A")
    with pytest.raises(PromptError) as exc_info:
        create_prompt("custom", body="B")
    assert "already exists" in exc_info.value.reason


def test_create_prompt_appears_in_list_templates(isolated_data_dir):
    create_prompt("custom", body="C")
    names = [t.name for t in list_templates()]
    assert "custom" in names


# ---- save_prompt ---------------------------------------------------------

def test_save_prompt_creates_new_when_missing(isolated_data_dir):
    path = save_prompt("standup", "first body")
    assert path.read_text(encoding="utf-8") == "first body"


def test_save_prompt_archives_prior_body(isolated_data_dir):
    save_prompt("standup", "version 1")
    save_prompt("standup", "version 2")
    archived = list_archived_versions("standup")
    assert len(archived) == 1
    assert archived[0].body == "version 1"


def test_save_prompt_skips_archive_when_prior_blank(isolated_data_dir):
    """A blank current body isn't worth archiving -- the user is
    moving from blank-to-something, not editing real content."""
    save_prompt("standup", "")
    save_prompt("standup", "real content")
    archived = list_archived_versions("standup")
    assert len(archived) == 0


def test_save_prompt_multiple_versions_newest_first(isolated_data_dir):
    save_prompt("standup", "v1")
    save_prompt("standup", "v2")
    save_prompt("standup", "v3")
    archived = list_archived_versions("standup")
    # v3 is current (not in archive); v1 + v2 are archived; newest first.
    assert [a.body for a in archived] == ["v2", "v1"]


def test_save_prompt_atomic_write(isolated_data_dir, monkeypatch):
    """No .tmp file is left behind after a successful save."""
    save_prompt("standup", "body")
    leftover = list(Path(isolated_data_dir / "prompts").glob("*.tmp"))
    assert leftover == []


def test_save_prompt_rejects_invalid_name(isolated_data_dir):
    with pytest.raises(PromptError):
        save_prompt("../escape", "body")


# ---- duplicate_prompt ---------------------------------------------------

def test_duplicate_prompt_copies_body(isolated_data_dir):
    save_prompt("source", "original body")
    duplicate_prompt("source", "destination")
    dest = get_template("destination")
    assert dest is not None
    assert dest.body == "original body"


def test_duplicate_prompt_refuses_when_dest_exists(isolated_data_dir):
    save_prompt("source", "A")
    save_prompt("destination", "B")
    with pytest.raises(PromptError):
        duplicate_prompt("source", "destination")


def test_duplicate_prompt_refuses_when_source_missing(isolated_data_dir):
    with pytest.raises(PromptError) as exc_info:
        duplicate_prompt("missing", "destination")
    assert "not found" in exc_info.value.reason.lower()


# ---- delete_prompt -------------------------------------------------------

def test_delete_prompt_removes_active_file(isolated_data_dir):
    save_prompt("custom", "body")
    assert delete_prompt("custom") is True
    assert get_template("custom") is None


def test_delete_prompt_archives_first_by_default(isolated_data_dir):
    save_prompt("custom", "body")
    delete_prompt("custom")
    archived = list_archived_versions("custom")
    assert len(archived) == 1
    assert archived[0].body == "body"


def test_delete_prompt_skip_archive_when_requested(isolated_data_dir):
    save_prompt("custom", "body")
    delete_prompt("custom", archive_first=False)
    assert list_archived_versions("custom") == []


def test_delete_prompt_missing_returns_false(isolated_data_dir):
    assert delete_prompt("ghost") is False


def test_delete_bundled_then_seed_re_creates(isolated_data_dir):
    """Deleting a bundled prompt is allowed; seed_user_prompts brings
    the bundled body back on the next list call. The user's deletion
    archive remains so they can recover their custom version."""
    list_templates()  # triggers seed
    bundled_before = get_template("default")
    assert bundled_before is not None
    save_prompt("default", "my custom default")
    delete_prompt("default")
    # Re-seed by calling list_templates; bundled body should reappear.
    list_templates()
    after = get_template("default")
    assert after is not None
    assert after.body != "my custom default"
    # Archive contains the user-customized version.
    archived = list_archived_versions("default")
    assert any(a.body == "my custom default" for a in archived)


# ---- list_archived_versions ---------------------------------------------

def test_list_archived_versions_empty_when_no_archive(isolated_data_dir):
    save_prompt("custom", "first save")  # no prior; nothing archived
    assert list_archived_versions("custom") == []


def test_list_archived_versions_returns_archived_prompt_objects(isolated_data_dir):
    save_prompt("custom", "v1")
    save_prompt("custom", "v2")
    archived = list_archived_versions("custom")
    assert all(isinstance(a, ArchivedPrompt) for a in archived)
    assert all(a.path.exists() for a in archived)
    assert all(isinstance(a.saved_at, datetime) for a in archived)


def test_list_archived_versions_validates_name(isolated_data_dir):
    with pytest.raises(PromptError):
        list_archived_versions("../escape")


# ---- restore_archived_version -------------------------------------------

def test_restore_replaces_active_body(isolated_data_dir):
    save_prompt("custom", "v1")
    save_prompt("custom", "v2")
    archived = list_archived_versions("custom")
    target = next(a for a in archived if a.body == "v1")
    restore_archived_version("custom", target.path)
    current = get_template("custom")
    assert current.body == "v1"


def test_restore_archives_the_just_replaced_body(isolated_data_dir):
    """Restore is itself reversible: the body it replaced lands in
    the archive so the user can re-restore it."""
    save_prompt("custom", "v1")
    save_prompt("custom", "v2")
    archived_before = list_archived_versions("custom")
    target = next(a for a in archived_before if a.body == "v1")
    restore_archived_version("custom", target.path)
    archived_after = list_archived_versions("custom")
    # v2 should now be in the archive (it was replaced by v1).
    assert any(a.body == "v2" for a in archived_after)


def test_restore_rejects_cross_prompt_archive(isolated_data_dir):
    """A path that points at another prompt's archive must be refused."""
    save_prompt("alpha", "a")
    save_prompt("alpha", "a2")
    save_prompt("beta", "b")
    alpha_archives = list_archived_versions("alpha")
    with pytest.raises(PromptError) as exc_info:
        restore_archived_version("beta", alpha_archives[0].path)
    assert "not in" in exc_info.value.reason.lower()


def test_restore_missing_archive_raises(isolated_data_dir, tmp_path):
    save_prompt("custom", "v1")
    fake = isolated_data_dir / "prompts" / "_archive" / "custom" / "20260101-120000.md"
    with pytest.raises(PromptError):
        restore_archived_version("custom", fake)


# ---- is_bundled_prompt --------------------------------------------------

def test_is_bundled_prompt_true_for_defaults(isolated_data_dir):
    assert is_bundled_prompt("default") is True
    assert is_bundled_prompt("one-on-one") is True
    assert is_bundled_prompt("standup") is True


def test_is_bundled_prompt_false_for_custom(isolated_data_dir):
    save_prompt("custom", "x")
    assert is_bundled_prompt("custom") is False


def test_is_bundled_prompt_handles_invalid_name(isolated_data_dir):
    """Defense against the UI handing a bad name; return False rather
    than raising."""
    assert is_bundled_prompt("../escape") is False
    assert is_bundled_prompt("") is False


# ---- ArchivedPrompt --------------------------------------------------

def test_archived_prompt_saved_at_display():
    when = datetime(2026, 6, 7, 12, 34, 56, tzinfo=timezone.utc)
    ap = ArchivedPrompt(
        name="x", path=Path("/tmp/x.md"), saved_at=when, body="",
    )
    assert ap.saved_at_display == "2026-06-07 12:34:56"
