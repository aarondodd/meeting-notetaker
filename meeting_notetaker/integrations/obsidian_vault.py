"""Obsidian vault discovery + config parsing (issue #96).

Pure-Python. No PyQt, no Obsidian dependency. Walks the OS-level
``obsidian.json`` registry that Obsidian writes when it opens a
vault, plus each vault's own ``.obsidian/`` config files, so the
caller can:

  * list registered vaults to seed the Settings vault picker;
  * map a configured vault path to the name Obsidian uses in
    ``obsidian://`` URIs (the URI needs the vault name, not the
    path);
  * honor the user's ``attachmentFolderPath`` setting on the rare
    occasion we need a path that Obsidian itself would compute
    (the namespaced workspace folder strategy avoids this in the
    normal case, but the daily-note backlink path does need it);
  * find the user's Daily Notes folder + filename template for the
    optional daily-note backlink feature.

All file reads are best-effort. A missing / malformed file returns
``None`` or an empty default; nothing here raises.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import date as _date
from pathlib import Path
from typing import Optional


# ---- OS-level obsidian.json (the vault registry) -------------------------


def obsidian_user_config_dir() -> Optional[Path]:
    """Per-user directory where Obsidian writes ``obsidian.json``.

    Windows: ``%APPDATA%/obsidian/``.
    macOS:   ``~/Library/Application Support/obsidian/``.
    Linux:   ``$XDG_CONFIG_HOME/obsidian/`` (default ``~/.config/obsidian/``).

    Returns None on unrecognized platforms; everything downstream
    treats a missing registry as "Obsidian is not installed."
    """
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return None
        return Path(appdata) / "obsidian"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "obsidian"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "obsidian"


@dataclass
class RegisteredVault:
    name: str
    path: Path
    open_in_obsidian: bool = False


def list_registered_vaults(
    obsidian_dir: Optional[Path] = None,
) -> list[RegisteredVault]:
    """Return every vault Obsidian knows about, newest first.

    Reads ``<obsidian_dir>/obsidian.json``. The file's ``vaults``
    object is keyed by vault id; each entry carries ``path`` and
    ``ts`` (last-opened timestamp). The vault "name" Obsidian
    displays + accepts in URIs is the path's basename; the JSON
    doesn't store it separately. Order: most-recently-opened first
    so the Settings picker can default to the obvious choice.
    """
    obsidian_dir = obsidian_dir or obsidian_user_config_dir()
    if obsidian_dir is None:
        return []
    registry = obsidian_dir / "obsidian.json"
    if not registry.is_file():
        return []
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    raw = data.get("vaults") or {}
    items: list[tuple[int, RegisteredVault]] = []
    for entry in raw.values():
        path_str = (entry or {}).get("path") or ""
        if not path_str:
            continue
        ts = int((entry or {}).get("ts") or 0)
        path = Path(path_str)
        items.append((ts, RegisteredVault(
            name=path.name,
            path=path,
            open_in_obsidian=bool((entry or {}).get("open", False)),
        )))
    items.sort(key=lambda pair: pair[0], reverse=True)
    return [v for _, v in items]


def vault_name_for_path(
    vault_root: Path,
    obsidian_dir: Optional[Path] = None,
) -> str:
    """Best-effort lookup of the name Obsidian uses for ``vault_root``.

    Prefer the registry entry (case-correct, matches the URI Obsidian
    accepts). Fall back to the directory's basename when the vault is
    not registered yet -- ``obsidian://open`` still works for paths
    registered later, and the picker surfaces the not-registered case.
    """
    try:
        wanted = vault_root.resolve()
    except OSError:
        wanted = vault_root
    for vault in list_registered_vaults(obsidian_dir):
        try:
            resolved = vault.path.resolve()
        except OSError:
            resolved = vault.path
        if resolved == wanted:
            return vault.name
    return vault_root.name


def is_vault_registered(
    vault_root: Path,
    obsidian_dir: Optional[Path] = None,
) -> bool:
    try:
        wanted = vault_root.resolve()
    except OSError:
        wanted = vault_root
    for vault in list_registered_vaults(obsidian_dir):
        try:
            resolved = vault.path.resolve()
        except OSError:
            resolved = vault.path
        if resolved == wanted:
            return True
    return False


# ---- per-vault config (.obsidian/app.json + daily-notes.json) -----------


def vault_is_valid(vault_root: Path) -> bool:
    """A vault is any directory; Obsidian creates ``.obsidian/`` on
    first open. Treat presence of the directory itself as the bar
    (so we can write into a vault Obsidian has not yet opened) but
    require it to be readable + writable."""
    if not vault_root.is_dir():
        return False
    if not os.access(vault_root, os.R_OK | os.W_OK):
        return False
    return True


def read_attachment_folder_path(vault_root: Path) -> Optional[str]:
    """Return the user's configured ``attachmentFolderPath`` or None.

    Obsidian stores this in ``<vault>/.obsidian/app.json``. The
    value is vault-relative. The default Obsidian uses when unset
    is the vault root itself ("./"), which the caller should treat
    as None ("no preference, use the namespaced workspace folder").
    """
    cfg = vault_root / ".obsidian" / "app.json"
    if not cfg.is_file():
        return None
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    raw = data.get("attachmentFolderPath")
    if not raw or raw in ("./", "/"):
        return None
    return str(raw).strip().strip("/")


@dataclass
class DailyNotesConfig:
    """Where the user's Daily Notes live, if they use them.

    Both the bundled Daily Notes core plugin and the third-party
    Periodic Notes community plugin use the same field shape; we
    read whichever file exists.
    """
    folder: str  # vault-relative, "" = vault root
    filename_format: str  # moment.js format string (default "YYYY-MM-DD")


def read_daily_notes_config(vault_root: Path) -> Optional[DailyNotesConfig]:
    """Return the Daily Notes folder + format, or None if not set.

    Search order: ``.obsidian/daily-notes.json`` (core plugin) ->
    ``.obsidian/plugins/periodic-notes/data.json`` (community).
    Missing or malformed files yield None.
    """
    core = vault_root / ".obsidian" / "daily-notes.json"
    if core.is_file():
        try:
            data = json.loads(core.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            folder = str(data.get("folder") or "").strip().strip("/")
            fmt = str(data.get("format") or "").strip() or "YYYY-MM-DD"
            return DailyNotesConfig(folder=folder, filename_format=fmt)
    periodic = (
        vault_root / ".obsidian" / "plugins" / "periodic-notes" / "data.json"
    )
    if periodic.is_file():
        try:
            data = json.loads(periodic.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            daily = (data.get("daily") or {})
            if daily.get("enabled"):
                folder = str(daily.get("folder") or "").strip().strip("/")
                fmt = str(daily.get("format") or "").strip() or "YYYY-MM-DD"
                return DailyNotesConfig(folder=folder, filename_format=fmt)
    return None


def daily_note_path_for(
    vault_root: Path,
    daily: DailyNotesConfig,
    when: _date,
) -> Path:
    """Resolve today's daily-note absolute path.

    Only the moment.js tokens Obsidian actually documents for daily
    notes are supported (YYYY, YY, MM, M, MMMM, MMM, DD, D, ddd,
    dddd). Anything else is left literal. This is intentionally
    narrow -- daily-note backlink is a polish feature and we don't
    want to ship a full moment.js translator.
    """
    rendered = render_moment_format(daily.filename_format, when)
    parts = [vault_root]
    if daily.folder:
        parts.append(Path(daily.folder))
    return Path(*parts) / f"{rendered}.md"


_MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def render_moment_format(fmt: str, when: _date) -> str:
    """Translate the subset of moment.js tokens daily notes use."""
    out: list[str] = []
    i = 0
    while i < len(fmt):
        for token, value in (
            ("YYYY", f"{when.year:04d}"),
            ("YY", f"{when.year % 100:02d}"),
            ("MMMM", _MONTH_NAMES[when.month - 1]),
            ("MMM", _MONTH_NAMES[when.month - 1][:3]),
            ("MM", f"{when.month:02d}"),
            ("M", str(when.month)),
            ("DD", f"{when.day:02d}"),
            ("D", str(when.day)),
            ("dddd", when.strftime("%A")),
            ("ddd", when.strftime("%a")),
        ):
            if fmt.startswith(token, i):
                out.append(value)
                i += len(token)
                break
        else:
            out.append(fmt[i])
            i += 1
    return "".join(out)
