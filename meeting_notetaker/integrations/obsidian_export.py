"""Publish finalized synthesis to a local Obsidian vault (issue #96).

Pure-Python. No PyQt, no Obsidian dependency. Filesystem-only:
write markdown + copy attachments into the configured vault. The
caller (UI layer) is responsible for choosing the vault path, the
session metadata, and the open-after-save behavior; this module
takes those inputs and produces the file on disk.

Per-Obsidian conventions:
  * No table of contents emitted; Obsidian's own outline view + the
    Outline plugin handle that responsibility.
  * Filename / folder forbidden character set is stricter than the
    generic Windows set (Obsidian also forbids ``[`` ``]`` ``#``
    ``^`` ``|`` ``:`` because they have meaning inside the
    wikilink + markdown grammars).
  * Image references that land inside the configured attachments
    folder use ``![[wikilink]]`` form (Obsidian's idiomatic embed);
    everything else uses standard markdown links so portability to
    non-Obsidian renderers is preserved.

Two outputs:
  * ``ExportResult`` -- success path; the caller opens the file or
    surfaces the path to the user.
  * ``RepublishCandidate`` -- the configured target folder already
    contains a note with a matching ``source_session_id``; the caller
    shows Overwrite / Save as new / Cancel and re-invokes.
"""
from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from .export import ExportAttachment, ExportResult


# ---- inputs --------------------------------------------------------------


@dataclass
class ObsidianSessionInfo:
    """The minimum metadata the publish needs from the session row.

    All fields are optional; the publisher emits what it has and
    omits what it doesn't. ``classification`` is only written to
    frontmatter when ``include_classification`` is also set on the
    config (per the issue #96 Q1 answer).
    """
    session_id: str
    title: str
    started_at: Optional[datetime] = None
    duration_minutes: Optional[int] = None
    attendees: list[str] = field(default_factory=list)
    series: str = ""
    tags: list[str] = field(default_factory=list)
    classification: str = ""


@dataclass
class ObsidianPublishOptions:
    """The publish-time toggles the picker dialog resolves.

    These mirror ``ObsidianConfig`` fields but live as a separate
    dataclass so tests + the worker don't depend on the full Config
    class.
    """
    vault_root: Path
    vault_name: str
    target_subdir: str  # vault-relative; "" = vault root
    filename_stem: str  # no extension
    write_frontmatter: bool = True
    wikilink_attendees: bool = True
    wikilink_series: bool = True
    include_classification: bool = False
    include_attachments: bool = False
    daily_note_backlink: bool = False
    open_after_save: bool = True
    # Set by re-publish flow: "save_as_new" (default) or "overwrite".
    on_conflict: str = "save_as_new"


# ---- outputs -------------------------------------------------------------


@dataclass
class RepublishCandidate:
    """The configured folder already has a note for this session_id.

    The caller surfaces the user choice; on the next call,
    ``ObsidianPublishOptions.on_conflict`` carries the answer.
    """
    existing_path: Path
    existing_title: str
    existing_frontmatter: dict


# ---- filename + path sanitization ---------------------------------------


_OBSIDIAN_FORBIDDEN_RE = re.compile(r'[\\/:*?"<>|\[\]#^\r\n\t]+')
_COLLAPSE_SPACES_RE = re.compile(r"\s+")


def sanitize_obsidian_stem(text: str, *, fallback: str = "Untitled") -> str:
    """Filename stem safe for an Obsidian vault on Windows + macOS.

    Strips the Windows-illegal set AND Obsidian's additional set
    (``[ ] # ^``). Collapses runs of whitespace, trims, removes
    trailing dots (Windows-create gotcha), enforces a non-empty
    result via ``fallback``.
    """
    cleaned = _OBSIDIAN_FORBIDDEN_RE.sub(" ", text or "")
    cleaned = _COLLAPSE_SPACES_RE.sub(" ", cleaned).strip()
    cleaned = cleaned.rstrip(".").strip()
    return cleaned or fallback


def sanitize_obsidian_path_segment(text: str, *, fallback: str = "Untitled") -> str:
    """Like ``sanitize_obsidian_stem`` but tolerates ``/`` so callers
    can build multi-segment vault-relative paths in one shot."""
    pieces = [
        sanitize_obsidian_stem(p, fallback=fallback)
        for p in (text or "").replace("\\", "/").split("/")
        if p.strip()
    ]
    return "/".join(pieces) or fallback


_MAX_FILENAME_BYTES = 200  # Windows long-path is 260; leave room for parent + .md


def truncate_filename_stem(stem: str) -> str:
    """Hard-clamp a stem so the resulting ``.md`` path fits on Windows.

    Real-world meeting titles can carry the entire attendee list;
    the truncation guards against MAX_PATH (260) on Windows while
    preserving the leading characters that identify the meeting.
    """
    encoded = stem.encode("utf-8")
    if len(encoded) <= _MAX_FILENAME_BYTES:
        return stem
    return encoded[:_MAX_FILENAME_BYTES].decode("utf-8", errors="ignore").rstrip()


# ---- location template ---------------------------------------------------


_NAMED_LAYOUTS: dict[str, tuple[str, str]] = {
    "year_month":      ("Meetings/{YYYY}/{MM}",  "{title}"),
    "by_series":       ("Meetings/{series}",      "{title}"),
    "by_series_dated": ("Meetings/{series}",      "{YYYY}-{MM}-{DD} - {title}"),
    "flat":            ("Meetings",               "{title}"),
}


def resolve_location_layout(
    *,
    template_name: str,
    template_custom: str,
    session: ObsidianSessionInfo,
    when: Optional[datetime] = None,
) -> tuple[str, str]:
    """Return the (subdir, filename_stem) pair for a template.

    Named templates each pin both halves explicitly (see
    ``_NAMED_LAYOUTS``). For ``custom``: split the user's literal
    template at the last ``/`` if the final segment carries
    ``{title}`` -- it's then treated as the filename pattern, the
    rest as the subdir. A custom template with no ``{title}`` in
    the last segment is treated entirely as a subdir and the
    filename defaults to ``{title}``.

    Placeholders in both halves: ``{YYYY}``, ``{MM}``, ``{DD}``,
    ``{title}``, ``{series}``, ``{session_id}``. Empty / unknown
    template_name falls back to ``year_month``.
    """
    moment = when or session.started_at or datetime.now().astimezone()
    placeholders = {
        "YYYY": f"{moment.year:04d}",
        "MM": f"{moment.month:02d}",
        "DD": f"{moment.day:02d}",
        "title": sanitize_obsidian_path_segment(session.title),
        "series": sanitize_obsidian_path_segment(session.series or "Untitled"),
        "session_id": session.session_id,
    }
    if template_name == "custom":
        layout = (template_custom or "Meetings/{YYYY}/{MM}").replace("\\", "/")
        parts = layout.split("/")
        if parts and "{title}" in parts[-1]:
            subdir_template = "/".join(parts[:-1]) or "Meetings"
            filename_template = parts[-1]
        else:
            subdir_template = layout
            filename_template = "{title}"
    else:
        subdir_template, filename_template = _NAMED_LAYOUTS.get(
            template_name, _NAMED_LAYOUTS["year_month"],
        )
    subdir = _fill_placeholders(subdir_template, placeholders)
    filename = _fill_placeholders_inline(filename_template, placeholders)
    return subdir, filename


def resolve_location_template(
    *,
    template_name: str,
    template_custom: str,
    session: ObsidianSessionInfo,
    when: Optional[datetime] = None,
) -> str:
    """Subdir-only convenience wrapper. The picker pre-populates the
    filename field separately via ``resolve_filename_template``."""
    subdir, _filename = resolve_location_layout(
        template_name=template_name,
        template_custom=template_custom,
        session=session,
        when=when,
    )
    return subdir


def resolve_filename_template(
    *,
    template_name: str,
    template_custom: str,
    session: ObsidianSessionInfo,
    when: Optional[datetime] = None,
) -> str:
    """Filename-stem convenience wrapper. The picker uses this to
    pre-populate the filename field; the user can override."""
    _subdir, filename = resolve_location_layout(
        template_name=template_name,
        template_custom=template_custom,
        session=session,
        when=when,
    )
    return filename


def _fill_placeholders(layout: str, placeholders: dict) -> str:
    out = _fill_placeholders_inline(layout, placeholders)
    # Strip empty segments produced by an unset placeholder so we
    # don't write a path with a doubled "/" or a trailing slash.
    parts = [p for p in out.replace("\\", "/").split("/") if p]
    return "/".join(parts)


def _fill_placeholders_inline(layout: str, placeholders: dict) -> str:
    """Substitute placeholders without splitting on path separators
    -- used for filename stems where ``/`` would be a sanitization
    target, not a real boundary."""
    out = layout
    for key, value in placeholders.items():
        out = out.replace("{" + key + "}", value)
    return out


# ---- frontmatter ---------------------------------------------------------


def build_frontmatter(
    session: ObsidianSessionInfo,
    options: ObsidianPublishOptions,
    *,
    when: Optional[datetime] = None,
) -> str:
    """Return the YAML frontmatter block (including leading + trailing ``---``).

    Output is deterministic + manually formatted. Avoids the PyYAML
    dependency (the project already chose mistune-based tooling
    over yaml libraries). Strings are minimally quoted so the
    written file matches what a human editor would produce.
    """
    if not options.write_frontmatter:
        return ""
    moment = when or session.started_at or datetime.now().astimezone()
    lines: list[str] = ["---"]
    lines.append(f"title: {_yaml_scalar(session.title)}")
    lines.append(f"date: {moment.strftime('%Y-%m-%d')}")
    lines.append(f"time: {moment.strftime('%H:%M')}")
    if session.duration_minutes:
        lines.append(f"duration_minutes: {int(session.duration_minutes)}")
    if session.attendees:
        lines.append("attendees:")
        for raw in session.attendees:
            name = (raw or "").strip()
            if not name:
                continue
            if options.wikilink_attendees:
                lines.append(f'  - "[[{_yaml_wikilink_target(name)}]]"')
            else:
                lines.append(f"  - {_yaml_scalar(name)}")
    if session.series:
        target = session.series.strip()
        if options.wikilink_series and target:
            lines.append(f'series: "[[{_yaml_wikilink_target(target)}]]"')
        elif target:
            lines.append(f"series: {_yaml_scalar(target)}")
    if options.include_classification and session.classification:
        lines.append(f"classification: {_yaml_scalar(session.classification)}")
    if session.tags:
        cleaned_tags = [_yaml_tag_token(t) for t in session.tags if t and t.strip()]
        if cleaned_tags:
            lines.append("tags: [" + ", ".join(cleaned_tags) + "]")
    lines.append("source_app: meeting-notetaker")
    lines.append(f"source_session_id: {session.session_id}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


_PLAIN_YAML_SCALAR_RE = re.compile(r"^[A-Za-z0-9 _\-+.@,'/]+$")


def _yaml_scalar(value: str) -> str:
    """Quote a scalar only when it carries chars YAML would parse
    specially. Bare strings are easier to read in the rendered note."""
    text = (value or "").strip()
    if not text:
        return '""'
    if text.startswith(("-", "?", ":", "!", "&", "*", "#", "%", "@", "`", "[", "{", '"', "'")):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if not _PLAIN_YAML_SCALAR_RE.match(text):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


_WIKILINK_FORBIDDEN_RE = re.compile(r'[\[\]|#^]')


def _yaml_wikilink_target(name: str) -> str:
    """Strip the characters Obsidian forbids inside ``[[...]]``."""
    return _WIKILINK_FORBIDDEN_RE.sub("", name).strip()


_TAG_FORBIDDEN_RE = re.compile(r"[ \t,\"'\[\]]+")


def _yaml_tag_token(tag: str) -> str:
    """Tags appear inline in a ``[a, b, c]`` flow sequence and must
    not embed brackets / commas / whitespace."""
    cleaned = _TAG_FORBIDDEN_RE.sub("-", tag.strip()).strip("-")
    return cleaned or "untagged"


# ---- re-publish detection -----------------------------------------------


_FRONTMATTER_RE = re.compile(
    r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL,
)
_FRONTMATTER_KEY_RE = re.compile(r"^([A-Za-z0-9_]+):\s*(.*)$")


def read_existing_frontmatter(path: Path) -> Optional[dict]:
    """Parse the leading YAML block of ``path`` into a dict.

    Recognizes simple ``key: value`` lines + ``key: [a, b, c]``
    inline sequences + ``key:\\n  - value`` block sequences. Enough
    for our re-publish check (``source_session_id`` + ``title``);
    not a full YAML implementation.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None
    block = match.group(1)
    out: dict[str, Any] = {}
    current_key: Optional[str] = None
    current_list: list[str] = []
    for raw_line in block.splitlines():
        if raw_line.startswith("  - ") and current_key:
            current_list.append(_strip_yaml_scalar(raw_line[4:]))
            continue
        if current_key:
            out[current_key] = current_list
            current_key = None
            current_list = []
        m = _FRONTMATTER_KEY_RE.match(raw_line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if value == "":
            current_key = key
            current_list = []
            continue
        if value.startswith("[") and value.endswith("]"):
            inside = value[1:-1].strip()
            out[key] = [
                _strip_yaml_scalar(p) for p in inside.split(",") if p.strip()
            ] if inside else []
            continue
        out[key] = _strip_yaml_scalar(value)
    if current_key:
        out[current_key] = current_list
    return out


def _strip_yaml_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def find_republish_candidate(
    *,
    search_root: Path,
    session_id: str,
) -> Optional[RepublishCandidate]:
    """Walk ``search_root`` for a markdown file whose frontmatter's
    ``source_session_id`` matches. First match wins; ordering is not
    promised, but typical use writes a single file per session."""
    if not search_root.is_dir():
        return None
    for md_path in search_root.rglob("*.md"):
        fm = read_existing_frontmatter(md_path)
        if not fm:
            continue
        if fm.get("source_session_id") == session_id:
            return RepublishCandidate(
                existing_path=md_path,
                existing_title=str(fm.get("title") or md_path.stem),
                existing_frontmatter=fm,
            )
    return None


# ---- image refs ----------------------------------------------------------


_IMAGE_REF_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def collect_local_image_refs(body: str) -> list[tuple[str, str]]:
    """Return every ``![alt](path)`` where path looks local.

    Local: relative path, ``file://`` URL, or no scheme. Remote
    (``http``, ``https``, ``data:``) is left alone; Obsidian
    renders inline ``http(s)`` and ``data:`` images natively.
    """
    out: list[tuple[str, str]] = []
    for match in _IMAGE_REF_RE.finditer(body or ""):
        alt = match.group(1)
        url = match.group(2).strip()
        if not url:
            continue
        parsed = urlparse(url)
        if parsed.scheme in ("http", "https", "data"):
            continue
        out.append((alt, url))
    return out


def resolve_local_image_path(url: str, *, base_dir: Path) -> Optional[Path]:
    parsed = urlparse(url)
    if parsed.scheme == "file":
        candidate = Path(parsed.path)
    else:
        candidate = (base_dir / url).resolve()
    if candidate.is_file():
        return candidate
    return None


def _content_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def copy_image_dedupe(
    *,
    src: Path,
    dest_dir: Path,
) -> Path:
    """Copy ``src`` into ``dest_dir`` with hash-based dedup.

    If a file with the same name already exists and matches by
    SHA-256: link to the existing one (no copy). If a file with the
    same name exists but differs: suffix the new one (``foo.png``
    -> ``foo-2.png``). Otherwise straight copy.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / src.name
    if not target.exists():
        shutil.copy2(src, target)
        return target
    if _content_hash(src) == _content_hash(target):
        return target
    stem, suffix = target.stem, target.suffix
    i = 2
    while True:
        candidate = dest_dir / f"{stem}-{i}{suffix}"
        if not candidate.exists():
            shutil.copy2(src, candidate)
            return candidate
        if _content_hash(src) == _content_hash(candidate):
            return candidate
        i += 1


# ---- markdown body rewriting --------------------------------------------


def rewrite_image_refs(
    body: str,
    *,
    rewrites: dict[str, str],
) -> str:
    """Replace local image URLs in ``body`` according to ``rewrites``.

    Keys are the original markdown URL (case-sensitive, as found in
    source). Values are the new vault-relative path (POSIX
    separators -- Obsidian normalizes on render).
    """
    if not rewrites:
        return body or ""

    def replace(m: re.Match) -> str:
        alt = m.group(1)
        url = m.group(2).strip()
        new = rewrites.get(url)
        if new is None:
            return m.group(0)
        return f"![{alt}]({new})"

    return _IMAGE_REF_RE.sub(replace, body or "")


# ---- collision-safe file emit -------------------------------------------


def unique_note_path(target: Path) -> Path:
    """Counter-suffix when ``target`` exists.

    Mirrors ``utils.export.unique_export_path`` shape but lives here
    so we don't pull a Qt-tainted module path into the Obsidian
    integration tests.
    """
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    parent = target.parent
    i = 2
    while True:
        candidate = parent / f"{stem}-{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


# ---- main entry ----------------------------------------------------------


def export_to_obsidian(
    *,
    session: ObsidianSessionInfo,
    body: str,
    options: ObsidianPublishOptions,
    session_dir: Path,
    attachments: Optional[list[ExportAttachment]] = None,
    progress: Optional[Any] = None,
    when: Optional[datetime] = None,
    location_template_name: str = "year_month",
    location_template_custom: str = "",
) -> ExportResult:
    """Publish a finalized synthesis to the configured vault.

    The caller is responsible for calling ``find_republish_candidate``
    + surfacing the conflict choice ahead of this call (the picker
    dialog does that). ``options.on_conflict`` is honored here:
    ``"overwrite"`` writes to ``existing_path`` (which the caller
    sets via ``filename_stem`` + ``target_subdir``); ``"save_as_new"``
    relies on ``unique_note_path`` to counter-suffix.
    """
    attachments = attachments or []
    vault_root = options.vault_root

    def report(msg: str) -> None:
        if progress is not None:
            try:
                progress(msg)
            except Exception:
                pass

    # 1. Resolve target directory + filename.
    sub = options.target_subdir or resolve_location_template(
        template_name=location_template_name,
        template_custom=location_template_custom,
        session=session,
        when=when,
    )
    target_dir = vault_root / sub if sub else vault_root
    target_dir.mkdir(parents=True, exist_ok=True)
    stem = truncate_filename_stem(
        sanitize_obsidian_stem(options.filename_stem, fallback=session.session_id),
    )
    target_path = target_dir / f"{stem}.md"
    if target_path.exists() and options.on_conflict != "overwrite":
        target_path = unique_note_path(target_path)

    # 2. Copy local images into Meetings/_assets/<session-id>/. Build a
    #    URL -> new-relative-path map for body rewrite.
    assets_root = vault_root / "Meetings" / "_assets" / session.session_id
    rewrites: dict[str, str] = {}
    refs = collect_local_image_refs(body)
    if refs:
        report(f"Copying {len(refs)} image(s)...")
    for _alt, url in refs:
        if url in rewrites:
            continue
        src = resolve_local_image_path(url, base_dir=session_dir)
        if src is None:
            continue
        copied = copy_image_dedupe(src=src, dest_dir=assets_root)
        rewrites[url] = _vault_relative_from(copied, target_dir)
    new_body = rewrite_image_refs(body, rewrites=rewrites)

    # 3. Copy session attachments (opt-in). Same dedup behavior.
    attachment_rewrites: list[tuple[str, Path]] = []
    if options.include_attachments and attachments:
        report(f"Copying {len(attachments)} attachment(s)...")
        for att in attachments:
            if not att.path.is_file():
                continue
            copied = copy_image_dedupe(src=att.path, dest_dir=assets_root)
            attachment_rewrites.append((att.label(), copied))

    # 4. Assemble file: frontmatter + body + optional Attachments tail.
    front = build_frontmatter(session, options, when=when)
    tail = _build_attachments_section(attachment_rewrites, target_dir) if attachment_rewrites else ""
    final = (front + new_body.rstrip() + ("\n\n" + tail if tail else "") + "\n").lstrip("\n")

    # 5. Write atomically: temp + rename so a crash mid-write doesn't
    #    leave a half-file inside the user's vault.
    report("Writing note...")
    _atomic_write_text(target_path, final)

    # 6. Optional daily-note backlink. Errors are logged via progress
    #    but never aborts the publish -- the note is already on disk.
    if options.daily_note_backlink:
        from . import obsidian_vault  # noqa: PLC0415
        daily = obsidian_vault.read_daily_notes_config(vault_root)
        if daily is not None:
            try:
                _append_daily_note_backlink(
                    vault_root=vault_root,
                    daily=daily,
                    note_path=target_path,
                    session=session,
                    when=when,
                )
            except OSError as exc:
                report(f"Skipped daily-note backlink: {exc}")

    rel = _posix_relpath(target_path, vault_root)
    open_uri = build_obsidian_open_uri(
        vault_name=options.vault_name,
        vault_root=vault_root,
        note_path=target_path,
    )
    return ExportResult(
        target="obsidian",
        page_id=session.session_id,
        page_url=open_uri,
        title=session.title or stem,
        message=(
            f"Saved to Obsidian vault as {rel}."
            if options.on_conflict != "overwrite"
            else f"Updated note in Obsidian vault at {rel}."
        ),
    )


def build_obsidian_open_uri(
    *,
    vault_name: str,
    vault_root: Path,
    note_path: Path,
) -> str:
    """Build the ``obsidian://open?...`` URI for a newly-written note.

    Prefers the vault-name + vault-relative-path form (the canonical
    Obsidian URI). Falls back to the absolute ``?path=...`` form when
    the caller didn't supply a vault name -- e.g. a vault that
    Obsidian has never opened.

    No file:// round-tripping: takes ``Path`` objects throughout so
    Windows path-with-drive-letter doesn't get mangled through
    urlparse's "/C:/..." shape.
    """
    from urllib.parse import quote  # noqa: PLC0415
    if vault_name:
        rel = _posix_relpath(note_path, vault_root)
        if rel.lower().endswith(".md"):
            rel = rel[:-3]
        return (
            f"obsidian://open?vault={quote(vault_name)}"
            f"&file={quote(rel)}"
        )
    return f"obsidian://open?path={quote(str(note_path))}"


# ---- attachments tail ----------------------------------------------------


def _build_attachments_section(
    items: list[tuple[str, Path]],
    note_dir: Path,
) -> str:
    """Emit a ``## Attachments`` section linking each copied file.

    Uses ``[[wikilinks]]`` for the attachment list -- this is the
    one place where wikilinks read more naturally than markdown
    links, and Obsidian resolves them against the vault's attachment
    discovery rules (which find files in ``Meetings/_assets/...``
    by basename without us hard-coding the relative path).
    """
    lines = ["## Attachments", ""]
    for label, path in items:
        lines.append(f"- [[{path.name}|{label}]]")
    return "\n".join(lines)


# ---- daily-note append ---------------------------------------------------


def _append_daily_note_backlink(
    *,
    vault_root: Path,
    daily: Any,
    note_path: Path,
    session: ObsidianSessionInfo,
    when: Optional[datetime],
) -> None:
    from . import obsidian_vault  # noqa: PLC0415
    moment = when or session.started_at or datetime.now().astimezone()
    target = obsidian_vault.daily_note_path_for(
        vault_root, daily, moment.date(),
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    rel = _posix_relpath(note_path, vault_root)
    rel_no_ext = rel[:-3] if rel.lower().endswith(".md") else rel
    label = session.title or note_path.stem
    duration = f" -- {int(session.duration_minutes)} min" if session.duration_minutes else ""
    line = f"- [[{rel_no_ext}|{label}]]{duration}"
    existing = ""
    if target.is_file():
        try:
            existing = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            existing = ""
        if line in existing:
            return
    sep = "" if (not existing or existing.endswith("\n")) else "\n"
    target.write_text(existing + sep + line + "\n", encoding="utf-8")


# ---- helpers -------------------------------------------------------------


def _posix_relpath(target: Path, root: Path) -> str:
    """Vault-relative POSIX path -- Obsidian normalizes to forward
    slashes regardless of host OS."""
    try:
        rel = target.relative_to(root)
    except ValueError:
        return str(target).replace("\\", "/")
    return str(rel).replace("\\", "/")


def _vault_relative_from(target: Path, note_dir: Path) -> str:
    """Path expressed relative to ``note_dir`` (the note's folder).

    Used by image refs: the markdown link form is interpreted by
    Obsidian relative to the file that carries it. The asset folder
    sits at the vault root, so the link traverses up out of the
    meeting subfolder.
    """
    import os  # noqa: PLC0415
    return os.path.relpath(target, note_dir).replace("\\", "/")


def _atomic_write_text(path: Path, content: str) -> None:
    """Write via a tmp sibling + os.replace so a crash mid-write
    leaves the previous file (or nothing) but never a half-file."""
    import os  # noqa: PLC0415
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
