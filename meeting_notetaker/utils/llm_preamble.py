"""Strip conversational preambles from LLM synthesis responses (#102 bug 8).

Claude's clipboard-copy + Chrome-extension scrape captures whatever
the assistant message contains, including leading conversational
prose Claude sometimes writes before the synthesis proper:

    Let me research the transcript and put together a summary.

    ## Summary
    ...

Aaron's 2026-06-13 report: those preambles show up in saved notes.md
and downstream renders, where they read as the LLM's thinking
process instead of meeting content. This module strips them before
the synthesis hits the save path.

Conservative rule of thumb:
- Only act when the response starts with text (not a heading), AND
- a markdown heading appears further down, AND
- the leading text starts with one of the common conversational
  openers ("Let me", "I'll", "Here's", etc.).

When all three are true, we drop everything up to the first heading.
Otherwise return the input verbatim. This intentionally accepts
false negatives (rare preambles we don't recognize) over false
positives (legitimate synthesis content erroneously stripped).
"""
from __future__ import annotations

import re


# Common opener phrases that signal conversational preamble rather
# than synthesis content. Case-insensitive prefix match. Order
# isn't significant; longer phrases that contain shorter ones (e.g.
# "i am going to" vs "i'm") are listed separately so the shorter
# doesn't accidentally swallow a different sentence shape.
_PREAMBLE_STARTERS = (
    "let me",
    "let's",
    "i'll",
    "i will",
    "i'm going to",
    "i am going to",
    "here's",
    "here is",
    "looking at",
    "based on",
    "after reviewing",
    "after analyzing",
    "after looking",
    "i've reviewed",
    "i have reviewed",
    "i'll go through",
    "i'll synthesize",
    "i'll summarize",
    "i'll work through",
    "i need to",
    "first,",
    "first ",
    "to start,",
)


_HEADING_RE = re.compile(r"^#{1,6}\s", re.MULTILINE)


def strip_preamble(markdown: str) -> str:
    """Drop conversational openers preceding the first heading.

    Returns ``markdown`` unchanged when:
    - the response is empty / whitespace-only,
    - the response already starts with a markdown heading,
    - no heading appears anywhere (conservative -- avoids
      stripping a heading-less synthesis), or
    - the leading text doesn't match one of the known preamble
      openers (treat unrecognized leads as legitimate prose).
    """
    if not markdown or not markdown.strip():
        return markdown
    leading_ws = len(markdown) - len(markdown.lstrip())
    stripped = markdown[leading_ws:]
    if stripped.startswith("#"):
        return markdown
    heading_match = _HEADING_RE.search(stripped)
    if heading_match is None:
        return markdown
    preamble_text = stripped[: heading_match.start()].strip()
    if not preamble_text:
        return markdown
    lower = preamble_text.lower()
    if not any(lower.startswith(starter) for starter in _PREAMBLE_STARTERS):
        return markdown
    return stripped[heading_match.start():]
