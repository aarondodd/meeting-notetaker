"""Heading numbering + table-of-contents transforms for markdown (#92).

Two opt-in document polish features that operate on the markdown
source before it reaches Preview / PDF / Notion / Confluence
converters. Source-level transforms = single source of truth for
all four output paths.

Numbering: walks heading lines, maintains per-level counters, prepends
'1.2.3 ' style prefixes. Resets deeper counters when a higher-level
heading is seen. Skipped levels (H1 jumping straight to H3) are
collapsed to match the actual outline depth.

TOC: walks heading lines, emits a markdown list with [Title](#slug)
entries indented by level, prepended under a '## Contents' heading.
Slug matches the standard Qt `QTextDocument.setMarkdown` convention
so internal anchors line up across Preview / PDF / Confluence and the
TOC is navigable wherever the renderer respects markdown link
anchors.

Both transforms skip content inside fenced code blocks (` ``` ` or
` ~~~ `) so heading-like lines inside code samples are left alone.

Pure-Python. No Qt. Cheap to test.
"""
from __future__ import annotations

import re
from typing import Iterator, List, Tuple


# Heading line: 1-6 leading `#`, at least one whitespace, body text.
# Captures the marker (group 1), the space (group 2), and the body
# (group 3). The body is everything after the leading whitespace; we
# trim it at output time so the numbering prefix sits flush against
# the text.
_HEADING_RE = re.compile(r"^(#{1,6})(\s+)(.*?)\s*$")

# Fenced code-block delimiter -- backticks or tildes, three or more,
# at line start (possibly with leading whitespace). The body of a
# fence is opaque to our transforms.
_FENCE_RE = re.compile(r"^\s*(```+|~~~+)")


# Default max depth for the generated TOC. Deeper than 3 produces a
# wall of text for typical meeting notes; configurable per call.
DEFAULT_TOC_MAX_DEPTH = 3

# TOC heading label. Standard short form; kept as a constant so the
# downstream removal helper (if we ever add one) can match exactly.
TOC_HEADING = "## Contents"

# Separator between the TOC block and the body content.
TOC_SEPARATOR = "---"


# ---- slug ----------------------------------------------------------------

def slugify(text: str) -> str:
    """Convert heading text into a markdown anchor slug.

    Matches the standard convention used by Qt's `setMarkdown` and
    most popular markdown renderers: lowercase, replace any run of
    non-alphanumeric characters with a single dash, strip dashes
    from both ends. Empty input returns empty.

    Examples:
        "Project Overview"           -> "project-overview"
        "Q3 / Q4 Planning"           -> "q3-q4-planning"
        "1.2.3 Some Heading"         -> "1-2-3-some-heading"
        "API Reference (v2)"         -> "api-reference-v2"
    """
    if not text:
        return ""
    lowered = text.lower()
    # Replace any run of non-alphanumeric ASCII chars with a single dash.
    slug = re.sub(r"[^a-z0-9]+", "-", lowered)
    return slug.strip("-")


# ---- heading iteration ---------------------------------------------------

def iter_headings(text: str) -> Iterator[Tuple[int, int, str]]:
    """Yield (line_index, level, body_text) for every heading line.

    Skips fenced code blocks (` ``` ` or ` ~~~ `). Heading bodies are
    returned trimmed of trailing whitespace; the line_index is the
    zero-based index into the source's lines list so the caller can
    locate the heading for in-place modification.
    """
    in_fence = False
    for idx, line in enumerate(text.splitlines()):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING_RE.match(line)
        if m is None:
            continue
        level = len(m.group(1))
        body = m.group(3).strip()
        yield idx, level, body


# ---- numbering ----------------------------------------------------------

def number_headings(text: str, *, skip_h1: bool = False) -> str:
    """Prepend dotted-decimal numbering to every heading.

    Counters are tracked per level (1-6). A heading at level N
    increments counter[N] and resets all deeper counters to zero.
    If a level is skipped (H1 -> H3 without H2), the missing
    intermediate counter stays 0 and is suppressed from the prefix
    so output stays clean ("1.0.1 Foo" would be ugly -- we emit
    "1.1 Foo" instead).

    When `skip_h1` is True, H1 headings are treated as document
    titles and left unnumbered. H2 becomes the top-level numbering
    slot ("1"), H3 becomes "1.1", and so on. Useful when the doc
    convention reserves H1 for the page title and the user wants
    the numbered outline to start one level deeper.

    Skips fenced code blocks. Heading lines that lack body text are
    left alone (a bare "##" with nothing after it doesn't make sense
    to number).

    Pure transform on the source; no mutation of the input string.
    """
    lines = text.splitlines(keepends=True)
    counters = [0, 0, 0, 0, 0, 0]
    in_fence = False
    for i, line in enumerate(lines):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING_RE.match(line)
        if m is None:
            continue
        body = m.group(3).strip()
        if not body:
            continue
        level = len(m.group(1))
        # When skip_h1 is on, H1 stays untouched and H2 becomes the
        # top-level counter slot. effective_level < 1 means "no
        # numbering" (i.e. an H1 we should leave alone).
        effective_level = level - 1 if skip_h1 else level
        if effective_level < 1:
            continue
        counters[effective_level - 1] += 1
        # Reset deeper levels.
        for j in range(effective_level, 6):
            counters[j] = 0
        # Compose prefix from non-zero counters (suppresses gaps
        # left by skipped levels).
        parts = [str(c) for c in counters[:effective_level] if c > 0]
        if not parts:
            continue
        prefix = ".".join(parts)
        # Preserve the original line ending.
        ending = ""
        if line.endswith("\r\n"):
            ending = "\r\n"
        elif line.endswith("\n"):
            ending = "\n"
        elif line.endswith("\r"):
            ending = "\r"
        lines[i] = f"{m.group(1)}{m.group(2)}{prefix} {body}{ending}"
    return "".join(lines)


# ---- TOC generation -----------------------------------------------------

def generate_toc(
    text: str,
    *,
    max_depth: int = DEFAULT_TOC_MAX_DEPTH,
    skip_h1: bool = False,
) -> str:
    """Return a markdown TOC for `text`, or "" if no headings exist.

    Output shape:

        ## Contents

        - [H1 text](#h1-text)
          - [H2 text](#h2-text)
            - [H3 text](#h3-text)
        - [Next H1](#next-h1)

        ---

    Indentation is two spaces per level so the standard markdown
    rendering produces a nested bullet list. The trailing
    `---` horizontal rule separates the TOC from the body content
    so the visual transition is unambiguous.

    Heading levels deeper than `max_depth` are omitted. Headings
    inside fenced code are skipped.

    When `skip_h1` is True, H1 headings are omitted from the TOC
    (treated as the document title) and H2 becomes the top-level
    entry. The `max_depth` is interpreted against the post-skip
    level so `max_depth=3` with `skip_h1=True` includes H2 through
    H4 -- matching the numbering's effective-level semantic.

    Returns the empty string when no headings are present, so the
    caller's `prefix + body` composition is harmless either way.
    """
    if max_depth < 1:
        return ""
    headings: List[Tuple[int, str]] = []
    for _, level, body in iter_headings(text):
        if not body:
            continue
        effective_level = level - 1 if skip_h1 else level
        if effective_level < 1 or effective_level > max_depth:
            continue
        headings.append((effective_level, body))
    if not headings:
        return ""
    out: List[str] = [TOC_HEADING, ""]
    for level, body in headings:
        indent = "  " * (level - 1)
        slug = slugify(body)
        out.append(f"{indent}- [{body}](#{slug})")
    out.append("")
    out.append(TOC_SEPARATOR)
    out.append("")
    # Trailing newline so the body starts on a fresh line when
    # concatenated.
    return "\n".join(out) + "\n"


def inject_toc(
    text: str,
    *,
    max_depth: int = DEFAULT_TOC_MAX_DEPTH,
    skip_h1: bool = False,
) -> str:
    """Prepend a generated TOC to `text`. No-op when no headings."""
    toc = generate_toc(text, max_depth=max_depth, skip_h1=skip_h1)
    if not toc:
        return text
    return toc + text


# ---- orchestrator ------------------------------------------------------

def apply_outline(
    text: str,
    *,
    number: bool = False,
    toc: bool = False,
    skip_h1: bool = False,
    max_depth: int = DEFAULT_TOC_MAX_DEPTH,
) -> str:
    """Apply numbering and/or TOC transforms in the correct order.

    Numbering runs first so the TOC's heading text already carries
    the numeric prefix when the TOC is generated. This means a TOC
    entry like "1.2 Goals" matches the numbered heading exactly,
    which is what users expect and what makes the slug-based
    anchor link find its target.

    `skip_h1` and `max_depth` are forwarded to both transforms so
    "H1 is the title" is handled consistently across numbering and
    TOC.
    """
    if number:
        text = number_headings(text, skip_h1=skip_h1)
    if toc:
        text = inject_toc(text, max_depth=max_depth, skip_h1=skip_h1)
    return text
