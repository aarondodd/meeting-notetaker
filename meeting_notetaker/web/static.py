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
"""
