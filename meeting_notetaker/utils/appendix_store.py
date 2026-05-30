"""Sidecar persistence for the parsed LLM appendices (#64 followup).

Stores the four LLM-derived appendix payloads (Attendee Context,
Attendee Details, Suggested Topics, Referenced Attachments) in
``notes.appendices.json`` next to ``notes.md`` in the session
directory. The Appendix tray + preview transform + PDF render read
from this sidecar so the four sections stay populated even when
the user has the strip-on-save Settings toggle ON (which would
otherwise clear notes.md and leave the tray empty).

Refresh rules:

* On synthesis paste-back: parse the LLM output, write the
  sidecar, then optionally strip notes.md per the toggle. The
  sidecar holds the full parsed data regardless of strip state.
* On debounced synthesis edits: re-parse current notes.md and
  rewrite the sidecar so user-side edits flow through. A section
  the user manually deletes from notes.md disappears from the
  sidecar on the next save.
* On session load: the tray + preview transform call
  ``load_as_appendix_data`` -- it returns sidecar-backed entries
  when the file is present, and falls back to parsing the
  notes.md buffer for any sections the sidecar doesn't carry.

Links and session attachments are NOT persisted here -- they live
in notes.md and the AttachmentsStore respectively and are computed
fresh at render time.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from . import attendee_appendix as _attendee_appendix_mod
from . import attendee_context as _attendee_context_mod
from . import invite_mentions as _invite_mentions_mod
from . import topic_appendix as _topic_appendix_mod
from .attendee_appendix import AttendeeAppendixEntry, parse_appendix
from .attendee_context import (
    AttendeeContextEntry,
    parse_attendee_context,
)
from .invite_mentions import InviteMentionEntry, parse_invite_mentions
from .paths import session_dir
from .topic_appendix import parse_topic_appendix


log = logging.getLogger(__name__)


SIDECAR_FILENAME = "notes.appendices.json"


class AppendixStore:
    """File-backed cache for the four LLM-derived appendices.

    Construct with a ``session_id``; the sidecar lives at
    ``<session_dir>/notes.appendices.json``. All operations are
    best-effort -- a malformed / unreadable sidecar yields an empty
    payload rather than raising so the rest of the synthesis flow
    keeps working.
    """

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id

    @property
    def path(self) -> Path:
        return session_dir(self._session_id) / SIDECAR_FILENAME

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> dict:
        """Return the raw sidecar dict or ``{}`` when absent / bad.

        Schema:
        ``{
            "attendee_context": [{"name": str, "observation": str}, ...],
            "attendee_details": [{"name": str, "title": str, ...}, ...],
            "topics": [str, ...],
            "referenced_attachments": [{"name": str, "context": str}, ...]
        }``
        """
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.exception("appendix sidecar load failed: %s", self.path)
            return {}

    def save(
        self,
        *,
        attendee_context: list[AttendeeContextEntry],
        attendee_details: list[AttendeeAppendixEntry],
        topics: list[str],
        referenced_attachments: list[InviteMentionEntry],
    ) -> None:
        """Persist parsed appendix entries.

        Empty payload -> the sidecar is removed (cleaner than
        keeping a stale empty file lying around).
        """
        is_empty = not (
            attendee_context
            or attendee_details
            or topics
            or referenced_attachments
        )
        if is_empty:
            try:
                if self.path.exists():
                    self.path.unlink()
            except OSError:
                log.exception(
                    "appendix sidecar remove failed: %s", self.path,
                )
            return
        data = {
            "attendee_context": [asdict(e) for e in attendee_context],
            "attendee_details": [asdict(e) for e in attendee_details],
            "topics": list(topics),
            "referenced_attachments": [
                asdict(e) for e in referenced_attachments
            ],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            log.exception("appendix sidecar write failed: %s", self.path)

    def save_from_notes(self, notes_markdown: str) -> None:
        """Convenience: parse notes.md content + persist all four
        sections in a single call."""
        self.save(
            attendee_context=parse_attendee_context(notes_markdown),
            attendee_details=parse_appendix(notes_markdown),
            topics=parse_topic_appendix(notes_markdown),
            referenced_attachments=parse_invite_mentions(notes_markdown),
        )

    def load_as_dataclasses(self) -> tuple[
        list[AttendeeContextEntry],
        list[AttendeeAppendixEntry],
        list[str],
        list[InviteMentionEntry],
    ]:
        """Return the sidecar payload reconstructed as the dataclasses
        the rest of the appendix code expects."""
        raw = self.load()
        ctx = [
            AttendeeContextEntry(
                name=str(d.get("name", "") or ""),
                observation=str(d.get("observation", "") or ""),
            )
            for d in raw.get("attendee_context", [])
            if isinstance(d, dict) and (d.get("name") or "").strip()
        ]
        details = [
            AttendeeAppendixEntry(
                name=str(d.get("name", "") or ""),
                title=str(d.get("title", "") or ""),
                company=str(d.get("company", "") or ""),
                department=str(d.get("department", "") or ""),
                email=str(d.get("email", "") or ""),
                phone=str(d.get("phone", "") or ""),
            )
            for d in raw.get("attendee_details", [])
            if isinstance(d, dict) and (d.get("name") or "").strip()
        ]
        topics = [
            str(t).strip()
            for t in raw.get("topics", [])
            if isinstance(t, str) and str(t).strip()
        ]
        referenced = [
            InviteMentionEntry(
                name=str(d.get("name", "") or ""),
                context=str(d.get("context", "") or ""),
            )
            for d in raw.get("referenced_attachments", [])
            if isinstance(d, dict) and (d.get("name") or "").strip()
        ]
        return ctx, details, topics, referenced


def regenerate_notes_json(
    notes_markdown: str,
    *,
    attendee_context: list[AttendeeContextEntry],
    attendee_details: list[AttendeeAppendixEntry],
    topics: list[str],
    referenced_attachments: list[InviteMentionEntry],
) -> str:
    """Replace the raw LLM appendix JSON blocks in ``notes_markdown``
    with freshly-rendered blocks built from the supplied entries.

    Used by the AppendixEditDialog (#followup) so a user edit /
    delete in the dialog flows back into ``notes.md`` -- otherwise
    the next debounced ``_flush_notes`` would re-parse the
    untouched LLM blocks and stomp the sidecar.

    Sections with zero entries get no block (the heading disappears
    too). The four blocks land in a consistent order at the
    bottom of the document so a user-written ``## Appendix``
    further up is preserved.
    """
    body = notes_markdown or ""
    body = _attendee_context_mod.strip_appendix(body)
    body = _attendee_appendix_mod.strip_appendix(body)
    body = _topic_appendix_mod.strip_appendix(body)
    body = _invite_mentions_mod.strip_appendix(body)
    blocks: list[str] = []
    if attendee_context:
        blocks.append(_render_context_block(attendee_context))
    if attendee_details:
        blocks.append(_render_details_block(attendee_details))
    if topics:
        blocks.append(_render_topics_block(topics))
    if referenced_attachments:
        blocks.append(_render_referenced_block(referenced_attachments))
    if not blocks:
        return body.rstrip() + "\n"
    body = body.rstrip()
    if body:
        body = body + "\n\n"
    return body + "\n\n".join(blocks) + "\n"


def _render_context_block(entries) -> str:
    payload = [
        {"name": e.name, "observation": e.observation}
        for e in entries
    ]
    return _wrap_block("Attendee Context", payload)


def _render_details_block(entries) -> str:
    payload = []
    for e in entries:
        obj: dict = {"name": e.name}
        for k in ("title", "company", "department", "email", "phone"):
            v = getattr(e, k, "")
            if v:
                obj[k] = v
        payload.append(obj)
    return _wrap_block("Attendee Details", payload)


def _render_topics_block(topics) -> str:
    return _wrap_block("Suggested Topics", list(topics))


def _render_referenced_block(entries) -> str:
    payload = [
        {"name": e.name, "context": e.context}
        for e in entries
    ]
    return _wrap_block("Referenced Attachments", payload)


def _wrap_block(section: str, payload) -> str:
    pretty = json.dumps(payload, indent=2, ensure_ascii=False)
    return (
        f"## {section} (auto-extracted)\n\n"
        f"```json\n{pretty}\n```"
    )


def collect_for_session(
    *,
    session_id: Optional[str],
    notes_text: str = "",
    live_notes_text: str = "",
    session_attachments: Optional[list[str]] = None,
):
    """Build the full ``AppendixData`` for a session.

    Prefers the sidecar for the four LLM-derived sections; falls
    back to parsing ``notes_text`` for any section the sidecar
    doesn't carry. Links + session attachments are always computed
    fresh -- they don't live in the sidecar.

    Returns an ``AppendixData`` ready to hand to the tray, the
    preview transform, or the PDF render.
    """
    from . import link_extractor  # noqa: PLC0415
    from .appendix_transform import AppendixData  # noqa: PLC0415

    # Sidecar lookup -- silently degrades to fresh-parse if the
    # sidecar isn't there yet (e.g. session predates the migration
    # or strip ran before the first sidecar write).
    ctx = []
    details = []
    topics: list[str] = []
    referenced = []
    if session_id:
        try:
            store = AppendixStore(session_id)
            if store.exists():
                ctx, details, topics, referenced = store.load_as_dataclasses()
        except Exception:
            log.exception(
                "appendix sidecar collect failed: %s", session_id,
            )
    # Fallback: any section the sidecar didn't surface gets parsed
    # straight from the notes buffer. Lets sessions that haven't been
    # touched since the sidecar migration still populate the tray.
    if not ctx:
        ctx = parse_attendee_context(notes_text)
    if not details:
        details = parse_appendix(notes_text)
    if not topics:
        topics = parse_topic_appendix(notes_text)
    if not referenced:
        referenced = parse_invite_mentions(notes_text)
    links = link_extractor.extract_links(
        notes_text=notes_text or "",
        live_notes_text=live_notes_text or "",
    )
    return AppendixData(
        attendee_context=ctx,
        attendee_details=details,
        topics=topics,
        referenced_attachments=referenced,
        session_attachments=list(session_attachments or []),
        links=links,
    )
