"""user_name plumbing through transcript labels and prompt rendering."""
from __future__ import annotations

from meeting_notetaker.models.transcript import (
    DEFAULT_USER_LABEL,
    TranscriptSegment,
    format_segment,
    label_for,
    rewrite_user_label,
)
from meeting_notetaker.utils import prompts as prompts_mod


def _seg(source: str, t_start: float = 0.0) -> TranscriptSegment:
    return TranscriptSegment(source=source, text="hello", t_start=t_start, t_end=t_start + 1.0)


def test_label_for_falls_back_to_me_when_user_name_empty():
    assert label_for("mic") == DEFAULT_USER_LABEL
    assert label_for("mic", "") == DEFAULT_USER_LABEL
    assert label_for("mic", "   ") == DEFAULT_USER_LABEL


def test_label_for_uses_user_name_when_set():
    assert label_for("mic", "Aaron") == "Aaron"


def test_label_for_sys_unchanged_by_user_name():
    assert label_for("sys", "Aaron") == "Them"


def test_format_segment_with_user_name():
    line = format_segment(_seg("mic", 65.0), "Aaron")
    assert line == "[00:01:05] Aaron: hello"


def test_format_segment_without_user_name_uses_me():
    line = format_segment(_seg("mic", 65.0))
    assert line == "[00:01:05] Me: hello"


def test_rewrite_user_label_replaces_line_prefixes():
    raw = "[00:00:05] Me: Hello\n[00:00:08] Them: Hi Aaron\n"
    out = rewrite_user_label(raw, "Aaron")
    assert "[00:00:05] Aaron: Hello" in out
    assert "[00:00:08] Them: Hi Aaron" in out  # untouched


def test_rewrite_user_label_does_not_touch_content_containing_me():
    raw = "[00:00:05] Them: Tell me about the project\n"
    assert rewrite_user_label(raw, "Aaron") == raw


def test_rewrite_user_label_noop_when_user_name_blank_or_default():
    raw = "[00:00:05] Me: Hello\n"
    assert rewrite_user_label(raw, "") == raw
    assert rewrite_user_label(raw, "Me") == raw


def test_render_substitutes_user_name_placeholder():
    body = "User: {{user_name}}\n{{transcript}}"
    out = prompts_mod.render(
        body,
        session_title="x",
        session_date="2026-05-15",
        transcript="[00:00:05] Me: Hi",
        user_name="Aaron",
    )
    assert "User: Aaron" in out
    assert "[00:00:05] Aaron: Hi" in out


def test_render_user_name_placeholder_falls_back_to_me():
    body = "{{user_name}}"
    out = prompts_mod.render(
        body, session_title="x", session_date="2026-05-15", transcript="", user_name=""
    )
    assert out == "Me"


def test_render_rewrites_transcript_me_to_user_name():
    body = "{{transcript}}"
    out = prompts_mod.render(
        body,
        session_title="x",
        session_date="2026-05-15",
        transcript="[00:00:05] Me: line one\n[00:00:08] Them: line two",
        user_name="Aaron Dodd",
    )
    assert "[00:00:05] Aaron Dodd: line one" in out
    assert "[00:00:08] Them: line two" in out


def test_render_leaves_transcript_alone_when_user_name_blank():
    body = "{{transcript}}"
    out = prompts_mod.render(
        body,
        session_title="x",
        session_date="2026-05-15",
        transcript="[00:00:05] Me: line",
    )
    assert "[00:00:05] Me: line" in out
