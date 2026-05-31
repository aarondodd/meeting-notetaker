# OWA Calendar Probe

Experiment for [issue #69](https://github.com/aarondodd/meeting-notetaker/issues/69)
Option C: replace the deprecating Outlook COM path with a Chrome
content script that reads OWA's internal calendar API and forwards
the result over native messaging.

**This is sandbox code.** Nothing here imports from
`meeting_notetaker/`, nothing here is wired into the prod app, and
the bundled installer never sees it. The branch
`experiment/owa-calendar-probe` carries it; merging is gated on
proving the OWA payload shape is sufficient for the existing
`MeetingInfo` consumers.

## What it does

A second Chrome MV3 extension (`MN OWA Calendar Probe`, separate
extension ID + native-host name from the production synthesis
bridge) injects a content script on `outlook.office.com`. The
content script calls these endpoints with the user's session
cookies:

- `GET /owa/0/api/v2.0/me/calendarview?startDateTime=...&endDateTime=...`
- `GET /owa/0/api/v2.0/me/people?$search=<email>` (title / company /
  department enrichment)
- `GET /owa/0/api/v2.0/me/events/{id}/attachments` (metadata)
- `GET /owa/0/api/v2.0/me/events/{id}/attachments/{att}/$value`
  (raw bytes, base64-encoded for native-messaging transport)

Every response goes to disk in `data/<UTC>-<verb>-<rid>.json` so
breakage shows up as a diffable capture rather than a vague failure.

## Install

### 1. Sideload the extension

1. `chrome://extensions`
2. Toggle **Developer mode** on (top-right).
3. **Load unpacked** -> point at
   `experiments/owa-calendar-probe/extension/`.
4. Confirm the extension ID matches `hllocpegdlgjbneinopdboclkekjljml`.
   The ID is derived from the RSA key embedded in `manifest.json`;
   if it differs, regenerate the key (see "Regenerating the
   extension key" below) and update `relay/install_host.py:
   EXTENSION_ID` to match.

### 2. Register the native-messaging host

From the project root:

```powershell
# Windows
.\.venv\Scripts\Activate.ps1
python experiments\owa-calendar-probe\relay\install_host.py
```

```bash
# Linux dev (smoke testing only -- OWA needs a real Windows browser session)
source .venv/bin/activate
python experiments/owa-calendar-probe/relay/install_host.py
```

The script:
- Writes a wrapper (`.cmd` on Windows, `.sh` elsewhere) that invokes
  the current Python interpreter on `relay/native_host.py`.
- Writes the Chrome native-messaging manifest pointing at that
  wrapper, with `allowed_origins` pinned to the probe extension ID.
- (Windows) Adds HKCU keys under both Chrome and Edge
  `NativeMessagingHosts`.

To uninstall: `python relay/install_host.py --uninstall`.

### 3. Launch the relay GUI

```bash
python experiments/owa-calendar-probe/relay/probe_app.py
```

By default, email local-parts are redacted in every capture file +
bridge log line. To capture raw addresses (you're debugging
attendee enrichment and need to confirm the field shape):

```bash
python experiments/owa-calendar-probe/relay/probe_app.py --no-redact
```

Or set the env var: `MN_PROBE_KEEP_EMAILS=1`.

### 4. Drive a fetch

1. Make sure you're signed in to `outlook.office.com` in Chrome.
2. In the relay GUI, click **Fetch today** (or **Fetch next 7 days**).
3. The events table populates from the OWA response.
4. Select a row, click **Resolve attendees for selected** to fire a
   `people.lookup` per attendee.
5. Select a row with attachments, click **Pull attachments for
   selected** to download up to 5 attachments to
   `data/attachments/`.

The bridge log (bottom pane) tails every event in real time.

## Sending captures back to Aaron / claude

The **Copy log bundle...** button packages the last 50 capture
JSONs + `bridge.log` + `extension-manifest.json` into a single zip.
Drop the zip path in chat and I can diagnose what OWA actually
returned without needing access to the live tenant.

## Layout

```
experiments/owa-calendar-probe/
  README.md
  extension/                    # MV3 extension, sideloaded
    manifest.json               # name "MN OWA Calendar Probe"
    background.js               # service worker; native-messaging port
    content/
      common.js                 # owaFetch helper + logging
      owa-probe.js              # verb dispatcher on outlook.office.com
    popup.html / popup.js
    icons/                      # PROBE-tagged, distinct from prod
  relay/                        # PyQt6 relay app (freestanding)
    __init__.py
    probe_app.py                # GUI entry point
    bridge.py                   # loopback TCP server (single peer)
    native_host.py              # Chrome stdio <-> TCP bridge
    install_host.py             # one-shot installer / uninstaller
    capture.py                  # disk capture + email redaction
    parser.py                   # OWA JSON -> ProbeMeeting dataclass
    paths.py                    # filesystem layout helpers
    protocol.py                 # length-prefixed JSON framing
  tests/
    test_owa_payload_shape.py   # parser + redaction tests
    fixtures/                   # redacted JSON snapshots
  data/                         # gitignored; bridge.json + captures live here
    .gitkeep                    # placeholder so the dir exists in checkout
```

## Verbs

| Verb | Params | Notes |
| --- | --- | --- |
| `calendar.fetch` | `start_iso`, `end_iso` (ISO-8601 UTC) | $top=100, ordered by start |
| `people.lookup` | `email` | enrichment from /people endpoint |
| `attachments.list` | `event_id` | metadata only |
| `attachments.fetch` | `event_id`, `attachment_id` | base64-encoded body |

The content script wraps responses uniformly:

```json
{
  "ok": true,
  "status": 200,
  "url": "...",
  "body": { ... },
  "headers": { "content-type": "..." },
  "owa_build": "16.3000.123",
  "error": ""
}
```

## Tests

```bash
python -m pytest experiments/owa-calendar-probe/tests/ -v
```

15 tests, all pure-Python (no Qt, no network, no Chrome). They
exercise the parser against the redacted fixtures + the
redaction helper itself.

When OWA breaks the API surface, the workflow is:

1. Capture a fresh response in `data/` via the GUI.
2. Manually copy + redact it into `tests/fixtures/`.
3. Update the parser to handle the new shape.
4. Make sure the tests pass against both the old fixture (if shape
   is compatible) or update the fixture to the new shape.

## Regenerating the extension key

If you need to rebuild the RSA key + extension ID (lost the key,
key compromised, whatever):

```bash
openssl genrsa -out /tmp/probe-key.pem 2048
openssl rsa -in /tmp/probe-key.pem -pubout -outform DER | base64 -w 0
```

Embed the base64 string into `extension/manifest.json` as the `key`
field. Then derive the extension ID:

```python
import base64, hashlib
key_b64 = "MIIB..."  # whatever you just generated
raw = base64.b64decode(key_b64)
digest = hashlib.sha256(raw).digest()[:16]
ext_id = "".join(chr(ord("a") + (b >> 4)) + chr(ord("a") + (b & 0xF)) for b in digest)
print(ext_id)
```

Update `relay/install_host.py: EXTENSION_ID` to match. Re-run
`install_host.py` so the native-messaging manifest's
`allowed_origins` reflects the new ID.

## Open questions for the eventual prod merge

These are not blockers for the probe; they're notes for the merge
plan:

- **Attendee enrichment fidelity.** `/people` returns title / company
  / department for *tenant-resolved* contacts. For external invitees
  (vendors), only display name + email come back. The existing COM
  path also degrades on externals, so this should be on parity, but
  needs real-data confirmation.
- **Service-worker lifetime.** MV3 service workers can be killed by
  Chrome between requests. The probe re-establishes the native port
  on every alarm tick (30s) + on the next `onStartup` / `onInstalled`
  event, mirroring the prod synthesis bridge.
- **Tab requirement.** The content script needs *some* OWA tab open
  (we open one if none exists). If the user has zero OWA tabs and
  Chrome isn't running, the probe can't fire. The COM path didn't
  have this dependency. Acceptable trade-off if the user is going
  to be in OWA for meetings anyway.
- **Toast / meeting-imminent (feature 3 of issue #69).** Deferred to
  a probe v2 -- the simplest extension is to poll `calendar.fetch`
  every 60s and fire a Chrome notification when start - now <=
  2 min. The probe's existing logging is sufficient to test that
  in isolation.

## Eventual merge target

If the probe holds up across a few weeks of use, the merge into
prod looks like:

1. Copy `relay/parser.py` -> a new
   `meeting_notetaker/integrations/owa_calendar.py`. Rename
   `Probe*` dataclasses to align with the existing
   `outlook_calendar.MeetingInfo` field names so the calendar
   picker in `new_session_dialog.py` doesn't need branching.
2. Fold the probe content script into
   `meeting_notetaker/resources/extension/` as a new content-script
   entry on `outlook.office.com`. The prod extension's
   service worker grows a `calendar_fetch` verb alongside
   `synthesize`.
3. In `outlook_calendar.py`, swap the `is_available` /
   `fetch_calendar_range` body for a layered fallback that tries
   COM first, then the OWA-via-extension path, then ICS.
4. Drop the probe (relay + sandbox extension). The native-host
   manifest for `com.meeting_notetaker.probe` gets unregistered.

The smaller the API surface the probe exposes (calendar / people /
attachments), the easier this merge is. Resist the temptation to
grow probe verbs that the prod app won't end up calling.
