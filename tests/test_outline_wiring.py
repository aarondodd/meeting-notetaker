"""Integration tests for the outline transform wiring (#92).

Pure-Python: confirms that the per-export entry points pass the
outline-transform kwargs through to the markdown body without
mocking the heavy Qt / network paths. The actual transforms are
tested exhaustively in test_markdown_outline.py.
"""
from __future__ import annotations

import inspect

import pytest

from meeting_notetaker.utils import markdown_outline


def test_apply_outline_is_exported():
    """Sanity: callers can import the module-level helpers."""
    assert callable(markdown_outline.apply_outline)
    assert callable(markdown_outline.number_headings)
    assert callable(markdown_outline.inject_toc)


# ---- Notion + Confluence export signature pin ---------------------------

def test_export_to_notion_accepts_outline_kwargs():
    """The Notion export entry point exposes the outline kwargs so
    the calling worker can plumb config values through."""
    from meeting_notetaker.integrations.export import export_to_notion
    sig = inspect.signature(export_to_notion)
    assert "number_headings" in sig.parameters
    assert "include_toc" in sig.parameters
    # Defaults preserve existing behavior for any caller that didn't
    # opt in.
    assert sig.parameters["number_headings"].default is False
    assert sig.parameters["include_toc"].default is False


def test_export_to_confluence_accepts_outline_kwargs():
    from meeting_notetaker.integrations.export import export_to_confluence
    sig = inspect.signature(export_to_confluence)
    assert "number_headings" in sig.parameters
    assert "include_toc" in sig.parameters
    assert sig.parameters["number_headings"].default is False
    assert sig.parameters["include_toc"].default is False


def test_notion_worker_accepts_outline_kwargs():
    from meeting_notetaker.integrations.export_worker import NotionExportWorker
    sig = inspect.signature(NotionExportWorker.__init__)
    assert "number_headings" in sig.parameters
    assert "include_toc" in sig.parameters


def test_confluence_worker_accepts_outline_kwargs():
    from meeting_notetaker.integrations.export_worker import ConfluenceExportWorker
    sig = inspect.signature(ConfluenceExportWorker.__init__)
    assert "number_headings" in sig.parameters
    assert "include_toc" in sig.parameters


# ---- PDF render entry point pin -----------------------------------------

def test_render_session_pdf_accepts_outline_kwargs():
    from meeting_notetaker.utils.export_package import render_session_pdf
    sig = inspect.signature(render_session_pdf)
    assert "number_headings" in sig.parameters
    assert "include_toc" in sig.parameters
    assert sig.parameters["number_headings"].default is False
    assert sig.parameters["include_toc"].default is False


# ---- Config field round-trip --------------------------------------------

def test_config_synthesis_carries_outline_toggles(isolated_data_dir):
    from meeting_notetaker.utils.config import Config

    cfg = Config()
    assert hasattr(cfg.synthesis, "heading_numbering")
    assert hasattr(cfg.synthesis, "toc_in_exports")
    # Defaults off so existing exports stay byte-for-byte identical.
    assert cfg.synthesis.heading_numbering is False
    assert cfg.synthesis.toc_in_exports is False


def test_config_synthesis_outline_toggles_round_trip(isolated_data_dir):
    from meeting_notetaker.utils.config import Config

    cfg = Config()
    cfg.synthesis.heading_numbering = True
    cfg.synthesis.toc_in_exports = True
    cfg.save()
    loaded = Config.load()
    assert loaded.synthesis.heading_numbering is True
    assert loaded.synthesis.toc_in_exports is True


def test_config_carries_skip_h1_and_max_depth(isolated_data_dir):
    """#92 follow-up: per-Aaron Settings should expose skip_h1
    and toc_max_depth with sensible defaults."""
    from meeting_notetaker.utils.config import Config

    cfg = Config()
    assert hasattr(cfg.synthesis, "heading_numbering_skip_h1")
    assert hasattr(cfg.synthesis, "toc_max_depth")
    # Defaults: include H1, depth 3.
    assert cfg.synthesis.heading_numbering_skip_h1 is False
    assert cfg.synthesis.toc_max_depth == 3


def test_config_skip_h1_and_max_depth_round_trip(isolated_data_dir):
    from meeting_notetaker.utils.config import Config

    cfg = Config()
    cfg.synthesis.heading_numbering_skip_h1 = True
    cfg.synthesis.toc_max_depth = 5
    cfg.save()
    loaded = Config.load()
    assert loaded.synthesis.heading_numbering_skip_h1 is True
    assert loaded.synthesis.toc_max_depth == 5


# ---- signature pin for the new kwargs ----------------------------------

def test_export_to_notion_accepts_skip_h1_and_max_depth():
    from meeting_notetaker.integrations.export import export_to_notion
    sig = inspect.signature(export_to_notion)
    assert "skip_h1" in sig.parameters
    assert "toc_max_depth" in sig.parameters
    assert sig.parameters["skip_h1"].default is False
    assert sig.parameters["toc_max_depth"].default == 3


def test_export_to_confluence_accepts_skip_h1_and_max_depth():
    from meeting_notetaker.integrations.export import export_to_confluence
    sig = inspect.signature(export_to_confluence)
    assert "skip_h1" in sig.parameters
    assert "toc_max_depth" in sig.parameters


def test_render_session_pdf_accepts_skip_h1_and_max_depth():
    from meeting_notetaker.utils.export_package import render_session_pdf
    sig = inspect.signature(render_session_pdf)
    assert "skip_h1" in sig.parameters
    assert "toc_max_depth" in sig.parameters
    assert sig.parameters["toc_max_depth"].default == 3
