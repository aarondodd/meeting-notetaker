"""Post-process a Qt-generated PDF to add navigation (#94).

Qt's ``QTextDocument`` print path doesn't emit PDF link annotations
even when the source markup carries ``<a href="#x">`` /
``<a name="x">`` anchors. The user sees the TOC entries as visible
underlined text but clicking them doesn't jump anywhere.

The robust fix is to open the rendered PDF with pypdf and add the
navigation features Qt didn't write:

  1. **Outline (bookmarks)** -- one entry per heading. Most PDF
     viewers (Edge, Acrobat, SumatraPDF, Preview, Foxit) show this
     in a sidebar with click-to-jump behavior. Robust because we
     only need the page index for each heading.

  2. **Link annotations** on the in-page TOC entries -- clickable
     rectangles overlaid on each TOC line that jump to the matching
     heading's page. More fragile because it requires extracting
     text positions from the PDF, but functional for the typical
     case where the TOC entry text matches the heading text
     verbatim (which is how ``generate_toc`` builds them).

Both ship together so even if some link annotations fail to match,
the sidebar navigation still works.

Pure-Python; the only deps are pypdf (added to requirements) and
the markdown_outline helpers (slugify + iter_headings).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from .markdown_outline import (
    DEFAULT_TOC_MAX_DEPTH,
    TOC_HEADING,
    iter_headings,
)


log = logging.getLogger(__name__)


def add_pdf_navigation(
    pdf_path: Path,
    markdown_body: str,
    *,
    toc_max_depth: int = DEFAULT_TOC_MAX_DEPTH,
) -> dict:
    """Open ``pdf_path``, add outline + link annotations, write back.

    Returns a small stats dict (``{"outline_added": N,
    "links_added": M, "error": str|None}``) so callers can log
    what happened.

    Failures here are non-fatal -- a corrupted post-process pass
    shouldn't break PDF export. The original Qt PDF stays on disk
    if anything goes wrong; we only overwrite on a complete clean
    run.
    """
    stats = {"outline_added": 0, "links_added": 0, "error": None}
    try:
        import pypdf
        from pypdf.annotations import Link as _PdfLink
    except ImportError as exc:
        stats["error"] = f"pypdf not installed: {exc}"
        return stats
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        stats["error"] = f"PDF not found: {pdf_path}"
        return stats
    try:
        body_headings = _collect_body_headings(
            markdown_body, max_depth=toc_max_depth,
        )
        toc_entries = _collect_toc_entries(
            markdown_body, max_depth=toc_max_depth,
        )
        reader = pypdf.PdfReader(str(pdf_path))
        writer = pypdf.PdfWriter(clone_from=reader)
        # Walk pages once, extracting text with positions. Build:
        #   heading_text -> first page_idx where heading appears
        #   toc_text -> [(page_idx, rect), ...]
        heading_pages = _find_heading_pages(writer, body_headings)
        toc_positions = _find_toc_positions(
            writer, toc_entries, heading_pages,
        )
        for heading_text, page_idx in heading_pages.items():
            if page_idx is None:
                continue
            try:
                writer.add_outline_item(heading_text, page_idx)
                stats["outline_added"] += 1
            except Exception:
                log.debug(
                    "outline add failed for %r", heading_text,
                    exc_info=True,
                )
        for toc_text, page_idx, rect, dest_page_idx in toc_positions:
            try:
                link = _PdfLink(
                    rect=rect,
                    target_page_index=dest_page_idx,
                )
                writer.add_annotation(page_idx, link)
                stats["links_added"] += 1
            except Exception:
                log.debug(
                    "link add failed for TOC entry %r", toc_text,
                    exc_info=True,
                )
        tmp_path = pdf_path.with_suffix(".pdf.tmp")
        with open(tmp_path, "wb") as fh:
            writer.write(fh)
        tmp_path.replace(pdf_path)
    except Exception as exc:
        log.exception("PDF post-processing failed for %s", pdf_path)
        stats["error"] = str(exc)
    return stats


# ---- markdown parsing helpers ------------------------------------------


def _collect_body_headings(
    markdown_body: str, *, max_depth: int,
) -> list[str]:
    """Return body heading texts in document order.

    Skips the TOC section (the ``## Contents`` heading and its
    list of links) so we don't try to outline-bookmark it.
    Respects the user's ``toc_max_depth`` so the outline stays
    aligned with the TOC.
    """
    headings: list[str] = []
    in_toc = False
    for _idx, level, body in iter_headings(markdown_body or ""):
        text = body.strip()
        if not text:
            continue
        if text.lower() == "contents" and level == 2:
            in_toc = True
            continue
        # The TOC section's headings are the body's content. We don't
        # try to filter ## Contents children separately -- the TOC
        # block is a markdown list, not nested headings, so
        # iter_headings won't return those entries anyway.
        if in_toc and level > 2:
            continue
        if in_toc and level <= 2:
            # Any heading at H2 or higher after Contents ends the TOC.
            in_toc = False
        if level <= max_depth:
            headings.append(text)
    return headings


def _collect_toc_entries(
    markdown_body: str, *, max_depth: int,
) -> list[str]:
    """Return the TOC list entries' visible text, in order.

    The TOC is a markdown bullet list immediately after the
    ``## Contents`` heading, with entries of the form
    ``- [Heading text](#slug)``. We don't strictly require the
    Contents heading to be present; any ``[text](#slug)`` link in
    the source that points at an internal anchor is fair game --
    but in practice the TOC is the only thing that contains those.
    """
    if not markdown_body:
        return []
    import re
    link_re = re.compile(r"\[([^\]]+)\]\(#[^)]+\)")
    entries: list[str] = []
    for match in link_re.finditer(markdown_body):
        text = match.group(1).strip()
        if text:
            entries.append(text)
    # Dedupe while preserving order. A TOC entry text typically
    # matches the heading text in the body too, but the heading
    # itself doesn't have the markdown link syntax so isn't picked
    # up by `link_re`.
    seen: set[str] = set()
    deduped: list[str] = []
    for entry in entries:
        if entry not in seen:
            deduped.append(entry)
            seen.add(entry)
    return deduped


# ---- PDF text extraction with positions --------------------------------


def _normalize_for_match(text: str) -> str:
    """Collapse all whitespace runs to a single space + casefold.

    pypdf's ``extract_text`` writes tab characters between visually
    adjacent words ("1\\tIntroduction") and inserts newlines at line
    breaks, so a naive substring match against the source heading
    "1 Introduction" fails. Normalizing both sides before matching
    sidesteps the formatting quirks without losing match precision.
    """
    return " ".join((text or "").split()).casefold()


def _find_heading_pages(writer, headings: list[str]) -> dict[str, Optional[int]]:
    """For each heading text, find the first page (0-indexed) where
    it appears in the rendered PDF.

    Returns ``{heading_text: page_idx or None}``. Uses a normalized
    contains-match (whitespace collapsed, case-folded) so PDF
    text-extraction quirks don't defeat the match.
    """
    result: dict[str, Optional[int]] = {h: None for h in headings}
    remaining = set(headings)
    for page_idx, page in enumerate(writer.pages):
        if not remaining:
            break
        try:
            page_text = page.extract_text() or ""
        except Exception:
            continue
        norm_page = _normalize_for_match(page_text)
        matched_this_page: set[str] = set()
        for heading in remaining:
            if _normalize_for_match(heading) in norm_page:
                result[heading] = page_idx
                matched_this_page.add(heading)
        remaining -= matched_this_page
    return result


def _find_toc_positions(
    writer, toc_entries: list[str], heading_pages: dict[str, Optional[int]],
) -> list[tuple[str, int, tuple[float, float, float, float], int]]:
    """For each TOC entry, find its rendered bounding box and the
    destination page index.

    Returns ``[(toc_text, source_page_idx, rect, dest_page_idx), ...]``.
    Entries with no matched destination heading (or no extractable
    position) are dropped.

    We walk pages and use pypdf's ``visitor_text`` callback to
    capture per-text-run x/y positions. The TOC entry is matched
    by finding text runs whose concatenated content equals the
    entry text. The bbox is the union of those runs' rectangles.
    """
    out: list[tuple[str, int, tuple[float, float, float, float], int]] = []
    remaining = list(toc_entries)
    for page_idx, page in enumerate(writer.pages):
        if not remaining:
            break
        # Capture every text run's (text, x, y, width estimate)
        # tuple. PDF text shows can emit runs whose tm matrix is
        # zero -- the rendered position depends on the accumulated
        # text state, not the matrix at that call. We track the
        # last non-zero position and assume zero-tm runs continue
        # from there. This approximation works for the typical
        # case (single line of left-to-right text drawn as a
        # sequence of show operators).
        runs: list[tuple[str, float, float, float, float]] = []
        last_x = [0.0]
        last_y = [0.0]

        def visitor(text, cm, tm, font_dict, font_size):
            if not text or not text.strip():
                return
            x = float(tm[4]) if len(tm) >= 6 else 0.0
            y = float(tm[5]) if len(tm) >= 6 else 0.0
            if x != 0.0 or y != 0.0:
                last_x[0] = x
                last_y[0] = y
            else:
                x = last_x[0]
                y = last_y[0]
            fs = float(font_size or 12)
            # Approximate width: average char ~ 0.5 * font_size for
            # proportional fonts. Slightly oversized rect is fine
            # for click targets.
            approx_w = max(1.0, len(text) * fs * 0.5)
            runs.append((text, x, y, approx_w, fs))
            # Advance the inferred position for the next zero-tm
            # run on the same line.
            last_x[0] = x + approx_w

        try:
            page.extract_text(visitor_text=visitor)
        except Exception:
            continue
        for entry in list(remaining):
            dest_page_idx = heading_pages.get(entry)
            if dest_page_idx is None:
                # No destination identified -- can't make a usable
                # link annotation. Skip; the entry stays in
                # remaining for later pages but realistically won't
                # match elsewhere either.
                continue
            bbox = _bbox_for_text(runs, entry)
            if bbox is None:
                continue
            out.append((entry, page_idx, bbox, dest_page_idx))
            remaining.remove(entry)
    return out


def _bbox_for_text(
    runs: list[tuple[str, float, float, float, float]],
    target: str,
) -> Optional[tuple[float, float, float, float]]:
    """Find a contiguous span of runs whose concatenated text
    contains ``target``, return the union bbox.

    Returns ``(x1, y1, x2, y2)`` in PDF user space where the
    PDF origin is bottom-left and y increases upward.

    Tries a few matching strategies:

      1. Exact text equality on a single run -- common when the
         TOC entry fits in one Qt text segment.
      2. Adjacent run concatenation -- a single line that Qt
         broke into multiple text runs (e.g. mixed bold + plain).
    """
    if not runs or not target:
        return None
    target_norm = _normalize_for_match(target)
    # Strategy 1: single run contains the target.
    for text, x, y, w, fs in runs:
        if target_norm in _normalize_for_match(text):
            return (x, y, x + w, y + fs * 1.2)
    # Strategy 2: walk runs on the same approximate y, concatenate
    # text, look for the target. Group runs by line (same y +/- 1
    # PDF point).
    by_line: list[list[tuple[str, float, float, float, float]]] = []
    for run in runs:
        text, x, y, w, fs = run
        placed = False
        for line in by_line:
            if abs(line[0][2] - y) < 1.0:
                line.append(run)
                placed = True
                break
        if not placed:
            by_line.append([run])
    for line in by_line:
        # Sort by x within a line so concatenation order matches
        # reading order.
        line.sort(key=lambda r: r[1])
        concat = " ".join(r[0] for r in line)
        if target_norm in _normalize_for_match(concat):
            x1 = min(r[1] for r in line)
            y1 = min(r[2] for r in line)
            x2 = max(r[1] + r[3] for r in line)
            fs = max(r[4] for r in line)
            return (x1, y1, x2, y1 + fs * 1.2)
    return None
