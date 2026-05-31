# Diagnostic Playbook

The probe is going to break. Microsoft rotates OWA's API surface,
auth model, hostnames, and SPA build cadence on its own schedule.
When the probe stops working, this playbook is the structured
ladder for figuring out what changed.

Each verb in `drive_probe.py` exists for one diagnostic step.
Climbing the ladder in order narrows the failure to one layer
before you start coding.

## Setup (re-validate these first if anything looks weird)

| Check | Command | Expected |
| --- | --- | --- |
| Probe extension still loaded in your browser | Open `edge://extensions` or `chrome://extensions` | "MN OWA Calendar Probe" present, enabled |
| Extension ID still matches | Same page | `hllocpegdlgjbneinopdboclkekjljml` |
| Native-messaging host registered | `python relay/install_host.py` (re-runs cleanly any time) | Prints `manifests: [...]` paths + on Windows, `registry_paths: [...]` |
| Bridge can listen | `python relay/drive_probe.py wait-only --wait 30` | Logs `bridge state -> connected` within ~30s |

If any of those fail, fix them before trying anything else --
they're prerequisites for every verb below.

## The diagnostic ladder

Run these in order. Stop at the first one that fails. The failure
mode of each verb pinpoints which layer broke.

### Step 1: `list-tabs` -- can the extension see your browser tabs?

```
python relay/drive_probe.py list-tabs --wait 30
```

What it does: returns every tab the extension has visibility into
plus the granted permissions.

What to look for:
- `granted host_permissions` should contain
  `*://outlook.cloud.microsoft/*`, `*://outlook.office.com/*`,
  `*://outlook.office365.com/*`, and `*://graph.microsoft.com/*`.
  If any are missing, the browser is holding back permissions --
  reload the extension and **explicitly accept** the permission
  prompt.
- `outlook tabs matched` count should equal the number of OWA
  tabs you actually have open. If it's lower, the matcher missed
  one -- likely a new OWA hostname that we haven't added yet.
  Check the `all tabs` list for any URL containing
  `outlook.` that isn't being matched.

Possible reactions:
- "outlook tabs matched: 0" with an OWA tab clearly visible:
  Microsoft moved OWA to a new origin. Add it to
  `extension/manifest.json` `host_permissions` and
  `content_scripts.matches` and to `background.js` `listOwaTabs`.

### Step 2: `inspect-storage` -- can the extension read MSAL tokens?

```
MN_PROBE_KEEP_EMAILS=1 python relay/drive_probe.py inspect-storage --wait 30
```

What it does: dumps every `accesstoken` key from the OWA tab's
`localStorage`, including the audience (`aud`) the token was
issued for.

What to look for:
- `localStorage entries: N` should be in the 50-200 range for a
  warm OWA tab. If it's 0-5, the tab hasn't completed MSAL boot --
  refresh the OWA tab and try again.
- `total accesstoken keys` should be 5-25. Each line shows the
  `target` field which is the OAuth2 scope MSAL is caching.
- **At least one** entry must have a `target` or `aud` containing
  `outlook.office`. If none do, the SPA hasn't called any
  Outlook-backed API yet. **Navigate the OWA tab to `/calendar/`
  view** (not `/mail/`) -- that forces the SPA to mint an
  Outlook-audience token.

Possible reactions:
- "0 accesstoken keys": MSAL store layout changed. Check what keys
  ARE in localStorage (the verb prints `all_keys`). Look for new
  patterns like `msal.cache.<...>` or whatever replaced
  `accesstoken`.
- "No outlook.office in any target": The SPA's token-acquire flow
  changed. Maybe MS rolled out a new resource scope name. Run
  step 3 to see what audience the server is *demanding*.

### Step 3: `diagnose-main` -- which endpoint does the server respond to?

```
MN_PROBE_KEEP_EMAILS=1 python relay/drive_probe.py diagnose-main --wait 30
```

What it does: from the OWA tab's MAIN-world, hits 4 candidate
calendar endpoints with bare cookies (no Authorization header).
Returns each response's status, content-type, and -- crucially --
`WWW-Authenticate` header.

What to look for:
- All endpoints SHOULD return 401 (we're not authenticated yet at
  this step). 401 is good news -- it means the path exists and
  the server is willing to talk if we add proper auth.
- The `WWW-Authenticate` header on a 401 names the audience the
  server expects: e.g.
  `Bearer client_id="00000002-0000-0ff1-ce00-000000000000"`. Match
  that against the audiences from step 2.
- A 200 + `text/html` content-type is a *dead path* -- the server
  is falling back to the SPA shell because that path doesn't
  exist anymore. Drop it from the candidate list.
- A 200 + `application/json` content-type at this step is a
  miracle: the endpoint accepts cookies alone. (Unlikely in 2026+
  but worth checking.)

Possible reactions:
- All endpoints return 200 + HTML: every candidate URL is dead.
  Open OWA's network tab in DevTools, look at what API calls the
  SPA itself is making, copy one of those paths into
  `background.js` as a new candidate.
- WWW-Authenticate names a new audience: add a candidate endpoint
  for it in `runDiagnoseMain` and re-run.

### Step 4: `try-all-tokens` -- which (token, endpoint) pair actually works?

```
MN_PROBE_KEEP_EMAILS=1 python relay/drive_probe.py try-all-tokens --wait 45 --timeout 60 --days 2
```

What it does: Cartesian product of every JWT in localStorage
crossed with three calendar endpoints, fired from MAIN-world with
`Authorization: Bearer <jwt>`.

What to look for:
- Every attempt returns "Failed to fetch" with no status code:
  page CSP / OWA service worker is blocking authed fetches.
  This is the expected failure mode for MAIN-world fetches with
  Authorization headers. Skip to step 5.
- One attempt returns 200 + JSON: you found a (token, endpoint)
  combination that works from the page. Note the audience.
- Some return 401 with `WWW-Authenticate`: the token is wrong
  audience for that endpoint. Cross-reference with step 3's
  result.

Possible reactions:
- All "Failed to fetch": skip ahead to step 5.
- One works with a non-standard audience: add that audience to
  the prod token-selection filter in `harvestExchangeTokens`.

### Step 5: `bg-fetch` -- can the BG service worker do it?

```
MN_PROBE_KEEP_EMAILS=1 python relay/drive_probe.py bg-fetch --wait 30 --timeout 60 --days 2
```

What it does: same as step 4, but the fetch runs from the
extension's BG service worker context instead of the page MAIN
world. BG SW is exempt from page CSP and OWA's service worker;
`host_permissions` gives CORS-bypass.

What to look for:
- `WINNER: outlook.office365.com_v2 -- parsed N meetings`: green
  light, the full flow works. Parse the events; the prod
  integration is feasible.
- `no_token_endpoint_combination_succeeded`: token discovery
  worked but no token had the right audience for any endpoint.
  Re-check step 3's `WWW-Authenticate` output -- the server is
  asking for an audience we don't have. May need to add a
  fallback path that triggers MSAL.acquireTokenSilent via
  injected page-context script.
- 401 across all combinations even though tokens were sent: the
  audience filter is too strict. Loosen the
  `audience_match` callbacks in `runBgFetchCalendar`.

## When everything passes but events look wrong

If `bg-fetch` returns events but they don't parse correctly into
`ProbeMeeting`:

1. Inspect the raw capture file: `experiments/owa-calendar-probe/
   data/<latest>-bg-fetch-calendar-*.json`.
2. Compare the event shape against
   `tests/fixtures/calendarview-outlook-rest-redacted.json`.
3. If keys changed (e.g. `Subject` became `eventSubject`), update
   `relay/parser.py:_parse_event` to handle the new key alongside
   the existing one via `_pick`.
4. Add a regression test using a redacted sample from the new
   shape.

## When the bridge connects but a verb fails with `unknown_verb`

The extension's BG service worker has stale code. Reload the
extension at `edge://extensions` and try again. If the verb is
new (you just added it), make sure `dispatchOwaRequest` in
`background.js` dispatches it before the fall-through to
content-script forwarding.

## When the extension reconnects every few seconds

`connectNative` is failing on Chrome's side. Two common causes:

1. **Native-messaging manifest missing.** Re-run `install_host.py`.
2. **Wrapper script not executable** (POSIX only). Check
   `experiments/owa-calendar-probe/data/native_host.sh` is mode
   `0755`. `install_host.py` should set this automatically.

Check `data/bridge.log` for `bridge accept` lines spaced suspiciously
close together (multiple per second).

## When everything else is right but capture files don't appear

Two things to verify:

1. **`data/` is writable.** Default location is inside the
   experiment folder; if you set `MN_PROBE_DATA_DIR`, check that
   path has write permission.
2. **The verb actually completed.** `relay/capture.py` only writes
   a per-request capture when the response arrives -- if the verb
   timed out before the extension responded, there'll be no file.
   Check the bridge log for an `in` line for the request_id.

## Reusable validation checklist

Run this end-to-end before declaring "the probe still works after
the Microsoft change of the week":

```
# 1. Setup
python relay/install_host.py
# Reload extension at edge://extensions, accept permission prompts

# 2. Tab visibility
python relay/drive_probe.py list-tabs --wait 30

# 3. Token discovery
MN_PROBE_KEEP_EMAILS=1 python relay/drive_probe.py inspect-storage --wait 30

# 4. End-to-end
MN_PROBE_KEEP_EMAILS=1 python relay/drive_probe.py bg-fetch --wait 30 --days 2

# 5. People lookup
python relay/drive_probe.py bg-people --email <a@test-email> --wait 30
```

If all five succeed, the probe is healthy. If any fail, climb the
ladder from step 1 of the matching diagnostic section above.

## When to update fixtures

Update `tests/fixtures/calendarview-outlook-rest-redacted.json`
or `tests/fixtures/people-outlook-rest-redacted.json` ONLY when:

1. A new field appears in the real response that the parser will
   need to surface (e.g. a new "CalendarEventClassification" type).
2. A field's shape changes (e.g. `Start.DateTime` becomes
   `Start.dateTimeUTC`).
3. A new PersonType subclass shows up (e.g. `ExternalContact`).

Do NOT update fixtures just because Microsoft added a new ignored
field. Keep the fixture minimal -- only what the parser asserts
on. Bloated fixtures hide real regressions.

When updating, scrub aggressively before committing:
- Email local parts -> `***@<domain>`
- Org-identifying domains -> `example-org.com` / `example-user.com`
- User + tenant UUIDs -> `fixture-user-id` / `fixture-tenant-id`
- Phone digits -> `5555555555`
- Real names -> `Sample User`
- Teams/SharePoint URLs -> `redacted-context`

## Saved invocations for fast iteration

```bash
# Most useful single command -- proves end-to-end auth + fetch + parse:
MN_PROBE_KEEP_EMAILS=1 python relay/drive_probe.py bg-fetch --days 2

# When the response shape changed -- shows raw body:
cat experiments/owa-calendar-probe/data/<latest>-bg-fetch-calendar-*.json \
  | python3 -c "import json, sys; d=json.load(sys.stdin); print(json.dumps(d['body']['winner']['body']['value'][0], indent=2))"

# When tab selection is wrong:
python relay/drive_probe.py list-tabs --wait 10

# When you just want to verify the bridge / native messaging path:
python relay/drive_probe.py wait-only --wait 30
```

## Investigation log (chronological)

A reference of past investigation runs, what each one taught us,
and how the probe evolved. Update this section every time a real
diagnosis lands.

### 2026-05-31 -- initial validation

* Found OWA on the new `outlook.cloud.microsoft` host (the unified
  Outlook hostname Microsoft is rolling out to replace office.com
  and office365.com). Cloud.microsoft is SPA-only; all API calls
  resolve to outlook.office365.com.
* Found that MAIN-world fetch with Authorization header fails
  "Failed to fetch" -- page CSP + OWA's service worker. The fix
  is to do the fetch from the BG service worker (extension
  context, host_permissions handles CORS, exempt from page CSP/SW).
* Found that OWA's MSAL cache does NOT contain a token issued to
  the well-known Exchange Online client_id
  (`00000002-0000-0ff1-ce00-000000000000`). The SPA's own client
  (`9199bf20-...` on the tested tenant) holds delegated tokens
  with various target audiences. Filter by audience containing
  `outlook.office` rather than by client_id.
* Found that the working endpoint is
  `https://outlook.office365.com/api/v2.0/me/calendarview` --
  returns 200 + JSON in PascalCase (Outlook REST v2.0). The
  modern `outlook.cloud.microsoft/owa/0/api/v2.0/...` and
  `outlook.office.com/owa/0/api/v2.0/...` paths both return the
  SPA shell HTML.
* Validated `people` enrichment: in-tenant addresses resolve as
  `PersonType.Subclass: OrganizationUser`; cross-tenant addresses
  resolve as `ImplicitContact` with name + email only.
