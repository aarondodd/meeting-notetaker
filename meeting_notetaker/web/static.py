"""Inline static assets served at /static/*.

Everything is a module-level string so a frozen pyinstaller build does
not have to carry an on-disk resources folder for the web UI.
"""
from __future__ import annotations


STYLE_CSS = """
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: #f6f7f9;
  color: #1f2328;
  line-height: 1.45;
}
header {
  background: #ffffff;
  border-bottom: 1px solid #d0d7de;
  padding: 12px 24px;
}
header .title { font-size: 18px; font-weight: 600; }
header .sub   { font-size: 12px; color: #6e7781; margin-top: 2px; }
header a {
  color: #0969da;
  text-decoration: none;
  margin-right: 12px;
}
header a:hover { text-decoration: underline; }
main { padding: 20px 24px; max-width: 1100px; margin: 0 auto; }
main.with-sidebar {
  max-width: 1240px;
  display: grid;
  grid-template-columns: 180px minmax(0, 1fr);
  gap: 24px;
}
main.with-sidebar .content { min-width: 0; }
aside.toc {
  position: sticky;
  top: 16px;
  align-self: start;
  font-size: 13px;
  background: #ffffff;
  border: 1px solid #d0d7de;
  border-radius: 6px;
  padding: 12px 14px;
  max-height: calc(100vh - 48px);
  overflow-y: auto;
}
aside.toc .toc-title {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.03em;
  color: #57606a;
  margin-bottom: 8px;
}
aside.toc ul.toc-list {
  list-style: none;
  margin: 0;
  padding: 0;
}
aside.toc ul.toc-list li { margin: 0 0 4px; }
aside.toc ul.toc-list a {
  color: #0969da;
  text-decoration: none;
  display: block;
  padding: 3px 6px;
  border-radius: 4px;
}
aside.toc ul.toc-list a:hover {
  background: #f6f8fa;
  text-decoration: none;
}
aside.toc .toc-back {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px solid #eaeef2;
  font-size: 12px;
}
aside.toc .toc-back a { color: #57606a; text-decoration: none; }
aside.toc .toc-back a:hover { text-decoration: underline; }
@media (max-width: 760px) {
  main.with-sidebar {
    grid-template-columns: 1fr;
    gap: 12px;
  }
  aside.toc {
    position: static;
    max-height: none;
  }
  aside.toc ul.toc-list {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }
  aside.toc ul.toc-list li { margin: 0; }
  aside.toc .toc-back { margin-top: 6px; padding-top: 6px; }
}
section {
  background: #ffffff;
  border: 1px solid #d0d7de;
  border-radius: 6px;
  padding: 16px;
  margin-bottom: 16px;
}
section h2 {
  font-size: 14px;
  font-weight: 600;
  margin: 0 0 10px 0;
  text-transform: uppercase;
  color: #57606a;
  letter-spacing: 0.03em;
}
details { margin-top: 8px; }
details summary {
  cursor: pointer;
  font-size: 13px;
  color: #0969da;
  padding: 4px 0;
}
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th, td {
  text-align: left;
  padding: 8px 10px;
  border-bottom: 1px solid #eaeef2;
  vertical-align: top;
}
th {
  background: #f6f8fa;
  font-weight: 600;
  color: #57606a;
  font-size: 12px;
  text-transform: uppercase;
}
tbody tr:hover { background: #f6f8fa; }
a.row-link { color: #0969da; text-decoration: none; }
a.row-link:hover { text-decoration: underline; }
.filters {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
  margin-bottom: 14px;
}
.filters label { font-size: 13px; color: #57606a; }
.filters select, .filters input[type="text"], .filters input[type="number"] {
  padding: 4px 8px;
  border: 1px solid #d0d7de;
  border-radius: 4px;
  font-size: 13px;
}
.filters button, button {
  padding: 5px 12px;
  background: #2da44e;
  color: #ffffff;
  border: 1px solid rgba(27,31,36,0.15);
  border-radius: 4px;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
}
button.secondary {
  background: #f6f8fa;
  color: #1f2328;
}
button:hover { opacity: 0.9; }
.meta-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 12px;
}
.meta-grid .cell { font-size: 13px; }
.meta-grid .cell .lbl {
  color: #57606a;
  text-transform: uppercase;
  font-size: 11px;
  letter-spacing: 0.03em;
}
.badge {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 10px;
  font-size: 11px;
  font-weight: 500;
  background: #ddf4ff;
  color: #0969da;
}
.badge.has    { background: #dafbe1; color: #116329; }
.badge.hasnt  { background: #f6f8fa; color: #6e7781; }
.badge.state  { background: #fff8c5; color: #633c01; }
.badge.state.complete  { background: #dafbe1; color: #116329; }
.badge.state.error     { background: #ffebe9; color: #82071e; }
pre {
  background: #f6f8fa;
  border: 1px solid #d0d7de;
  border-radius: 4px;
  padding: 10px;
  overflow-x: auto;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12.5px;
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0;
}
textarea.notes {
  width: 100%;
  min-height: 320px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 13px;
  padding: 10px;
  border: 1px solid #d0d7de;
  border-radius: 4px;
  resize: vertical;
}
.flash {
  padding: 10px 14px;
  border-radius: 6px;
  margin-bottom: 14px;
  font-size: 13px;
}
.flash.ok  { background: #dafbe1; color: #116329; border: 1px solid #a6ddb0; }
.flash.err { background: #ffebe9; color: #82071e; border: 1px solid #ff8182; }
form.inline { display: inline-flex; gap: 8px; align-items: center; }
.attendees { margin: 0; padding-left: 18px; font-size: 13px; }
.footer {
  text-align: center;
  color: #8c959f;
  font-size: 11px;
  padding: 20px 0;
}

/* Rich markdown rendering for synthesis + live-notes panes.
   Kept tight so the surrounding section chrome still dominates. */
.md {
  font-size: 14px;
  color: #1f2328;
}
.md h1, .md h2, .md h3, .md h4 {
  font-weight: 600;
  margin: 14px 0 8px;
  line-height: 1.3;
}
.md h1 { font-size: 20px; border-bottom: 1px solid #eaeef2; padding-bottom: 6px; }
.md h2 { font-size: 17px; }
.md h3 { font-size: 15px; color: #24292f; }
.md h4 { font-size: 14px; color: #24292f; }
.md p { margin: 0 0 10px; }
.md ul, .md ol { margin: 0 0 10px 0; padding-left: 22px; }
.md li { margin-bottom: 2px; }
.md li > p { margin: 0; }
.md blockquote {
  border-left: 3px solid #d0d7de;
  padding: 2px 10px;
  color: #57606a;
  margin: 0 0 10px;
}
.md code {
  background: #f6f8fa;
  border-radius: 3px;
  padding: 1px 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12.5px;
}
.md pre {
  background: #f6f8fa;
  border: 1px solid #d0d7de;
  border-radius: 4px;
  padding: 10px;
  overflow-x: auto;
  font-size: 12.5px;
  white-space: pre-wrap;
}
.md pre code { background: transparent; padding: 0; }
.md table {
  border-collapse: collapse;
  margin: 0 0 10px;
  font-size: 13px;
}
.md table th, .md table td {
  border: 1px solid #d0d7de;
  padding: 4px 10px;
  text-align: left;
}
.md table th {
  background: #f6f8fa;
  font-weight: 600;
}
.md a { color: #0969da; }
.md hr {
  border: none;
  border-top: 1px solid #d0d7de;
  margin: 14px 0;
}
.md del { color: #8c959f; }
.md input[type="checkbox"] { margin-right: 4px; }

/* Inline per-row export form (meeting list "To Obsidian" button). */
form.inline-form { display: inline; margin: 0; }
button.tiny {
  padding: 3px 8px;
  font-size: 11px;
}
.muted {
  color: #8c959f;
  font-size: 12px;
}
"""
