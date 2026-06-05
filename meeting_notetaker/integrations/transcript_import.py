"""Import a transcript from an external source into a session.

Used when the user attended a meeting they could not record locally
(off-mic call, hardware issue, joined late) but has the transcript from
elsewhere -- typically the Teams meeting transcript export, sometimes
copy-pasted out of the live captions panel. The import flow normalizes
the body, optionally lets the user remap speaker labels, and writes the
result to raw.transcript.md so the existing synthesis path lights up.

Pure Python, no Qt, no app state. The Qt-side dialog
(`ui/import_transcript_dialog.py`) drives it; MainApp glues the result
to the session's TranscriptStore and SessionStore flag.

Teams export shapes that show up in practice:

  .docx (official export):
    Started transcription
    May 14, 2026, 10:00 AM

    Jane Smith   0:00:01.234
    All right, let's get started.

    Aaron Dodd   0:00:08.117
    Sounds good.

    View original meeting

  Web-client copy/paste (live transcript pane):
    Jane Smith
    0:00:01
    All right, let's get started.
    Aaron Dodd
    0:00:08
    Sounds good.

  Caption-only copy/paste (no speakers, no timestamps):
    All right, let's get started.
    Sounds good.

The strip-Teams-chrome pass needs to handle all three so a single
"Strip Teams formatting" toggle in the dialog covers the common cases
without per-source picker UI.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable


class TranscriptImportError(Exception):
    """Raised when load_transcript_from_file cannot read the source.

    The dialog catches this and surfaces .reason verbatim to the user
    along with a suggested next step (paste the body instead of
    importing the file).
    """

    def __init__(self, reason: str, *, suggest_paste: bool = True) -> None:
        super().__init__(reason)
        self.reason = reason
        self.suggest_paste = suggest_paste


# Common boilerplate lines in Teams exports + web copy/paste. Matched
# case-insensitively against the stripped line. These survive both
# .docx exports and copy/paste because Teams puts them in the same
# DOM nodes as the transcript itself.
_TEAMS_BOILERPLATE_PATTERNS = [
    re.compile(r"^started transcription(?:\s|:)?", re.IGNORECASE),
    re.compile(r"^stopped transcription(?:\s|:)?", re.IGNORECASE),
    re.compile(r"^view original meeting\b", re.IGNORECASE),
    re.compile(r"^transcript$", re.IGNORECASE),
    re.compile(r"^download(?:\s|$)", re.IGNORECASE),
    re.compile(r"^translate$", re.IGNORECASE),
    re.compile(r"^pin$", re.IGNORECASE),
    re.compile(r"^copy$", re.IGNORECASE),
    re.compile(r"^reply$", re.IGNORECASE),
    re.compile(r"^more options$", re.IGNORECASE),
]

# A line that is just a timestamp -- web copy/paste form splits the
# speaker, the timestamp, and the body across three lines. We want to
# fold the timestamp back into the speaker label so downstream synthesis
# sees one coherent "Name: text" prefix.
_BARE_TIMESTAMP_RE = re.compile(
    r"^\s*\d{1,2}:\d{2}(?::\d{2}(?:\.\d{1,3})?)?\s*$"
)

# Same shape but anchored to the start of a line for the "Name 0:00:01"
# form (.docx export).
_TRAILING_TIMESTAMP_RE = re.compile(
    r"\s+\d{1,2}:\d{2}(?::\d{2}(?:\.\d{1,3})?)?\s*$"
)

# Date-only "May 14, 2026, 10:00 AM" lines that the .docx export
# inserts under the "Started transcription" banner. Be conservative:
# only drop lines that look exactly like a date+time, not lines that
# happen to mention a date.
_TEAMS_DATE_LINE_RE = re.compile(
    r"^\s*(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*"
    r"\s+\d{1,2},\s+\d{4}(?:,\s*\d{1,2}:\d{2}\s*(?:AM|PM)?)?\s*$",
    re.IGNORECASE,
)

# Heuristic for "Name:" prefix -- one or more capitalized words
# followed by a colon at the start of the line. The colon is the
# anchor; everything before it on that line is the label. We bound
# the label length to keep us from matching a runaway prose line that
# happens to contain a colon ("This was the rule:..."). The bound is
# generous: 60 chars covers double-barreled names + suffixes.
_SPEAKER_PREFIX_RE = re.compile(
    r"^([A-Z][A-Za-z0-9 .'\-]{0,58}[A-Za-z0-9.])\s*:\s*",
    re.MULTILINE,
)


@dataclass(frozen=True)
class SpeakerOccurrence:
    """A speaker label and its first appearance index in the source.

    First-appearance order is what we surface in the dialog so the
    list reads top-to-bottom the way the conversation does. Callers
    rebuild a mapping keyed by .name; .first_index is metadata.
    """
    name: str
    first_index: int


def load_transcript_from_file(path: Path) -> str:
    """Read a transcript from disk.

    Supported: .txt, .md (read as UTF-8 with a fallback to latin-1 if
    UTF-8 decoding fails), .docx (via python-docx if installed).
    Anything else raises TranscriptImportError so the dialog can
    surface a clear "paste the body instead" message.

    Note: this does NOT normalize -- normalization is a separate step
    so the dialog can re-run it when the user toggles the checkbox
    without re-reading the file.
    """
    if not path.exists():
        raise TranscriptImportError(
            f"File not found: {path}", suggest_paste=False,
        )
    suffix = path.suffix.lower()
    if suffix in (".txt", ".md", ""):
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return path.read_text(encoding="latin-1", errors="replace")
    if suffix == ".docx":
        try:
            from docx import Document  # type: ignore[import-untyped]  # noqa: PLC0415
        except ImportError as exc:
            raise TranscriptImportError(
                "Reading .docx files requires python-docx, which is "
                "not installed in this build. Open the file in Word, "
                "select all, copy, then use the 'Paste from clipboard' "
                "option.",
                suggest_paste=True,
            ) from exc
        try:
            doc = Document(str(path))
        except Exception as exc:  # noqa: BLE001
            raise TranscriptImportError(
                f"Could not open .docx: {exc}. Try 'Paste from clipboard' instead.",
                suggest_paste=True,
            ) from exc
        # python-docx yields paragraphs in document order. Preserve
        # blank paragraphs as blank lines so the post-normalize pass
        # sees the same line structure the file has visually.
        return "\n".join(p.text for p in doc.paragraphs)
    raise TranscriptImportError(
        f"Unsupported file type '{suffix}'. Supported: .txt, .md, .docx. "
        "If you have the transcript in another format, use 'Paste from clipboard'.",
        suggest_paste=True,
    )


def normalize_text(body: str, *, strip_teams_chrome: bool = True) -> str:
    """Normalize an imported transcript body.

    Runs are:
      1. Universal: collapse \r\n + \r to \n; strip trailing whitespace
         per line. Always runs.
      2. Teams chrome: drop boilerplate lines, fold bare timestamp
         lines into the previous non-blank line (web copy/paste form),
         strip trailing timestamps from speaker lines (.docx form),
         drop standalone date headers.
      3. Universal cleanup: collapse runs of 3+ blank lines to 1
         (single-blank paragraph breaks survive). Strip leading and
         trailing blanks. Append a trailing newline so concatenation
         downstream is well-behaved.
    """
    if not body:
        return ""

    # (1) Universal newline + trailing-whitespace normalization.
    text = body.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.rstrip() for ln in text.split("\n")]

    if strip_teams_chrome:
        lines = _strip_teams_chrome(lines)

    # (3) Cleanup.
    out: list[str] = []
    blank_streak = 0
    for ln in lines:
        if not ln.strip():
            blank_streak += 1
            if blank_streak <= 1:
                out.append("")
            continue
        blank_streak = 0
        out.append(ln)
    # Trim leading/trailing blanks.
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out) + ("\n" if out else "")


def _strip_teams_chrome(lines: list[str]) -> list[str]:
    """Drop Teams-export boilerplate + reassemble split speaker lines.

    Operates on a list of right-stripped lines. Returns a new list of
    the same shape (blank lines preserved for the cleanup pass to
    collapse downstream).
    """
    cleaned: list[str] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        stripped = ln.strip()

        # Drop boilerplate banners.
        if any(p.match(stripped) for p in _TEAMS_BOILERPLATE_PATTERNS):
            i += 1
            continue
        # Drop standalone Teams date headers.
        if _TEAMS_DATE_LINE_RE.match(stripped):
            i += 1
            continue

        # Web copy/paste form:
        #   <line N>     "Jane Smith"
        #   <line N+1>   "0:00:01"
        #   <line N+2>   "All right, let's get started."
        #
        # We detect this by looking at "is the next line a bare
        # timestamp, and is the line after that something to attach
        # it to?" If so, fold the timestamp into the previous line's
        # speaker prefix.
        if (
            i + 1 < len(lines)
            and _BARE_TIMESTAMP_RE.match(lines[i + 1].strip())
            and stripped
            and not _BARE_TIMESTAMP_RE.match(stripped)
            and not stripped.endswith(":")
        ):
            # Treat this line as the speaker label. Append a colon if
            # it doesn't already carry one, then drop the bare-timestamp
            # next line entirely.
            speaker = stripped
            if not speaker.endswith(":"):
                speaker += ":"
            cleaned.append(speaker)
            i += 2  # skip the timestamp line
            continue

        # .docx export form: "Jane Smith   0:00:01.234"
        # Strip the trailing timestamp; replace with a colon if the
        # remaining label doesn't already end with one.
        m = _TRAILING_TIMESTAMP_RE.search(ln)
        if m and not _BARE_TIMESTAMP_RE.match(stripped):
            head = ln[: m.start()].rstrip()
            if head and not _BARE_TIMESTAMP_RE.match(head):
                if not head.endswith(":"):
                    head += ":"
                cleaned.append(head)
                i += 1
                continue

        # Drop bare-timestamp lines that survived (no preceding label;
        # not useful as a transcript line).
        if _BARE_TIMESTAMP_RE.match(stripped):
            i += 1
            continue

        cleaned.append(ln)
        i += 1
    return cleaned


def detect_speakers(body: str) -> list[SpeakerOccurrence]:
    """Find unique "Name:"-prefixed speaker labels in body.

    Returns them in first-appearance order; the dialog renders the
    list that way so users see the speakers in conversation order.
    """
    seen: dict[str, SpeakerOccurrence] = {}
    for m in _SPEAKER_PREFIX_RE.finditer(body):
        name = m.group(1).strip()
        if name not in seen:
            seen[name] = SpeakerOccurrence(name=name, first_index=m.start())
    return sorted(seen.values(), key=lambda s: s.first_index)


def apply_speaker_map(body: str, mapping: dict[str, str]) -> str:
    """Rewrite "OldName:" prefixes to "NewName:" using mapping.

    Empty values in mapping mean "leave this label alone" -- the
    dialog uses that to surface untouched speakers in the list.

    Matching anchors on the "Name:" prefix at the start of a line so
    we don't rewrite a name that appears in body prose. Labels that
    aren't in the mapping pass through unchanged.
    """
    if not mapping:
        return body
    effective = {old: new for old, new in mapping.items() if new and new != old}
    if not effective:
        return body

    def _replace(m: "re.Match[str]") -> str:
        label = m.group(1).strip()
        new = effective.get(label, label)
        # Preserve any trailing whitespace after the colon by
        # re-attaching it from the original match.
        tail = m.group(0)[m.end(1) - m.start():]
        return f"{new}{tail}"

    return _SPEAKER_PREFIX_RE.sub(_replace, body)


def iter_speakers_with_counts(body: str) -> Iterable[tuple[str, int]]:
    """Yield (speaker_name, occurrence_count). Used by the dialog to
    show how many lines each speaker contributed -- helps the user
    decide which labels to remap (high-count = main participant)."""
    counts: dict[str, int] = {}
    for m in _SPEAKER_PREFIX_RE.finditer(body):
        name = m.group(1).strip()
        counts[name] = counts.get(name, 0) + 1
    yield from sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
