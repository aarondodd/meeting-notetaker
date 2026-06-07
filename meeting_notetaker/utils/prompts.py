"""Synthesis prompt templates.

User-editable templates live in %APPDATA%/MeetingNotetaker/prompts/. Bundled
templates ship in meeting_notetaker/resources/prompts/ and are copied into
the user directory on first run (user files always win after that).

Prompt editor support (#89, v0.7.9): the user can edit / create / duplicate /
delete templates via the in-app editor. Each save archives the prior body to
prompts/_archive/<name>/<timestamp>.md so the user can revert without
losing work. The archive cap is _ARCHIVE_MAX_PER_NAME (oldest dropped first
beyond that), bounded to keep the archive dir healthy.
"""
from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import live_notes as live_notes_mod
from .paths import prompts_dir, resource_path


# Archive cap (per prompt name). Beyond this, the oldest archive is
# dropped on each save. Sized so a heavy editor day (~50 saves) still
# leaves several days of revert history.
_ARCHIVE_MAX_PER_NAME = 100

# Archive subfolder. Hidden-by-convention "_archive" so the user's
# prompts/ folder stays uncluttered when they Open Prompts Folder.
_ARCHIVE_SUBDIR = "_archive"

# Template-name validation. ASCII letters/digits/dash/underscore only,
# 1-64 chars, no leading dot or underscore (reserves _archive + any
# future _-prefixed convention).
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class PromptError(ValueError):
    """Raised when a prompt operation can't proceed.

    Distinct from ValueError so the UI can catch it and surface
    user-readable messages without swallowing programming errors.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def validate_prompt_name(name: str) -> str:
    """Normalize + validate a prompt name. Returns the normalized form.

    Strips whitespace, refuses empty / overly-long / unsafe names.
    Allowed characters: A-Z, a-z, 0-9, hyphen, underscore. Must start
    with an alphanumeric (reserves underscore prefixes for internal
    conventions like the _archive subdir).
    """
    stripped = (name or "").strip()
    if not stripped:
        raise PromptError("Prompt name is required.")
    if not _NAME_RE.match(stripped):
        raise PromptError(
            "Prompt name must be 1-64 characters of letters, digits, "
            "dash, or underscore and start with a letter or digit."
        )
    return stripped


# SHA-256 of bundled prompt bodies shipped in prior versions, computed against
# line-ending-normalized content (CRLF and CR collapsed to LF). When the user
# has a file whose normalized body hashes to one of these values it is
# considered unmodified and gets refreshed to the current bundled body.
# Per-filename to avoid cross-template accidents. Append new hashes here
# whenever a bundled prompt body changes.
_PRIOR_BUNDLED_HASHES: dict[str, set[str]] = {
    # v0.1 -- pre-merge templates without {{live_notes}} / {{attendees}}.
    # v0.2 -- merged-synthesis templates without {{user_name}} support.
    # v0.4 -- final pre-diarization bodies (single "Them:" label scheme).
    # v0.5 -- diarization-aware body with the {{live_notes}} reference bug
    # (placeholder substituted at every textual mention, not just the
    # final section). Replaced post-v0.5 with literal "live notes" references.
    # Post-v0.5.1 -- bullet-led Notes spec rewritten as prose with stronger
    # directive + shape example; Output preamble made explicit per section.
    "default.md": {
        "ec67b04a5c86bb91e9dcd61e31455b1130bd123681c6f89ca9d66ecacd14bdf4",
        "dd281f15122bfdc4f0466c13576a7d45ecd8acc27e14d23092181ff470132309",
        "0004ea62bf061e95be43c27326a7fbb7d9bef3d44fc9de71251519918e3f7c98",
        "81ff640c2ea393feb8520d440b340802cb36347aca56f4646c35f4945fe2192e",
        "e98c35805be75f90c36212dd5852164c09ef49d930d28836d32a5077b6aae4bb",
        # v0.6.2 -- strengthened the image-preservation rule from a
        # single sentence to an explicit three-part contract (preserve
        # every one, byte-for-byte, placed contextually with end-of-
        # section fallback). The prior single-sentence form was being
        # under-followed by some LLMs, which dropped or mangled
        # `![alt](path)` references during synthesis.
        "086f4dd38e0bab29fbbdc4d3cc4a3bbf06719de195ea5d84edbfed05497dee1c",
        # v0.7.3 -- Aaron's conciseness pass: Speaker N caveat for
        # split identities, attendees-stay-bare-names rule with
        # commentary routed to Open Questions, Notes section
        # rewritten as concise paragraphs with key-point bullets
        # under each topic heading. Replaces the pure-prose Notes
        # spec from 0.6.2.
        "a634f536e575f07bff2998b1026524796a7cf07bedc5b2ec6185f6cd9be3afe4",
        # v0.7.3 (as-shipped) through v0.7.6 -- this is the body that
        # actually shipped from v0.7.3 onward. The hash above predates
        # the v0.7.3 ship and never matched a released bundled body, so
        # without this entry an Aaron-driven prompt update in v0.7.7
        # wouldn't auto-refresh existing installs.
        "a1da85a06ec2b5b29f64a17eeedfd48b16197dfe8e3c8568f1cf3b840c6098db",
    },
    "one-on-one.md": {
        "2a140d9fd7d1ae7687bfd5f7fba0d517b6f41135e467a98cdd13abd8e1ca87f5",
        "b756a457b03b3903f2eb2350fba6c93a8e6f20097355402388fb0e62a12829c9",
        "91cb2de98164f71722cf520f167f2882280d5329266ab9a80fb14f603bb25ed1",
    },
    "standup.md": {
        "d46aff489d5c60cdcc3fa95318a027b06da0125dcfda79aa650be4c3b18991e5",
        "f03f06ef0fb6ffd79ac6f8d5fee034159e3406cc5b7764312e56ec874ef8356d",
        "a2cc0a5d0279043db4d94a77aea79edd0c71c6baef687721aaa673cd71051519",
    },
}


def _normalized_digest(body: bytes) -> str:
    """SHA-256 of `body` with CRLF and CR newlines collapsed to LF.

    Git's autocrlf setting checks files out with CRLF on Windows, so a file
    that is byte-identical to the bundled source upstream hashes differently
    after checkout. Normalizing makes the upgrade-detection robust across
    platforms.
    """
    normalized = body.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(normalized).hexdigest()


@dataclass(frozen=True)
class PromptTemplate:
    name: str
    path: Path
    body: str

    @property
    def display_name(self) -> str:
        return self.name.replace("-", " ").replace("_", " ").title()


def _bundled_prompts_dir() -> Path:
    return resource_path("prompts")


def _system_prompts_dir() -> Path:
    """Issue #51 Phase 4: app-injected system prompts.

    Distinct from the user-managed templates in `prompts/`. These
    are appended to every render() output (when enabled) and aren't
    seeded to the user's prompts directory -- they're internal to
    the app and shouldn't be editable from the user's prompts UI.
    """
    return resource_path("system_prompts")


def _load_system_prompt_bodies() -> list[str]:
    """Return every system prompt body, sorted by filename.

    Sorted for determinism: the prompt assembly should be stable
    across runs so the cached behaviors (LLM seeing the same input
    twice -> same response shape) hold.
    """
    sysdir = _system_prompts_dir()
    if not sysdir.is_dir():
        return []
    bodies: list[str] = []
    for p in sorted(sysdir.glob("*.md")):
        try:
            bodies.append(p.read_text(encoding="utf-8"))
        except OSError:
            # Skip unreadable system prompts rather than failing the
            # whole synthesis. The miss surfaces as "no appendix
            # extracted" which is recoverable.
            continue
    return bodies


# Separator between the user's chosen template and any opt-in
# auxiliary requests appended via the system-prompts dir.
# Originally an HTML comment sentinel ("<!-- mn:system-prompts -->")
# but that caused Claude to flag the appended content as embedded
# prompt-injection -- the sentinel + the imperative tone read as
# spoofed system instructions inside a transcript paste. Rewritten
# 2026-05-29 as a plain blank-line separator; the appended content
# itself is now first-person user voice ("Also, please also do X")
# so Claude sees a coherent two-part user request instead of
# user-text-plus-something-else. See issue #51 thread + the
# attendee_details_appendix.md rewrite for the matching content fix.
_SYSTEM_PROMPT_SEPARATOR = "\n\n"
# Legacy alias retained for any external test that imports the old
# name. New code should reference _SYSTEM_PROMPT_SEPARATOR.
_SYSTEM_PROMPT_SENTINEL = _SYSTEM_PROMPT_SEPARATOR


def seed_user_prompts(user_dir: Path | None = None) -> int:
    """Copy bundled prompt templates into the user directory if missing or stale.

    A user file is considered "stale" if its body hash matches a prior shipped
    version listed in `_PRIOR_BUNDLED_HASHES`. Stale files are refreshed to the
    current bundled body so users who never customized their prompts pick up
    upstream improvements automatically. User-modified files are always
    preserved.

    Returns the number of templates copied or refreshed.
    """
    user_dir = user_dir or prompts_dir()
    bundled = _bundled_prompts_dir()
    if not bundled.exists():
        return 0
    written = 0
    for src in bundled.glob("*.md"):
        dst = user_dir / src.name
        if not dst.exists():
            shutil.copy(src, dst)
            written += 1
            continue
        prior_hashes = _PRIOR_BUNDLED_HASHES.get(src.name)
        if not prior_hashes:
            continue
        try:
            current = dst.read_bytes()
        except OSError:
            continue
        if _normalized_digest(current) in prior_hashes:
            shutil.copy(src, dst)
            written += 1
    return written


def list_templates(user_dir: Path | None = None) -> list[PromptTemplate]:
    seed_user_prompts(user_dir)
    user_dir = user_dir or prompts_dir()
    templates: list[PromptTemplate] = []
    for path in sorted(user_dir.glob("*.md")):
        try:
            body = path.read_text(encoding="utf-8")
        except OSError:
            continue
        templates.append(PromptTemplate(name=path.stem, path=path, body=body))
    return templates


def get_template(name: str, user_dir: Path | None = None) -> PromptTemplate | None:
    for tpl in list_templates(user_dir):
        if tpl.name == name:
            return tpl
    return None


def render(
    template: PromptTemplate | str,
    *,
    session_title: str,
    session_date: datetime | str,
    transcript: str,
    live_notes: str = "",
    attendees: list[str] | None = None,
    user_name: str = "",
    include_system_prompts: bool = True,
) -> str:
    """Substitute the prompt placeholders.

    Replaces {{session_title}}, {{date}}, {{transcript}}, {{live_notes}},
    {{attendees}}, and {{user_name}}. Unknown placeholders are left intact
    so users can include literal `{{whatever}}` text.

    If `attendees` is None, the attendee list is parsed from `live_notes`.
    The {{live_notes}} substitution is replaced with a clear "(none)"
    placeholder if the user has not added content beyond the seeded template.

    If `user_name` is provided, `[HH:MM:SS] Me: ` line prefixes in the
    transcript are rewritten to `[HH:MM:SS] <user_name>: ` before
    substitution, so the LLM sees the user's actual name and can attribute
    action items by name. The {{user_name}} placeholder expands to the
    same name (or "Me" when unset).
    """
    from ..models.transcript import DEFAULT_USER_LABEL, rewrite_user_label

    if isinstance(template, PromptTemplate):
        body = template.body
    else:
        body = template
    if isinstance(session_date, datetime):
        date_str = session_date.strftime("%Y-%m-%d %H:%M")
    else:
        date_str = str(session_date)
    if attendees is None:
        attendees = live_notes_mod.parse_attendees(live_notes)
    attendees_str = live_notes_mod.format_attendee_list(attendees)
    if live_notes_mod.has_user_content(live_notes):
        live_notes_str = live_notes.strip()
    else:
        live_notes_str = "(none -- user did not take live notes)"
    name = (user_name or "").strip()
    user_name_str = name or DEFAULT_USER_LABEL
    transcript_for_prompt = rewrite_user_label(transcript, name)
    rendered = (
        body
        .replace("{{session_title}}", session_title)
        .replace("{{date}}", date_str)
        .replace("{{transcript}}", transcript_for_prompt)
        .replace("{{live_notes}}", live_notes_str)
        .replace("{{attendees}}", attendees_str)
        .replace("{{user_name}}", user_name_str)
    )
    if include_system_prompts:
        system_bodies = _load_system_prompt_bodies()
        if system_bodies:
            rendered = (
                rendered.rstrip()
                + _SYSTEM_PROMPT_SEPARATOR
                + "\n\n".join(b.strip() for b in system_bodies)
                + "\n"
            )
    return rendered


# ---------------------------------------------------------------------------
# In-app prompt editor support (#89)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArchivedPrompt:
    """One archived snapshot of a prompt body."""

    name: str
    path: Path
    saved_at: datetime
    body: str

    @property
    def saved_at_display(self) -> str:
        return self.saved_at.strftime("%Y-%m-%d %H:%M:%S")


def _archive_dir_for(name: str, user_dir: Path | None = None) -> Path:
    user_dir = user_dir or prompts_dir()
    return user_dir / _ARCHIVE_SUBDIR / name


def _parse_archive_timestamp(stem: str) -> datetime | None:
    """Decode the YYYYMMDD-HHMMSS-ffffff stem back to a UTC datetime.

    Archive filenames carry microseconds so two saves within a wall-
    clock second still sort correctly (a same-second collision under
    second-only timestamps left newest-first ordering broken).
    """
    try:
        parts = stem.split("-")
        if len(parts) < 2:
            return None
        date_part = parts[0]
        time_part = parts[1]
        if len(date_part) != 8 or len(time_part) < 6:
            return None
        micros = 0
        if len(parts) >= 3 and parts[2].isdigit():
            micros = int(parts[2].ljust(6, "0")[:6])
        return datetime.strptime(
            date_part + time_part[:6], "%Y%m%d%H%M%S",
        ).replace(microsecond=micros, tzinfo=timezone.utc)
    except (IndexError, ValueError):
        return None


def list_archived_versions(
    name: str, user_dir: Path | None = None,
) -> list[ArchivedPrompt]:
    """Return all archived snapshots for `name`, newest first.

    Reads each archive's body so the UI can preview without re-opening
    files. Returns [] when no archive dir exists yet.
    """
    name = validate_prompt_name(name)
    archive_dir = _archive_dir_for(name, user_dir=user_dir)
    if not archive_dir.is_dir():
        return []
    items: list[ArchivedPrompt] = []
    for p in sorted(archive_dir.glob("*.md"), reverse=True):
        try:
            body = p.read_text(encoding="utf-8")
        except OSError:
            continue
        saved_at = _parse_archive_timestamp(p.stem)
        if saved_at is None:
            # Archive file without a recognizable timestamp -- include
            # it anyway, with mtime as the fallback timestamp, so the
            # user can still see + restore it.
            try:
                saved_at = datetime.fromtimestamp(
                    p.stat().st_mtime, tz=timezone.utc,
                )
            except OSError:
                continue
        items.append(ArchivedPrompt(
            name=name, path=p, saved_at=saved_at, body=body,
        ))
    return items


def _archive_existing_body(
    name: str, user_dir: Path | None = None,
) -> Path | None:
    """Move the current prompt body into the archive directory.

    Called from `save_prompt` before the new body is written, so the
    archive holds the immediately-prior version (not the version-before
    that). Returns the archive path, or None if no current body exists.

    The most-recently-saved archive's timestamp comes from "now" rather
    than the file's mtime so two saves within a single second still
    sort correctly (the -N counter handles same-second collisions).
    """
    user_dir = user_dir or prompts_dir()
    src = user_dir / f"{name}.md"
    if not src.exists():
        return None
    try:
        current = src.read_text(encoding="utf-8")
    except OSError:
        return None
    if not current.strip():
        return None
    archive_dir = _archive_dir_for(name, user_dir=user_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)
    # Microsecond resolution so same-second saves sort correctly.
    # Format: YYYYMMDD-HHMMSS-ffffff. _parse_archive_timestamp consumes
    # the same shape on the way out.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    dst = archive_dir / f"{stamp}.md"
    counter = 1
    while dst.exists():
        dst = archive_dir / f"{stamp}-{counter}.md"
        counter += 1
    dst.write_text(current, encoding="utf-8")
    # Prune oldest archives beyond the cap so the archive dir doesn't
    # grow unbounded under heavy editing.
    _prune_old_archives(archive_dir)
    return dst


def _prune_old_archives(archive_dir: Path) -> int:
    """Drop archive files beyond _ARCHIVE_MAX_PER_NAME, oldest first."""
    if not archive_dir.is_dir():
        return 0
    files = sorted(archive_dir.glob("*.md"), reverse=True)
    if len(files) <= _ARCHIVE_MAX_PER_NAME:
        return 0
    removed = 0
    for path in files[_ARCHIVE_MAX_PER_NAME:]:
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def save_prompt(
    name: str,
    body: str,
    *,
    user_dir: Path | None = None,
) -> Path:
    """Write `body` to the named prompt, archiving the prior body if any.

    Returns the path to the saved prompt. Raises PromptError on invalid
    name. An empty body is allowed (the user can deliberately blank a
    prompt and rewrite it from scratch); the archive still gets the
    prior non-empty body.

    The active prompts/ folder is touched ATOMICALLY (write to tmp +
    rename) so a crash mid-write can't corrupt the source-of-truth.
    """
    name = validate_prompt_name(name)
    user_dir = user_dir or prompts_dir()
    user_dir.mkdir(parents=True, exist_ok=True)
    _archive_existing_body(name, user_dir=user_dir)
    dst = user_dir / f"{name}.md"
    tmp = dst.with_suffix(".md.tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(dst)
    return dst


def create_prompt(
    name: str,
    *,
    body: str = "",
    user_dir: Path | None = None,
) -> Path:
    """Create a new prompt with the given body.

    Refuses to overwrite an existing prompt; the caller must pick a
    unique name. Body defaults to empty so the user can start from
    scratch in the editor. Returns the path to the new prompt.
    """
    name = validate_prompt_name(name)
    user_dir = user_dir or prompts_dir()
    user_dir.mkdir(parents=True, exist_ok=True)
    dst = user_dir / f"{name}.md"
    if dst.exists():
        raise PromptError(
            f"A prompt named {name!r} already exists. Pick a different name."
        )
    dst.write_text(body, encoding="utf-8")
    return dst


def duplicate_prompt(
    source_name: str,
    dest_name: str,
    *,
    user_dir: Path | None = None,
) -> Path:
    """Copy `source_name`'s current body into a new prompt `dest_name`.

    Wraps create_prompt so the destination is refused if it already
    exists. The archive of the source is NOT carried over -- the new
    prompt starts fresh.
    """
    source = get_template(source_name, user_dir=user_dir)
    if source is None:
        raise PromptError(f"Source prompt {source_name!r} not found.")
    return create_prompt(dest_name, body=source.body, user_dir=user_dir)


def restore_archived_version(
    name: str,
    archive_path: Path,
    *,
    user_dir: Path | None = None,
) -> Path:
    """Replace the active prompt with the contents of an archived version.

    The current body is archived first (same mechanism as save_prompt),
    so the operation is reversible -- the user can revert the revert.
    Raises PromptError if the archive isn't under this prompt's
    archive dir (protects against path-confused calls).
    """
    name = validate_prompt_name(name)
    archive_path = Path(archive_path)
    expected_dir = _archive_dir_for(name, user_dir=user_dir)
    if archive_path.parent != expected_dir:
        raise PromptError(
            f"Archive {archive_path} is not in {expected_dir}; "
            "cross-prompt restore is not allowed."
        )
    if not archive_path.exists():
        raise PromptError(f"Archived version not found: {archive_path}")
    body = archive_path.read_text(encoding="utf-8")
    return save_prompt(name, body, user_dir=user_dir)


def delete_prompt(
    name: str,
    *,
    user_dir: Path | None = None,
    archive_first: bool = True,
) -> bool:
    """Remove a prompt from the active prompts dir.

    `archive_first=True` (default) snapshots the current body into the
    archive before deletion so the user can recover it. Returns True
    if a prompt was deleted, False if the prompt didn't exist.

    Note: deleting a bundled prompt is allowed -- seed_user_prompts
    will re-create the user file from the bundled source on next
    list_templates call. The user can also choose to keep their custom
    body by saving over the re-seeded version. Deleting + re-seeding
    is the path to "factory reset this prompt."
    """
    name = validate_prompt_name(name)
    user_dir = user_dir or prompts_dir()
    target = user_dir / f"{name}.md"
    if not target.exists():
        return False
    if archive_first:
        _archive_existing_body(name, user_dir=user_dir)
    try:
        target.unlink()
    except OSError as exc:
        raise PromptError(f"Could not delete {target}: {exc}") from exc
    return True


def is_bundled_prompt(name: str) -> bool:
    """True if `name` ships as a bundled default.

    The UI uses this to show a small badge ("bundled") and to confirm
    before delete (a delete of a bundled prompt is harmless because
    seed_user_prompts re-creates it, but the user should know).
    """
    try:
        validated = validate_prompt_name(name)
    except PromptError:
        return False
    bundled = _bundled_prompts_dir()
    return (bundled / f"{validated}.md").exists()
