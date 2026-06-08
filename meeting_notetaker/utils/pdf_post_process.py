"""Post-process a Qt-generated PDF to add TOC navigation (#94).

Qt's ``QTextDocument`` print path writes Link annotations with the
correct rectangles and named destination references (e.g.
``/Dest = '1-introduction'``). But the named destinations are
broken in two ways that prevent any PDF viewer from navigating:

  1. Qt doesn't write a ``/Names/Dests`` dictionary at all.
  2. Even when we add one (verified via PDFium / pypdfium2, which
     is Chrome's embedded PDF engine), browsers don't follow the
     string-name lookup from a link annotation's ``/Dest`` to a
     name tree entry in this form. Acrobat shows the rect as
     clickable but it resolves nowhere; Chrome / Edge don't even
     show it as clickable.

The fix that PDFium validates as navigable: rewrite each Link
annotation's ``/Dest`` from a string-name reference to an inline
destination array ``[page_ref /FitH top]``. PDFium follows inline
destination arrays directly without consulting the name tree.

We also add outline entries (PDF sidebar bookmarks) for each
heading so the viewer's sidebar tree gives a second navigation
path.

Validation: this module is exercised against the Qt-generated PDF
in tests using pypdfium2 (the same PDF engine Chrome and Edge use),
so "navigable in the test" maps directly to "navigable in the
user's browser PDF viewer."

Pure-Python; deps are pypdf (post-process write) and pypdfium2
(validation; only loaded in tests).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from .markdown_outline import (
    DEFAULT_TOC_MAX_DEPTH,
    TOC_HEADING,
    iter_headings,
    slugify,
)


log = logging.getLogger(__name__)


def add_pdf_navigation(
    pdf_path: Path,
    markdown_body: str,
    *,
    toc_max_depth: int = DEFAULT_TOC_MAX_DEPTH,
) -> dict:
    """Rewrite TOC link destinations + add sidebar outline, write back.

    For each Qt-written Link annotation whose ``/Dest`` is a slug
    string, replace the string reference with the inline
    destination array Qt already wrote for that slug in the PDF's
    ``/Names/Dests`` name tree. PDFium-based viewers (Chrome /
    Edge) don't follow string-name destinations from a Link
    annotation back into the name tree; they only honor inline
    arrays on the Link itself. Copying the tree entries inline is
    the smallest possible fix.

    Falls back to a font-size-aware text scan if a slug doesn't
    have a /Names/Dests entry (rare; only happens when something
    upstream strips the name tree).

    Also adds a PDF outline (sidebar bookmarks) entry per heading.

    Returns ``{"links_rewritten": N, "outline_added": M, "error": str|None}``.

    Failures are non-fatal -- a corrupted post-process leaves the
    original Qt PDF on disk untouched.
    """
    stats = {"links_rewritten": 0, "outline_added": 0, "error": None}
    try:
        import pypdf
        from pypdf.generic import ArrayObject, FloatObject, NameObject
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
        reader = pypdf.PdfReader(str(pdf_path))
        writer = pypdf.PdfWriter(clone_from=reader)

        # Qt writes the named-destinations tree at /Root/Names/Dests
        # with each slug pointing at an inline destination array of
        # the form [page_ref /XYZ x y zoom]. That array carries the
        # exact (page, y) Qt computed during pagination -- far more
        # accurate than re-scanning the rendered PDF text for the
        # heading. The bug we're fixing is that PDFium-based viewers
        # (Chrome / Edge) don't follow string-name destinations from
        # a Link annotation back into /Names/Dests; they only honor
        # inline arrays on the Link itself. So we read Qt's tree and
        # copy each entry inline onto the matching Link annotation,
        # translating the page references from the reader's object
        # namespace to the writer's (PdfWriter(clone_from=...) gives
        # each page a new indirect reference; the cloned name-tree
        # entries still hold the reader's page refs, which would
        # resolve to nothing in the writer's output).
        qt_dests = _read_named_dests(reader)
        slug_to_dest_array: dict[str, ArrayObject] = {}
        slug_to_page: dict[str, int] = {}
        for slug, dest_arr in qt_dests.items():
            translated = _translate_dest_to_writer(
                dest_arr, reader, writer,
            )
            if translated is None:
                continue
            slug_to_dest_array[slug] = translated
            page_idx = _dest_array_page_index(translated, writer)
            if page_idx is not None:
                slug_to_page[slug] = page_idx

        # Fallback for slugs Qt didn't put in the tree (rare). Scan
        # for heading occurrences using font-size discrimination.
        missing_headings = [
            h for h in body_headings
            if slugify(h) and slugify(h) not in slug_to_dest_array
        ]
        if missing_headings:
            heading_pages = _find_heading_pages(writer, missing_headings)
            for heading_text in missing_headings:
                page_idx = heading_pages.get(heading_text)
                if page_idx is None:
                    continue
                slug = slugify(heading_text)
                if not slug:
                    continue
                target_page_obj = writer.pages[page_idx]
                page_height = float(target_page_obj.mediabox.height)
                slug_to_dest_array[slug] = ArrayObject([
                    target_page_obj.indirect_reference,
                    NameObject("/FitH"),
                    FloatObject(page_height),
                ])
                slug_to_page[slug] = page_idx

        # Outline (sidebar bookmarks) -- one entry per heading, in
        # document order. Independent of the link rewrite below.
        for heading_text in body_headings:
            slug = slugify(heading_text)
            page_idx = slug_to_page.get(slug)
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

        # Walk every Link annotation; rewrite /Dest from a slug
        # string to the inline destination array.
        for page in writer.pages:
            for annot_ref in page.get("/Annots", []) or []:
                obj = annot_ref.get_object()
                if obj.get("/Subtype") != "/Link":
                    continue
                dest = obj.get("/Dest")
                if not isinstance(dest, str):
                    continue
                dest_array = slug_to_dest_array.get(dest)
                if dest_array is None:
                    continue
                try:
                    obj[NameObject("/Dest")] = dest_array
                    stats["links_rewritten"] += 1
                except Exception:
                    log.debug(
                        "link rewrite failed for slug %r", dest,
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


# ---- /Names/Dests extraction (Qt-written name tree) -------------------


def _read_named_dests(reader) -> dict:
    """Return ``{slug: ArrayObject([page_ref, /XYZ|/FitH|..., ...])}``
    from the PDF's ``/Root/Names/Dests`` name tree.

    Qt's QTextDocument PDF writer populates this tree with the exact
    destination per ``<a name="X">`` anchor it saw during print --
    page reference and y in PDF user coords. Reading the tree directly
    is the most reliable way to know where Qt wants each anchor to
    land; predicting pagination ourselves is brittle because Qt uses
    device-pixel coords internally that don't round-trip through
    QSizeF(points) cleanly.

    Returns an empty dict when there's no name tree (older PDFs or
    other tools' output).
    """
    out: dict = {}
    try:
        root = reader.trailer.get("/Root")
        if root is None:
            return out
        root_obj = root.get_object() if hasattr(root, "get_object") else root
        names = root_obj.get("/Names")
        if names is None:
            return out
        names_obj = (
            names.get_object() if hasattr(names, "get_object") else names
        )
        dests = names_obj.get("/Dests")
        if dests is None:
            return out
        dests_obj = (
            dests.get_object() if hasattr(dests, "get_object") else dests
        )
        _walk_name_tree(dests_obj, out)
    except Exception:
        log.debug("name tree read failed", exc_info=True)
    return out


def _walk_name_tree(node, out: dict) -> None:
    """Recurse a PDF name tree node, populating ``out``.

    PDF name trees are either flat (``/Names`` array of alternating
    name + value) or hierarchical (``/Kids`` array of subtree nodes).
    Qt typically writes the flat form; we handle both for robustness.
    """
    names = node.get("/Names")
    if names is not None:
        items = names if isinstance(names, list) else list(names)
        i = 0
        while i + 1 < len(items):
            slug = items[i]
            value = items[i + 1]
            if hasattr(value, "get_object"):
                value = value.get_object()
            # Some PDFs wrap the destination array in a /D dict; both
            # forms are valid per the PDF spec.
            if hasattr(value, "get") and value.get("/D") is not None:
                value = value.get("/D")
                if hasattr(value, "get_object"):
                    value = value.get_object()
            if isinstance(slug, str) and value is not None:
                out[slug] = value
            i += 2
    kids = node.get("/Kids")
    if kids is not None:
        for kid in kids:
            kid_obj = kid.get_object() if hasattr(kid, "get_object") else kid
            _walk_name_tree(kid_obj, out)


def _translate_dest_to_writer(dest_array, reader, writer):
    """Translate a destination array's page reference from the
    reader's namespace to the writer's.

    ``PdfWriter(clone_from=reader)`` copies pages but assigns new
    indirect references in the writer's output. Destination arrays
    read from the reader's name tree still point at reader page
    objects, which the viewer can't resolve in the writer's PDF. We
    find the reader page index, then build a new array using the
    writer's page indirect reference at the same index.
    """
    try:
        from pypdf.generic import ArrayObject
    except ImportError:
        return None
    if not dest_array or len(dest_array) == 0:
        return None
    page_ref = dest_array[0]
    target_obj = (
        page_ref.get_object() if hasattr(page_ref, "get_object") else page_ref
    )
    reader_idx = None
    for idx, r_page in enumerate(reader.pages):
        if r_page == target_obj:
            reader_idx = idx
            break
        try:
            if hasattr(r_page, "indirect_reference") and (
                r_page.indirect_reference == page_ref
            ):
                reader_idx = idx
                break
        except Exception:
            continue
    if reader_idx is None or reader_idx >= len(writer.pages):
        return None
    new_page_ref = writer.pages[reader_idx].indirect_reference
    out = ArrayObject([new_page_ref])
    for item in list(dest_array)[1:]:
        out.append(item)
    return out


def _dest_array_page_index(dest_array, writer) -> Optional[int]:
    """Find the page index for the page reference at the start of a
    destination array.

    PDF destination arrays are ``[page_ref view_type ...coords]``.
    The first element is an indirect reference to a page object.
    Map it back to a zero-indexed page number by comparing against
    the writer's page list.
    """
    if not dest_array or len(dest_array) == 0:
        return None
    page_ref = dest_array[0]
    target_obj = (
        page_ref.get_object() if hasattr(page_ref, "get_object") else page_ref
    )
    for idx, page in enumerate(writer.pages):
        if page == target_obj:
            return idx
        try:
            if hasattr(page, "indirect_reference") and (
                page.indirect_reference == page_ref
            ):
                return idx
        except Exception:
            continue
    return None


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
    """For each heading text, find the page where it's rendered AS A
    HEADING (not as a TOC bullet entry).

    Returns ``{heading_text: page_idx or None}``.

    The naive "first page containing the heading text" approach is
    broken when the TOC sits at the top of the document: the TOC
    bullets contain the heading text too, so every heading collapses
    to "page 0" (the TOC page). Clicking a TOC link then "navigates"
    to the page the user is already on, producing the
    do-nothing-on-click symptom.

    Strategy: walk text runs with a visitor that surfaces font_size,
    classify each text run as heading-sized vs body-sized by
    comparing against the page's body-text font-size mode, and only
    count a match when the run holding the target text is heading-
    sized. Falls back to plain-text containment when font-size info
    isn't available.
    """
    result: dict[str, Optional[int]] = {h: None for h in headings}
    remaining = set(headings)
    norm_headings = {h: _normalize_for_match(h) for h in headings}

    for page_idx, page in enumerate(writer.pages):
        if not remaining:
            break
        runs = _collect_text_runs(page)
        if runs:
            body_size = _body_text_font_size(runs)
            heading_threshold = body_size * 1.10 if body_size else 0.0
            # ONLY consider runs whose own font_size is heading-sized.
            # Mixing in body-sized runs (the TOC bullets emitted by Qt
            # with zero-tm matrices that inherit the y of the Contents
            # heading) would put TOC text into the same "line" as the
            # real heading, defeating the discrimination.
            if heading_threshold:
                heading_runs = [
                    r for r in runs if r[4] >= heading_threshold
                ]
            else:
                heading_runs = list(runs)
            for line in _group_runs_by_line(heading_runs):
                line.sort(key=lambda r: r[1])
                line_text_norm = _normalize_for_match(
                    " ".join(r[0] for r in line),
                )
                hit_now: list[str] = []
                for heading in remaining:
                    if norm_headings[heading] in line_text_norm:
                        result[heading] = page_idx
                        hit_now.append(heading)
                for h in hit_now:
                    remaining.discard(h)
        else:
            # Visitor returned nothing -- fall back to the legacy
            # substring search. Loses TOC-vs-heading discrimination on
            # this page but at least produces a guess.
            try:
                page_text = page.extract_text() or ""
            except Exception:
                continue
            norm_page = _normalize_for_match(page_text)
            for heading in list(remaining):
                if norm_headings[heading] in norm_page:
                    result[heading] = page_idx
                    remaining.discard(heading)
    return result


def _collect_text_runs(
    page,
) -> list[tuple[str, float, float, float, float]]:
    """Run pypdf's visitor_text and return runs as ``(text, x, y, w, fs)``.

    ``fs`` is the font size at the point of the show operator. Pages
    that don't yield runs (encrypted, malformed, etc.) return an
    empty list -- callers fall back to plain ``extract_text``.
    """
    runs: list[tuple[str, float, float, float, float]] = []
    last_x = [0.0]
    last_y = [0.0]

    def visitor(text, cm, tm, font_dict, font_size):
        if not text or not text.strip():
            return
        x = float(tm[4]) if hasattr(tm, "__len__") and len(tm) >= 6 else 0.0
        y = float(tm[5]) if hasattr(tm, "__len__") and len(tm) >= 6 else 0.0
        if x != 0.0 or y != 0.0:
            last_x[0] = x
            last_y[0] = y
        else:
            x = last_x[0]
            y = last_y[0]
        fs = float(font_size or 0.0)
        approx_w = max(1.0, len(text) * fs * 0.5)
        runs.append((text, x, y, approx_w, fs))
        last_x[0] = x + approx_w

    try:
        page.extract_text(visitor_text=visitor)
    except Exception:
        return []
    return runs


def _body_text_font_size(
    runs: list[tuple[str, float, float, float, float]],
) -> float:
    """Most common nonzero font size in the page's runs.

    Used as the "body text" reference; anything materially larger
    is treated as a heading. Returns 0.0 when no usable sizes are
    available so the caller falls back to substring matching.
    """
    counts: dict[float, int] = {}
    for _t, _x, _y, _w, fs in runs:
        if fs <= 0.0:
            continue
        # Bucket to 0.5pt so jitter from PDF emission doesn't split
        # one font size into two buckets.
        bucket = round(fs * 2) / 2
        counts[bucket] = counts.get(bucket, 0) + 1
    if not counts:
        return 0.0
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _group_runs_by_line(
    runs: list[tuple[str, float, float, float, float]],
) -> list[list[tuple[str, float, float, float, float]]]:
    """Bucket runs by approximate y coordinate (+/- 1 pt).

    The same heading text often spans multiple runs (one per styled
    span). Grouping by line and concatenating before matching means
    "1 Introduction" survives Qt splitting it into "1" + " " +
    "Introduction" runs.
    """
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
    return by_line


def _find_toc_positions(  # pragma: no cover -- not used by add_pdf_navigation
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
