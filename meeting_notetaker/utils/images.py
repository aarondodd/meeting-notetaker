"""Image save helpers for live notes.

Images pasted from the clipboard or inserted from disk are written under
the session's `images/` subdir and referenced from live_notes.md (or
notes.md) via standard Markdown image syntax:

    ![alt text](images/pasted-20260517-1430.png "optional caption")

The relative path keeps notes portable -- moving or copying a session
folder preserves the references.

QImage save is performed via a lazy PyQt6 import so this module imports
on hosts that have no Qt available (the unit test environment).
"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional


SUPPORTED_INSERT_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
IMAGES_SUBDIR = "images"


def images_subdir(session_dir: Path) -> Path:
    """Return the session's images/ subfolder, creating it on first use."""
    path = session_dir / IMAGES_SUBDIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def generate_pasted_filename(
    *, now: Optional[datetime] = None, ext: str = "png"
) -> str:
    """Filename for clipboard-pasted images: pasted-YYYYMMDD-HHMMSS.<ext>."""
    when = now or datetime.now()
    stamp = when.strftime("%Y%m%d-%H%M%S")
    cleaned = ext.lstrip(".").lower() or "png"
    return f"pasted-{stamp}.{cleaned}"


def unique_path(target: Path) -> Path:
    """Append -2, -3, ... to the stem if target already exists."""
    if not target.exists():
        return target
    counter = 2
    while True:
        candidate = target.with_name(f"{target.stem}-{counter}{target.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def copy_image_file(src: Path, images_dir: Path) -> Path:
    """Copy an external image file into images_dir. Returns the saved path.

    The source filename is preserved (with -2/-3/... appended on collision).
    Caller is responsible for validating src.exists() and the extension.
    """
    images_dir.mkdir(parents=True, exist_ok=True)
    target = unique_path(images_dir / src.name)
    shutil.copy2(src, target)
    return target


def markdown_image_ref(rel_path: str, alt: str, caption: str = "") -> str:
    """Build a Markdown image reference.

    Format:
        ![alt](rel_path "caption")   # when caption is non-empty
        ![alt](rel_path)             # otherwise

    `rel_path` is used verbatim except for a `)` -> `%29` escape so the
    Markdown link grammar doesn't break on filenames containing `)`.
    The alt text has any `]` stripped (the literal Markdown closing
    bracket); the caption has `"` escaped.
    """
    safe_alt = (alt or "").replace("]", "")
    safe_path = rel_path.replace(")", "%29")
    if caption:
        safe_caption = caption.replace('"', '\\"')
        return f'![{safe_alt}]({safe_path} "{safe_caption}")'
    return f"![{safe_alt}]({safe_path})"


def save_qimage(image, images_dir: Path, *, filename: Optional[str] = None) -> Path:
    """Persist a QImage to images_dir as PNG. Returns the saved path.

    Lazy-imports PyQt6 so this module remains importable on test hosts
    that may have no GUI stack installed.
    """
    images_dir.mkdir(parents=True, exist_ok=True)
    target_name = filename or generate_pasted_filename()
    target = unique_path(images_dir / target_name)
    ok = image.save(str(target), "PNG")
    if not ok:
        raise OSError(f"QImage.save failed for {target}")
    return target


def join_relative(images_dir_name: str, filename: str) -> str:
    """Build the relative Markdown path from the saved filename.

    Always uses forward slashes so the path stays portable across Windows
    and POSIX (Markdown viewers accept either, but `/` is the lingua
    franca and reads cleanly in source).
    """
    return f"{images_dir_name}/{filename}"


def has_supported_extension(name: str, extensions: Iterable[str] = SUPPORTED_INSERT_EXTENSIONS) -> bool:
    suffix = Path(name).suffix.lower()
    return suffix in tuple(extensions)
