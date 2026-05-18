"""Synthesis prompt templates.

User-editable templates live in %APPDATA%/MeetingNotetaker/prompts/. Bundled
templates ship in meeting_notetaker/resources/prompts/ and are copied into
the user directory on first run (user files always win after that).
"""
from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import live_notes as live_notes_mod
from .paths import prompts_dir, resource_path


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
    "default.md": {
        "ec67b04a5c86bb91e9dcd61e31455b1130bd123681c6f89ca9d66ecacd14bdf4",
        "dd281f15122bfdc4f0466c13576a7d45ecd8acc27e14d23092181ff470132309",
        "0004ea62bf061e95be43c27326a7fbb7d9bef3d44fc9de71251519918e3f7c98",
        "81ff640c2ea393feb8520d440b340802cb36347aca56f4646c35f4945fe2192e",
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
    return (
        body
        .replace("{{session_title}}", session_title)
        .replace("{{date}}", date_str)
        .replace("{{transcript}}", transcript_for_prompt)
        .replace("{{live_notes}}", live_notes_str)
        .replace("{{attendees}}", attendees_str)
        .replace("{{user_name}}", user_name_str)
    )
