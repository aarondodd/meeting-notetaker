# OWA Calendar Probe

Experiment for [issue #69](https://github.com/aarondodd/meeting-notetaker/issues/69)
Option C: replace the deprecating Outlook COM path with a Chromium
extension that reads OWA's calendar API and forwards results over
native messaging.

**This is sandbox code.** Nothing here imports from
`meeting_notetaker/`, nothing here is wired into the prod app, and
the bundled installer never sees it. The branch
`experiment/owa-calendar-probe` carries it; merging is gated on
proving the OWA payload shape is sufficient for the existing
`MeetingInfo` consumers.

## Validated flow (2026-05-31)

Confirmed working end-to-end on Linux Edge against a personal M365
tenant with five "Test N" calendar entries, including a cross-
tenant attendee (an FHB email on a different M365 tenant).

```
Edge service worker (probe extension)
  |
  +-- chrome.runtime.connectNative("com.meeting_notetaker.probe")
  |
  v
native_host.py (stdio bridge, spawned by Edge per connectNative call)
  |
  +-- 127.0.0.1 TCP loopback
  |
  v
relay.Bridge (single peer, token-authed)
  |
  +-- request: {type: "owa_request", verb: "bg-fetch-calendar", ...}
  |
  v
background.js: dispatchOwaRequest
  |
  +-- chrome.scripting.executeScript({world: "MAIN", func: harvestTokens})
  |       walks the OWA tab's localStorage for MSAL access tokens
  |       and returns every JWT we can find
  |
  +-- For each Outlook-audienced JWT:
  |       fetch(https://outlook.office365.com/api/v2.0/me/calendarview...)
  |       with "Authorization: Bearer <jwt>"
  |       <-- BG service worker context bypasses page CSP + the OWA SW
  |       <-- host_permissions handles CORS
  |
  +-- First 200 + JSON wins. Body is PascalCase (Outlook REST v2.0).
  |
  v
sendToApp -> native_host stdio -> relay -> capture.py -> parser.parse_calendarview
  -> ProbeMeeting dataclass with subject, start, end, attendees, location,
     online_meeting_url, web_link.
```

The verb dispatch matrix lives in `extension/background.js`:

| Verb | What it does | Validated 2026-05-31? |
| --- | --- | --- |
| `bg-fetch-calendar` | Read MSAL token, fetch /api/v2.0/me/calendarview | Yes - 5 events incl. cross-tenant attendee |
| `bg-fetch-people` | Same auth path, /api/v2.0/me/people?$search=... | Yes - both OrganizationUser + ImplicitContact paths |
| `list-tabs` | Diagnostic: enumerate matching tabs + granted perms | Yes |
| `diagnose-main`, `inspect-storage`, `try-all-tokens` | Diagnostics used during the validation process | Yes (kept for future API rotations) |

### Why "BG-SW fetch", not content-script fetch

The probe's first three iterations all tried fetches from within a
content script (page MAIN world, after token grab). All three
failed:

1. **No Authorization header** (`credentials: 'include'` only) -> 401
   from the API with `WWW-Authenticate: Bearer client_id="00000002-
   0000-0ff1-ce00-000000000000"` (Exchange Online's well-known
   first-party app).
2. **With Authorization header, same-origin fetch** -> "Failed to
   fetch" (page-level CSP / OWA's own service worker rejects
   bearer-authed fetches from non-MSAL code).
3. **With Authorization header, cross-origin fetch** -> "Failed to
   fetch" (CORS preflight from outlook.cloud.microsoft to
   outlook.office365.com doesn't satisfy the Authorization-header
   constraint).

The fix: do the fetch from the extension's BG service worker, not
the page. The BG SW is exempt from the OWA tab's CSP and SW, and
the extension's `host_permissions` provides CORS-bypass. This is
the architecturally correct MV3 pattern for an extension reading
backend APIs the page's CSP would normally block.

### Token discovery

OWA does not mint Exchange-audience tokens to the well-known
Exchange Online client_id (`00000002-...`). Instead, the OWA SPA's
own MSAL client (`9199bf20-a13f-4107-85dc-02114787ef48` on the
tenants tested) holds delegated tokens with various audiences. We
filter by audience containing `outlook.office` and use the first
one that succeeds against the API. Tokens with audiences pointing
at `graph.microsoft.com`, `clients.config.office.net`,
`presence.teams.microsoft.com`, etc. are filtered out -- they're
present in localStorage but won't authenticate against the
calendar API.

### Endpoint surface findings

| URL | Result |
| --- | --- |
| `outlook.cloud.microsoft/owa/0/api/v2.0/...` | 200 + `text/html` SPA shell (cloud.microsoft is SPA-only) |
| `outlook.office.com/owa/0/api/v2.0/...` | 200 + `text/html` SPA shell (legacy path retired) |
| `outlook.office365.com/api/v2.0/me/calendarview` | **200 + JSON** (Outlook REST v2.0, PascalCase) |
| `outlook.office365.com/api/v2.0/me/people` | **200 + JSON** (same shape, $search query) |
| `graph.microsoft.com/v1.0/me/calendarview` | Requires Graph-audience token (not minted on calendar load) |

### Response shape

Outlook REST v2.0 returns **PascalCase** JSON:

```json
{
  "value": [{
    "Id": "AAMk...",
    "Subject": "Test 1",
    "Start": {"DateTime": "2026-05-31T22:30:00.0000000", "TimeZone": "UTC"},
    "End":   {"DateTime": "2026-05-31T23:00:00.0000000", "TimeZone": "UTC"},
    "Attendees": [{
      "Type": "Required",
      "EmailAddress": {"Name": "Aaron Dodd", "Address": "***@fhb.com"}
    }],
    "Organizer": {"EmailAddress": {"Name": "...", "Address": "..."}},
    "Location": {"DisplayName": "Microsoft Teams Meeting"},
    "OnlineMeeting": {"JoinUrl": "https://teams.microsoft.com/..."},
    "HasAttachments": false,
    "IsOnlineMeeting": true,
    "WebLink": "https://outlook.office365.com/owa/?itemid=..."
  }]
}
```

If we ever shift to Graph (camelCase: `subject`, `start.dateTime`,
`attendees[].emailAddress`), the parser is dual-shape-aware -- it
sniffs each event for `Subject` vs `subject` and dispatches. No
caller-side changes needed.

### Attendee enrichment parity

| Resolution | Fields returned |
| --- | --- |
| Tenant member (`PersonType.Subclass: OrganizationUser`) | DisplayName, GivenName, Surname, UserPrincipalName, IMAddress, ScoredEmailAddresses, Phones |
| External invitee (`Subclass: ImplicitContact`) | DisplayName + ScoredEmailAddresses only |

JobTitle, CompanyName, Department, OfficeLocation may be null even
for OrganizationUser if the tenant doesn't populate them (personal
tenants often don't; enterprise tenants like FHB will). The probe
preserves null -> empty string in the dataclass so downstream code
doesn't need to special-case None. This is identical to the COM
path's behavior on externals.

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

Hard questions are now answered. Soft ones remain:

- **Token lifetime.** Outlook-audience access tokens cached in OWA's
  localStorage have a typical 60-90 minute lifetime. If the user
  closes Edge / kills the OWA tab for an hour, the cached token
  expires. MSAL.js refreshes silently when OWA itself makes a call,
  but the probe doesn't trigger that refresh. Two options: (a) the
  prod calendar pull only runs when an OWA tab is active, OR (b) we
  trigger a forced silent refresh by injecting a script that calls
  the OWA SPA's MSAL instance's `acquireTokenSilent()`. Defer until
  empirical token-expiry breakage shows up.
- **Service-worker lifetime.** MV3 service workers can be killed by
  Chrome between requests. The probe re-establishes the native port
  on every alarm tick (30s) and on `onStartup` / `onInstalled`,
  mirroring the prod synthesis bridge. Validated.
- **Tab requirement.** The content script needs *some* OWA tab open
  with a valid auth state. The COM path didn't have this dependency,
  but the trade-off is acceptable: the user is going to be in OWA
  for meetings anyway, and we can document "Keep an Outlook tab open
  for calendar features" as the install caveat.
- **Toast / meeting-imminent (feature 3 of issue #69).** Deferred to
  a probe v2. The simplest extension is to poll `bg-fetch-calendar`
  every 60s in the BG SW and fire a Chrome notification when
  start - now <= 2 min. The existing logging + parser are sufficient
  to test this in isolation.
- **Attachment binary fetch.** The `attachments.fetch` verb in the
  content script still does the fetch in MAIN world and would hit
  the same CSP wall as calendar fetches did. Needs the same BG-SW
  refactor before it'll work. Lazy-fix when the prod merge actually
  needs attachments (it's a per-meeting flow that COM did handle).

## Eventual merge target

If the probe holds up across a few weeks of use, the merge into
prod looks like:

1. Copy `relay/parser.py` -> a new
   `meeting_notetaker/integrations/owa_calendar_rest.py`. Rename
   `Probe*` dataclasses to align with the existing
   `outlook_calendar.MeetingInfo` field names so the calendar
   picker in `new_session_dialog.py` doesn't need branching.
2. Add `outlook.cloud.microsoft`, `outlook.office.com`,
   `outlook.office365.com`, and `graph.microsoft.com` to the prod
   extension's `host_permissions` (and `outlook.cloud.microsoft`
   to `content_scripts.matches` for the token-harvest hook).
3. Add the BG-SW handlers (`bg-fetch-calendar`, `bg-fetch-people`)
   into the prod extension's `background.js` alongside the
   existing `synthesize` verb. The two verbs are independent so
   they coexist without interference.
4. In the prod app, add a layered fallback in `outlook_calendar.py`:
   - `is_available` returns True if EITHER COM is reachable OR the
     synthesis bridge is connected to an extension whose
     `host_permissions` covers `outlook.office365.com`.
   - `fetch_calendar_range` tries COM first (lower-latency,
     no browser dependency); falls back to the extension path on
     COM failure (the New Outlook case).
   - `fetch_imminent_meetings` likewise.
5. Drop the probe (relay + sandbox extension). The native-host
   manifest for `com.meeting_notetaker.probe` gets unregistered.

Two design notes for the merge:

- **Use the existing synthesis bridge socket**, not a separate one.
  The prod extension already has `com.meeting_notetaker.bridge`
  wired in; adding `bg-fetch-calendar` to the same bridge keeps the
  install surface flat. The probe's separate `com.meeting_notetaker.
  probe` host name is sandbox-only.
- **Keep the parser dual-shape** even at merge time. The Graph path
  may become reachable later (if MS rotates the SPA to mint
  Graph-audience tokens by default), and the dual-shape parser
  lets us silently shift endpoints with no caller changes.

The smaller the API surface the probe exposes (calendar / people /
attachments), the easier this merge is. Resist the temptation to
grow probe verbs that the prod app won't end up calling.
