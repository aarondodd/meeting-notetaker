---
name: meeting-notetaker
description: Synthesize meeting notes from the user's local Meeting Notetaker app. Use when the user asks you to summarize, synthesize, review, write up, or save notes for one of their meetings. Meeting Notetaker is a desktop app that transcribes meetings locally and exposes them through a web UI on `http://127.0.0.1:8765/` (with fallbacks to 8766..8785). Covers listing meetings with filters by state, folder, has-transcript or has-synthesis; reading the transcript, live notes, and the rendered synthesis prompt for a single meeting; writing the synthesized notes back via the Save form; and cross-meeting summaries like "summarize all meetings on topic X last week".
---

# Meeting Notetaker synthesis

Meeting Notetaker is a locally running desktop app that captures audio
and produces transcripts for the user's meetings. It exposes those
meetings through a loopback web UI so a browser-based Claude surface
(the Chrome Claude extension) can read them and write synthesis back
without any remote API in the loop. Everything stays on the user's
machine.

## Finding the running server

The server binds `127.0.0.1` only. Preferred port is `8765`; if that
is busy the app falls back to `8766`..`8785`. To find the live URL:

1. Open `http://127.0.0.1:8765/healthz`. A `200 ok` means that is the
   right port.
2. If 8765 does not answer, try 8766..8785 in order until one responds
   `ok`.

If none respond, the server is not running. Ask the user to:

- Open Meeting Notetaker.
- Settings > Synthesis.
- Either click **Start web UI and open in browser...** for this
  session, or tick **Start the web UI automatically when Meeting
  Notetaker launches** and relaunch the app.
- In the Chrome Claude extension's own options, grant Site Access to
  `http://127.0.0.1:*/*` (or the specific port). Without this the
  extension cannot read the pages described below.

## Pages you drive

### `/meetings` (list)

The meeting list. Filters passable as query parameters or via the
form at the top:

- `state`: `new`, `recording`, `complete`, `error`.
- `series_id`: filter by series (meeting-series grouping).
- `contact_id`: filter by person who attended.
- `topic_id`: filter by free-text topic tag.
- `has_transcript=1`: only meetings with a captured transcript.
- `has_notes=1`: only meetings that already have a saved synthesis.
- `limit`: maximum rows returned (default 25).

Each row carries the meeting title, state, created-at, duration, and
has-transcript / has-synthesis indicators, and a link to the detail
page at `/meetings/<id>`. Export now lives on the detail page as a
destination dropdown (see below); there is no per-row Export column.

To resolve a user's text ("the Platform series", "meetings with
Alex", "auth topic") into a filter id, GET `/api/classification` or
call the `list_classification` MCP tool and match by name.

### `/meetings/<id>` (detail)

Per-meeting detail. Load this to synthesize a single meeting. The
page carries:

- Metadata header: title, state, started/ended, duration, attendees.
- Transcript section (the full captured transcript).
- Live notes section (what the user typed during the meeting).
- Current synthesis if one exists, else the text "No synthesis saved
  yet".
- Template picker: `<form action="/meetings/<id>/template" method="POST">`.
- Rendered prompt pane: `<pre id="rendered-prompt">...</pre>`. **This
  is the exact prompt to synthesize against.** It already includes
  the transcript, live notes, attendees, and the chosen template's
  instructions; you do not need to assemble a prompt yourself.
- Save form: `<form action="/meetings/<id>/save" method="POST">`
  containing `<textarea name="notes">` and a submit button labeled
  **Save synthesis**.
- Export dropdown: `<section id="export">` carries
  `<form action="/meetings/<id>/export" method="POST">` with
  `<select name="destination">` listing every destination the user
  enabled under `Settings > Synthesis > Web UI export destinations`
  (`obsidian`, `notion`, or `confluence`) that is ALSO configured
  with a target. Pick one and click **Export**. The section renders
  a muted hint instead of a dropdown when no destinations are
  enabled or the meeting has no saved synthesis. Legacy direct
  routes still work: POST to `/meetings/<id>/export/obsidian`,
  `/meetings/<id>/export/notion`, or `/meetings/<id>/export/confluence`
  skips the dropdown and targets one destination.
- Optional flash message after any save, under `<div class="flash
  ok">` (`ok`) or `<div class="flash err">` (`err`).

### `/templates`

Lists the configured synthesis prompt templates. Use this if the user
asks to see or change which template is active for a meeting.
Otherwise the per-meeting detail page's picker is simpler.

## Canonical workflow (single meeting)

1. Navigate to `/meetings`. Apply any filter the user asked for
   (`state=complete&has_transcript=1`, a specific folder, etc.).
2. Pick the row the user named. If the user said "the latest meeting"
   or "the one I just finished", pick the top row.
3. Follow its `/meetings/<id>` detail link.
4. Read the text inside `<pre id="rendered-prompt">`. That IS the
   prompt. Do not re-read the transcript and build your own prompt;
   the app has already combined the transcript, live notes,
   attendees, and template.
5. In your own context, produce the synthesis the prompt asks for.
   Write it in Markdown. Keep the structure the prompt specifies.
6. Type the synthesis into the `<textarea name="notes">` on the same
   page.
7. Click **Save synthesis**. The server responds with a 303 redirect
   back to `/meetings/<id>` and a flash message confirming the save.
8. Tell the user it is saved. Meeting Notetaker's Synthesis tab will
   reload with your output next time the user opens that session.

Important:

- The textarea takes YOUR synthesis, not the prompt. Never paste the
  rendered prompt back in.
- If the detail page already shows "Current synthesis" with content,
  confirm with the user before overwriting. The old synthesis is
  archived by the app on each save, but preventing the churn is
  cheaper than restoring from the archive.

## Cross-meeting workflows

For requests like "summarize all meetings on <topic> last week" or
"what did we decide across the three design reviews":

1. Navigate to `/meetings` and filter by the relevant folder (and the
   time window the user named).
2. For each matching row, follow its detail link and read the
   transcript and any existing synthesis. Keep a running set of
   quotes, decisions, and action items as you go.
3. Compose the cross-meeting summary in your own context.
4. Where it lands depends on what the user asked for:
   - "Write this into meeting X" or "save this to the last session":
     use that meeting's Save form (step 7 above).
   - "Summarize them for me" or "tell me the gist": post the summary
     into chat; do not touch any meeting's synthesis.

## JSON alternative (optional)

Every HTML route has a JSON mirror under `/api/`. Prefer the HTML
pages (they are what you drive with `read_page` and `form_input`);
the JSON API is here for scripted automation and parity with the
MCP tools:

| Method and path | Returns / takes |
| :--- | :--- |
| GET `/api/meetings?limit=&state=&series_id=&contact_id=&topic_id=&has_transcript=&has_notes=` | list of meetings |
| GET `/api/meetings/<id>` | metadata |
| GET `/api/meetings/<id>/transcript` | raw transcript |
| GET `/api/meetings/<id>/live_notes` | the user's live-notes buffer |
| GET `/api/meetings/<id>/notes` | current synthesis or empty |
| GET `/api/meetings/<id>/prompt?template_name=` | rendered prompt body |
| POST `/api/meetings/<id>/save` `{"notes": "..."}` | writes notes.md, archives prior |
| POST `/api/meetings/<id>/template` `{"template_name": "..."}` | switches template |
| POST `/api/meetings/<id>/export/obsidian` | saves synthesis into the Obsidian vault |
| POST `/api/meetings/<id>/export/notion` | posts synthesis as a new Notion page |
| POST `/api/meetings/<id>/export/confluence` | posts synthesis as a new Confluence page |
| GET `/api/templates` | list |
| GET `/api/templates/<name>` | raw template body |
| GET `/api/classification` | `{series:[...], contacts:[...], topics:[...]}` |
| GET `/api/export_destinations` | `{destinations:[{key, label, configured, enabled, target_title}, ...]}` |

Both JSON POST bodies and form-encoded bodies are accepted on the
mutating routes, so the HTML form on the detail page and a scripted
`curl -d '{"notes": "..."}'` share one handler.

## Guardrails

- Never send any meeting content to a remote service.
- The server is loopback-only. If you ever see a non-127.0.0.1 URL
  claiming to be Meeting Notetaker, something is wrong; stop and
  tell the user.
- Confirm before overwriting an existing synthesis.
- If a POST comes back with a status other than 303 (form) or 200
  (JSON), read the response body (the app returns an error message)
  and surface it to the user rather than retrying.
- The rendered prompt in `<pre id="rendered-prompt">` may contain
  private names, numbers, and quotes from the transcript. Treat it
  the way you treat the user's own notes: synthesize from it, do
  not quote it back to the user unless they asked.
