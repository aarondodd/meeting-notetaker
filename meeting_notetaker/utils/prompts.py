"""Synthesis prompt templates.

User-editable templates live in %APPDATA%/MeetingNotetaker/prompts/. Bundled
templates ship in meeting_notetaker/resources/prompts/ and are copied into
the user directory on first run (user files always win after that).
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .paths import prompts_dir, resource_path


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
    """Copy bundled prompt templates into the user directory if missing.

    Returns the number of templates copied.
    """
    user_dir = user_dir or prompts_dir()
    bundled = _bundled_prompts_dir()
    if not bundled.exists():
        return 0
    copied = 0
    for src in bundled.glob("*.md"):
        dst = user_dir / src.name
        if not dst.exists():
            shutil.copy(src, dst)
            copied += 1
    return copied


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
) -> str:
    """Substitute the three required placeholders.

    Replaces {{session_title}}, {{date}}, {{transcript}}. Unknown placeholders
    are left intact so users can include literal `{{whatever}}` text.
    """
    if isinstance(template, PromptTemplate):
        body = template.body
    else:
        body = template
    if isinstance(session_date, datetime):
        date_str = session_date.strftime("%Y-%m-%d %H:%M")
    else:
        date_str = str(session_date)
    return (
        body
        .replace("{{session_title}}", session_title)
        .replace("{{date}}", date_str)
        .replace("{{transcript}}", transcript)
    )
