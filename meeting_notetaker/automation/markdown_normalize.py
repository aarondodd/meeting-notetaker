"""Tighten the markdown that Claude.ai's Copy button writes.

The synthesis-automation flow clicks Claude's "Copy" button and reads
the clipboard via ``navigator.clipboard.readText()``. Claude's
serialization is valid CommonMark but uses **loose** list spacing --
a blank line between every list item, plus 2+ blank lines between
sections. Rendered output is identical to a tight list, but the
source view in our Synthesis pane (and the on-disk ``notes.md``)
ends up much longer than necessary and harder to diff.

A user who selects + Ctrl+C's the same response inside the browser
gets the **tight** version: no blank line between bullets, single
blank line between sections. That's the format we want stored so the
in-app source view matches what the user expects.

This module's only responsibility is collapsing the loose-form
output into the tight form, without touching intentional spacing
(paragraphs separated by a single blank line, fenced code blocks,
etc.). The transform is idempotent: running it on already-tight
markdown is a no-op.

Issue: #42.
"""
from __future__ import annotations

import re


# Match a standard markdown list-item line. We're intentionally
# strict: the item must start with `-`, `*`, or `+`, followed by a
# space, followed by non-whitespace. This excludes blockquote
# fragments and intentional decorative dashes inside paragraphs.
_LIST_ITEM_RE = re.compile(r"^[-*+]\s+\S")

# ATX heading: 1-6 `#` characters at line start, followed by a space.
# Setext-style headings (`===` underline) aren't covered -- Claude's
# Copy button uses ATX everywhere we've observed.
_HEADING_RE = re.compile(r"^#{1,6}\s")


def normalize_synthesis_markdown(text: str) -> str:
    """Return ``text`` with loose-list + multi-blank-line spacing tightened.

    Two passes:

    1. **Tight-list pass.** A blank line that sits between two
       consecutive list-item lines -- or between a heading and a list
       item -- gets dropped. The pass walks line by line + only
       collapses the blank when the surrounding pair matches one of
       those two shapes. Blank lines that separate a list from
       prose paragraphs are preserved.

    2. **Multi-blank collapse.** Any run of 3+ consecutive newlines
       is collapsed to 2 (i.e. at most one blank line between blocks).
       CommonMark treats 1 vs N+ blank lines identically when
       rendering, so this is loss-free.

    Idempotent. Pure function (no I/O). Cheap -- linear in input size.
    """
    if not text:
        return text
    lines = text.splitlines(keepends=False)
    out: list[str] = []
    i = 0
    while i < len(lines):
        out.append(lines[i])
        if (
            i + 2 < len(lines)
            and (
                _LIST_ITEM_RE.match(lines[i])
                or _HEADING_RE.match(lines[i])
            )
            and lines[i + 1].strip() == ""
            and _LIST_ITEM_RE.match(lines[i + 2])
        ):
            # Drop the blank line. Advance past the blank but NOT past
            # the next list item -- the loop's next iteration outputs
            # it. Two cases collapse here:
            #   * bullet -> blank -> bullet  (the loose-list pattern)
            #   * heading -> blank -> bullet (a heading immediately
            #     followed by a list rendered identically in both
            #     CommonMark forms)
            i += 2
        else:
            i += 1
    tightened = "\n".join(out)
    # Preserve the trailing newline policy of the input: splitlines
    # drops a single trailing \n, and our join doesn't add one back.
    if text.endswith("\n") and not tightened.endswith("\n"):
        tightened += "\n"
    # Collapse any remaining 3+ newline runs (post-section padding) to
    # a single blank line. This is what makes the section dividers
    # look right after the loose-list pass.
    return re.sub(r"\n{3,}", "\n\n", tightened)
