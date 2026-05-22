"""LLM target catalog.

The catalog is the contract the settings UI + extension share. A typo
in the dropdown options or a forgotten entry breaks the Send button
silently, so we pin the expected shape here.
"""
from __future__ import annotations

import pytest

from meeting_notetaker.automation.targets import (
    ALL_TARGETS,
    CLAUDE,
    COPILOT,
    get_target,
)
from meeting_notetaker.utils.config import VALID_LLM_TARGETS


def test_claude_target_metadata():
    assert CLAUDE.key == "claude"
    assert CLAUDE.label == "Claude.ai"
    assert "claude.ai" in CLAUDE.new_chat_url
    assert "claude.ai" in CLAUDE.domain_match
    assert CLAUDE.enabled is True
    assert CLAUDE.implemented is True


def test_copilot_target_is_plumbed_but_unimplemented_in_063():
    """Settings shows Copilot in the dropdown but the Send button
    surfaces a 'not yet implemented' error if Copilot is selected.
    The flag flip happens when the Copilot content script ships."""
    assert COPILOT.key == "copilot"
    assert COPILOT.enabled is True
    assert COPILOT.implemented is False


def test_all_targets_match_config_whitelist():
    """The Config validator + the LLM target catalog must agree on
    which keys are valid. A mismatch makes round-trip tests pass but
    the actual UI broken."""
    catalog_keys = {t.key for t in ALL_TARGETS}
    assert catalog_keys == set(VALID_LLM_TARGETS)


def test_get_target_by_key_returns_dataclass():
    assert get_target("claude") is CLAUDE
    assert get_target("copilot") is COPILOT


def test_get_target_unknown_raises():
    """A typo in config.toml must blow up loudly rather than silently
    falling back to a default target -- the user's intent is unclear
    in that case and a fallback can route the prompt to the wrong LLM."""
    with pytest.raises(ValueError, match="unknown LLM target"):
        get_target("gemini")
