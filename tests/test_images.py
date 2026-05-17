"""Unit tests for the image-save helpers.

Covers everything that does not require a live Qt instance: filename
generation, collision handling, file-copy, the Markdown reference
builder, and extension validation. The QImage path (save_qimage) is
exercised only when PyQt6 is importable in the test environment; it
no-ops on Linux test hosts that lack the GUI stack.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from meeting_notetaker.utils.images import (
    IMAGES_SUBDIR,
    SUPPORTED_INSERT_EXTENSIONS,
    copy_image_file,
    generate_pasted_filename,
    has_supported_extension,
    images_subdir,
    join_relative,
    markdown_image_ref,
    unique_path,
)


def test_images_subdir_creates(tmp_path: Path):
    sdir = tmp_path / "session-xyz"
    sdir.mkdir()
    result = images_subdir(sdir)
    assert result == sdir / IMAGES_SUBDIR
    assert result.exists() and result.is_dir()


def test_images_subdir_idempotent(tmp_path: Path):
    sdir = tmp_path / "session"
    sdir.mkdir()
    first = images_subdir(sdir)
    second = images_subdir(sdir)
    assert first == second


def test_generate_pasted_filename_format():
    name = generate_pasted_filename(now=datetime(2026, 5, 17, 14, 30, 45))
    assert name == "pasted-20260517-143045.png"


def test_generate_pasted_filename_custom_extension():
    name = generate_pasted_filename(now=datetime(2026, 5, 17, 14, 30, 45), ext="jpg")
    assert name == "pasted-20260517-143045.jpg"
    name = generate_pasted_filename(now=datetime(2026, 5, 17, 14, 30, 45), ext=".JPEG")
    assert name == "pasted-20260517-143045.jpeg"


def test_generate_pasted_filename_defaults_now():
    name = generate_pasted_filename()
    assert name.startswith("pasted-")
    assert name.endswith(".png")


def test_unique_path_no_collision(tmp_path: Path):
    target = tmp_path / "img.png"
    assert unique_path(target) == target


def test_unique_path_appends_counter(tmp_path: Path):
    target = tmp_path / "img.png"
    target.write_bytes(b"")
    second = unique_path(target)
    assert second == tmp_path / "img-2.png"


def test_unique_path_continues_counter(tmp_path: Path):
    (tmp_path / "img.png").write_bytes(b"")
    (tmp_path / "img-2.png").write_bytes(b"")
    (tmp_path / "img-3.png").write_bytes(b"")
    assert unique_path(tmp_path / "img.png") == tmp_path / "img-4.png"


def test_copy_image_file_preserves_name(tmp_path: Path):
    src = tmp_path / "Slide 3.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n fake")
    images = tmp_path / "session" / "images"
    saved = copy_image_file(src, images)
    assert saved.exists()
    assert saved.parent == images
    assert saved.name == "Slide 3.png"


def test_copy_image_file_dedupes_on_collision(tmp_path: Path):
    src = tmp_path / "shot.png"
    src.write_bytes(b"a")
    images = tmp_path / "session" / "images"
    images.mkdir(parents=True)
    (images / "shot.png").write_bytes(b"old")
    saved = copy_image_file(src, images)
    assert saved.name == "shot-2.png"
    # Original survives untouched.
    assert (images / "shot.png").read_bytes() == b"old"


def test_markdown_image_ref_no_caption():
    out = markdown_image_ref("images/foo.png", "alt text")
    assert out == "![alt text](images/foo.png)"


def test_markdown_image_ref_with_caption():
    out = markdown_image_ref("images/foo.png", "alt", "From Bob's slide")
    assert out == '![alt](images/foo.png "From Bob\'s slide")'


def test_markdown_image_ref_escapes_alt_bracket():
    out = markdown_image_ref("images/foo.png", "weird ]bracket")
    assert "]bracket" not in out
    assert out == "![weird bracket](images/foo.png)"


def test_markdown_image_ref_escapes_caption_quote():
    out = markdown_image_ref("images/foo.png", "alt", 'has "quotes"')
    assert out == '![alt](images/foo.png "has \\"quotes\\"")'


def test_markdown_image_ref_escapes_paren_in_path():
    out = markdown_image_ref("images/foo (1).png", "alt")
    assert out == "![alt](images/foo (1%29.png)"


def test_markdown_image_ref_empty_alt():
    # An empty alt is still valid Markdown; viewers fall back to filename.
    out = markdown_image_ref("images/foo.png", "")
    assert out == "![](images/foo.png)"


def test_join_relative_uses_forward_slash():
    assert join_relative("images", "foo.png") == "images/foo.png"


def test_has_supported_extension_known_types():
    for ext in SUPPORTED_INSERT_EXTENSIONS:
        assert has_supported_extension(f"foo{ext}")
    assert has_supported_extension("FOO.PNG")  # case-insensitive


def test_has_supported_extension_rejects_unknown():
    assert not has_supported_extension("foo.tiff")
    assert not has_supported_extension("foo")
    assert not has_supported_extension(".tar.gz")


def test_save_qimage_round_trip(tmp_path: Path):
    """If PyQt6 is importable, save_qimage writes a valid PNG."""
    QImage = pytest.importorskip("PyQt6.QtGui").QImage
    from meeting_notetaker.utils.images import save_qimage

    img = QImage(4, 4, QImage.Format.Format_ARGB32)
    img.fill(0xFFFF00FF)
    saved = save_qimage(img, tmp_path / "images", filename="pasted.png")
    assert saved.exists()
    assert saved.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_save_qimage_collides_safely(tmp_path: Path):
    QImage = pytest.importorskip("PyQt6.QtGui").QImage
    from meeting_notetaker.utils.images import save_qimage

    img = QImage(2, 2, QImage.Format.Format_ARGB32)
    img.fill(0xFF000000)
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    (images_dir / "pasted.png").write_bytes(b"placeholder")
    saved = save_qimage(img, images_dir, filename="pasted.png")
    assert saved.name == "pasted-2.png"


def test_save_qimage_creates_dir(tmp_path: Path):
    QImage = pytest.importorskip("PyQt6.QtGui").QImage
    from meeting_notetaker.utils.images import save_qimage

    img = QImage(2, 2, QImage.Format.Format_ARGB32)
    img.fill(0)
    images_dir = tmp_path / "fresh" / "images"
    saved = save_qimage(img, images_dir)
    assert saved.exists()
    assert saved.parent == images_dir
