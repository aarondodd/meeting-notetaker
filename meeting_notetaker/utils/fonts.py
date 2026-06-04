"""Font resolution helpers for the editor + preview surfaces.

Centralizes the logic that turns UiConfig's user-facing strings
(editor_font_family, preview_font_family + sizes) into actual
``QFont`` instances. Two callers today: the SessionView (My Notes
editor + transcript view + the preview panes on Synthesis / Live
Notes / Previous Notes) and the notes popout window (#80
followup).

Defaults:
    Editor: Consolas with Monospace style hint; falls back to
            whatever the OS provides as fixed-pitch. The style hint
            survives a missing-family lookup so the editor stays
            monospace even on a host without Consolas installed.
    Preview: whatever the application default font is. PyQt6 keeps
             a single QApplication default font, so an empty
             family means "use that". Resolving via the application
             default avoids hard-coding a sans-serif name that
             might not be installed on every platform.

A size of 0 means "use the QFont's natural default after the
family is set" -- we do not override pointSize. Existing installs
that never opened the Fonts section therefore see no visible
change on first launch of v0.7.7.
"""
from __future__ import annotations

from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication


# Picked because it's the de-facto Windows monospace and is also
# present on most macOS / Linux setups via OS fonts or the Cascadia
# package. The style hint guarantees the OS picks SOMETHING
# monospace even when the family lookup misses.
_DEFAULT_EDITOR_FAMILY = "Consolas"


def resolve_editor_font(family: str, size: int) -> QFont:
    """Return a QFont for the My Notes / Markdown editor surfaces.

    ``family`` empty -> auto-default. ``size`` 0 -> don't set
    pointSize (keep the QFont's natural default for the resolved
    family). Always sets the Monospace style hint so the editor
    renders as monospace even when the requested family isn't
    installed.
    """
    name = family.strip() or _DEFAULT_EDITOR_FAMILY
    font = QFont(name)
    font.setStyleHint(QFont.StyleHint.Monospace)
    # setFixedPitch(True) is a stronger hint than the StyleHint alone
    # -- it tells Qt to reject proportional substitutes during family
    # fallback. Belt-and-braces against the monospace regression that
    # prompted this whole section (#80 followup).
    font.setFixedPitch(True)
    if size > 0:
        font.setPointSize(size)
    return font


def resolve_preview_font(family: str, size: int) -> QFont:
    """Return a QFont for the Markdown preview surfaces.

    ``family`` empty -> QApplication default font's family. ``size``
    0 -> don't override pointSize. No style hint -- the preview
    is mixed content (headings + body + tables + code blocks) and
    each Markdown block carries its own intrinsic styling, so we
    only seed the base.
    """
    if family.strip():
        font = QFont(family)
    else:
        # Use the application-level default. Reading + cloning it
        # avoids the platform-name guessing game ("which sans is
        # installed?") and respects whatever Qt's host plugin
        # picked at startup (e.g. Segoe UI on Windows).
        app = QApplication.instance()
        font = QFont(app.font()) if app is not None else QFont()
    if size > 0:
        font.setPointSize(size)
    return font
