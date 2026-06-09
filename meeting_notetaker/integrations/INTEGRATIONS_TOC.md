# Native TOC convention for Save-To integrations (#94)

When a Save-To target supports a native table-of-contents primitive,
**use it** instead of emitting our own markdown TOC list. Native
TOCs are:

  * **Clickable** -- they navigate, our markdown bullet list doesn't
    in most renderers.
  * **Auto-syncing** -- if the user edits the destination later,
    the TOC updates automatically.
  * **Styled to match the platform** -- looks native, not foreign.

## Current native-TOC support

| Integration | Native TOC primitive | Status |
|---|---|---|
| Confluence | `<ac:structured-macro ac:name="toc">` macro with `maxLevel` parameter | Implemented (#92 / #94). See `confluence_storage.build_toc_macro` and `export.export_to_confluence`. |
| Notion | `table_of_contents` block | Implemented (#94). See `notion_blocks.build_toc_block` and `export.export_to_notion`. |
| PDF | Named destinations + Link annotations (Qt path) | Implemented (#94). Qt writes the link annotations correctly but the post-processor in `utils/pdf_post_process.py` rewrites their `/Dest` from name-strings to inline arrays so PDFium-based viewers (Chrome / Edge) navigate them. |
| Word (.docx) | TOC field (`TOC \o "1-N" \h \z \u`) inserted via `python-docx`; populated by Word on first open OR by our `populate_toc_via_word` COM helper on Windows | Implemented (#94 follow-up). See `utils/word_export.export_to_docx`, `populate_toc_via_word`, `export_to_pdf_via_word`. |

## How the integration should handle TOC

In `export_to_<integration>`:

  1. Apply heading numbering to the markdown body (if the
     `number_headings` toggle is on). Numbered headings flow into
     the integration's rendered output regardless of where the
     TOC lives.
  2. **Skip the markdown TOC list** (`apply_outline(..., toc=True)`).
     Our markdown bullet list with `[text](#slug)` links is for
     output paths that don't have a native TOC. Native-TOC paths
     don't need it -- and would render it as duplicate static text
     next to the proper native TOC.
  3. Convert the markdown body to the integration's block / XML
     format as usual.
  4. **Prepend (or insert at the top of the body)** the
     integration's native TOC primitive. It auto-discovers the
     headings server-side.

## When to ship a new integration without native TOC

If the platform has no native TOC, fall back to the markdown TOC
that `utils.markdown_outline.apply_outline(..., toc=True)` produces.
The links won't navigate in every renderer (depends on the
destination's anchor support) but the user gets a readable list of
the document's structure.

## Future integrations

| Target | Native TOC? | Notes |
|---|---|---|
| Google Docs | `documents.batchUpdate -> Doc TOC` | TOC support via API exists -- use it. |
| Microsoft OneNote | No native TOC primitive | Fall back to markdown TOC. |
| Obsidian (vault save) | Obsidian's built-in outline view + the Outline community plugin | Implemented (#96). Obsidian renders an outline in the right sidebar from the document's headings; we explicitly do NOT emit a markdown TOC into the body because it would duplicate the outline view. The export still respects `number_headings`, just not `toc_in_exports`. |
