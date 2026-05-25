"""Deterministic topic extraction from synthesis output.

Per issue #24's design constraint, this MUST NOT call an LLM. Instead
it harvests three signal kinds that are reliably present in well-
formed meeting notes:

1. **Code-fenced or backticked strings** -- the LLM's own
   convention for project names, technology names, and other
   verbatim terms.
2. **ALL_CAPS acronyms** -- the universal meeting-note signature
   for project / technology / domain markers (MDM, EDA, EDAPA,
   SAP, ETL).
3. **CapitalizedNouns appearing twice or more** -- the
   conversational signature of a real topic vs an incidental
   mention ("Informatica" appearing once in passing vs four times
   means it's a topic).

Output is a deduped, lowercase-keyed, max-N suggestion list with
stable preferred-display capitalization (preserves the form the
text used).

Treats this as a "good first guess" the user reviews -- false
positives are cheap (one click to reject); false negatives are
cheap (one click to add a missing topic via the chips row).
"""
from __future__ import annotations

import re
from collections import Counter, OrderedDict
from typing import Iterable, Optional


# Maximum number of topics returned per extraction. The chips row
# tops out at ~6 before wrapping; suggestions beyond that buried in a
# popover would be unreviewed by most users. Tunable per the issue's
# acceptance criteria.
DEFAULT_MAX_TOPICS = 8

# Minimum count for a CapitalizedNoun to qualify as a topic
# suggestion. Hand-tuned: 2 is too aggressive (every name a meeting
# said becomes a topic); 3 misses topics in short meetings. Default
# 2 with the stopword filter handling most noise.
DEFAULT_MIN_NOUN_OCCURRENCES = 2


# Stopwords -- proper-noun-looking words that show up in every
# meeting and aren't topics. Keep this list short + curated; tooling
# noise filtering relies on the heading-section filter below, not on
# this list.
_TITLE_STOPWORDS = frozenset({
    "TBD", "TBA", "FYI", "AFAIK", "ETA", "AOB", "OK", "TLDR",
    "I", "We", "They", "It", "He", "She", "You", "Us", "Me",
    "The", "A", "An", "And", "Or", "But", "If", "Then", "So",
    "This", "That", "These", "Those", "Now", "Today", "Tomorrow",
    "Yesterday", "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
    "Note", "Notes", "Action", "Items", "Item", "Decisions",
    "Attendees", "Agenda", "Goals", "Goal", "Status", "Topic",
    "Topics", "Summary", "Overview", "Discussion", "Q&A",
    "Yes", "No", "Maybe",
})


# Word-shape regexes. Compiled once at module load.

# ALL_CAPS_WITH_OPTIONAL_DIGITS_AND_INTERNAL_HYPHENS.
# Length floor (2) drops "I" / "A" but keeps "OS" / "QA". Cap at
# 8 chars before requiring a vowel-light pattern to skip URL paths
# masquerading as acronyms.
_ACRONYM_RE = re.compile(r"\b[A-Z][A-Z0-9]+(?:-[A-Z0-9]+)*\b")

# CapitalizedNoun: leading uppercase + at least 2 more chars (avoids
# treating standalone "I" / "A" as nouns). Allows internal-cap
# words like CamelCase but not all-caps strings (covered separately).
_CAP_NOUN_RE = re.compile(r"\b[A-Z][a-z][A-Za-z]+\b")

# Backtick-quoted code spans. The trailing `+?` avoids consuming
# multiple short spans on the same line.
_BACKTICK_RE = re.compile(r"`([^`\n]+?)`")

# Triple-backtick fenced code blocks (and language hint).
_FENCED_BLOCK_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)

# Block-quote prefixes the extractor should NOT look at -- "> some
# meta line" rarely contains topic-worthy content (it's the LLM's
# attribution or section divider).
_BLOCKQUOTE_LINE_RE = re.compile(r"^\s*>", re.MULTILINE)


def extract_topics(
    body: str,
    *,
    max_topics: int = DEFAULT_MAX_TOPICS,
    min_noun_occurrences: int = DEFAULT_MIN_NOUN_OCCURRENCES,
    extra_stopwords: Optional[Iterable[str]] = None,
) -> list[str]:
    """Pull a deduped, ranked list of topic suggestions from synthesis text.

    Returns at most `max_topics` strings in stable rank order
    (most-evidence-first). Each returned topic preserves the
    capitalization it had in the input.

    `extra_stopwords` lets MainApp inject a per-session ignore list
    (the user's own name + every known person's name + every
    attendee name + name tokens, so first-name mentions of people
    don't double-surface as topic suggestions). Matching is
    case-insensitive: "alice" in stopwords suppresses "Alice" /
    "ALICE" / "alice" alike.
    """
    if not body:
        return []
    # Stopwords stored lowercased; _credit lowercases tokens before
    # comparison. Case-insensitive lookup is the right call here --
    # a meeting note that mentions "ALICE" in title case shouldn't
    # surface as a topic just because the MainApp stopword list
    # carried it as "Alice".
    stopwords = {s.lower() for s in _TITLE_STOPWORDS}
    if extra_stopwords:
        for word in extra_stopwords:
            if word:
                stopwords.add(word.strip().lower())

    # Counter keyed on lowercased form for dedup; first-seen
    # canonical capitalization preserved separately. OrderedDict
    # gives stable insertion-order rank when scores tie.
    scores: Counter[str] = Counter()
    display: OrderedDict[str, str] = OrderedDict()

    # 1. Fenced code blocks (the strongest signal -- LLM only
    #    fences things it considers verbatim / proper names).
    for match in _FENCED_BLOCK_RE.finditer(body):
        block = match.group(1)
        for raw_token in re.findall(r"\b[A-Za-z][\w.-]*\b", block):
            _credit(raw_token, scores, display, weight=3, stopwords=stopwords)

    # Strip blockquotes before scanning prose -- the LLM's
    # attribution lines ("> source: original transcript") are
    # signal-poor.
    prose_body = _BLOCKQUOTE_LINE_RE.sub("", body)

    # 2. Backtick spans (still strong; LLM convention for project /
    #    tool / file names).
    for match in _BACKTICK_RE.finditer(prose_body):
        token = match.group(1).strip()
        if not token:
            continue
        # Treat each backtick payload as one unit even if it's a
        # multi-word phrase; preserves "Apache Kafka" as one topic.
        _credit(token, scores, display, weight=3, stopwords=stopwords)

    # 3. ALL_CAPS acronyms (universal meeting-note signature).
    for match in _ACRONYM_RE.finditer(prose_body):
        _credit(match.group(0), scores, display, weight=2, stopwords=stopwords)

    # 4. CapitalizedNouns with min occurrence count.
    noun_counts: Counter[str] = Counter()
    for match in _CAP_NOUN_RE.finditer(prose_body):
        token = match.group(0)
        if token.lower() in stopwords:
            continue
        noun_counts[token.lower()] += 1
        # Remember the first capitalization seen.
        display.setdefault(token.lower(), token)
    for key, count in noun_counts.items():
        # The lowered key is the comparison form (so stopwords from
        # extra_stopwords like "alice" suppress a "Alice" that
        # squeaked past the per-token check above through
        # display.setdefault -- defense in depth).
        if key in stopwords:
            continue
        if count >= min_noun_occurrences:
            scores[key] += count   # base score = occurrences

    # 5. Top-N by score; ties broken by first-seen order
    #    (OrderedDict iteration).
    ranked = sorted(
        scores.items(),
        key=lambda kv: (-kv[1], list(display).index(kv[0]) if kv[0] in display else 1_000_000),
    )
    out: list[str] = []
    for key, _score in ranked:
        if len(out) >= max_topics:
            break
        out.append(display.get(key, key))
    return out


def _credit(
    raw_token: str,
    scores: Counter[str],
    display: OrderedDict[str, str],
    *,
    weight: int,
    stopwords: set[str],
) -> None:
    """Add credit to a token, preserving first-seen capitalization
    and skipping stopword hits.

    Tokens are stored in the counter keyed by lowercased form so
    "MDM" and "mdm" collapse cleanly; display capitalization is
    decided by first sighting (the file's canonical form).

    Stopword check is case-insensitive: extract_topics lowercases
    the stopword set on entry, and we lower the candidate before
    comparing so "Alice" matches a "alice" stopword and vice
    versa.
    """
    token = raw_token.strip()
    if not token:
        return
    if token.lower() in stopwords:
        return
    # Defensive: drop trailing punctuation a regex might have left
    # behind (rare with the configured patterns but cheap to guard).
    token = token.rstrip(".,;:!?")
    if len(token) < 2:
        return
    if token.lower() in stopwords:
        return
    key = token.lower()
    scores[key] += weight
    display.setdefault(key, token)
