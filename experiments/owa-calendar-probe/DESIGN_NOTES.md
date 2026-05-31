# Design Notes for the Prod Merge

This is the "when we merge to prod, build it this way" doc.
DIAGNOSTICS.md is the "when it breaks, fix it here" doc.

The probe proves the Chromium-extension path is **technically
feasible**. This document captures the trade-offs vs the COM path
and the UX work required to make the extension path **actually
seamless** for users rather than just "technically working."

The prod merge keeps COM and the Chromium extension as
**coexisting** Outlook integration modes, with the user explicitly
choosing in Settings. Neither is fallback-only; both are
first-class. The user's choice should hide all the differences
documented below.

---

## Realistic downsides of the Chromium-extension path

Listed roughly in order of how often they'll bite a typical user.

### 1. Browser-must-be-running

COM doesn't care -- Outlook can be closed and COM will spin it up
on demand. The extension path needs the user's Chromium browser
running with an authenticated OWA tab somewhere. If they close
all browser windows for the night, calendar integration goes
dark until the morning's first browser launch + OWA sign-in.

### 2. MSAL token expiry (60-90 min lifetime)

Tokens cached in OWA's `localStorage` have a sliding lifetime.
OWA refreshes them silently *when OWA itself makes an API call*.
If the OWA tab sits idle while the user does other work, the
token expires and the next fetch 401s. The probe today just
fails on expiry -- doesn't refresh.

### 3. Outlook-audience token isn't always cached

The SPA only mints an Outlook-audience token after the user
navigates somewhere that needs one -- typically the Calendar
view. A user who only uses OWA for Mail will have Outlook tokens
for mail-side features but not necessarily for /me/calendarview.

### 4. Silent failures by default

If the BG service worker dies mid-fetch, or the OWA tab gets
closed, or a token expires -- the prod app just sees "no
calendar." User has no in-app indication that something specific
is wrong, just that the feature stopped working. COM gives crisp
errors ("RPC server is unavailable"); the extension path's error
surface is muddier without explicit instrumentation.

### 5. Auth-token harvesting smells funky to security folks

We read JWT access tokens out of `localStorage`. Those tokens
grant access to mail + calendar + contacts at full SPA scope.
We use them only for calendar reads, but an audit-minded IT
admin or Edge SmartScreen heuristic could flag the extension.
Less of a concern for personal users; could be friction for
enterprise rollout.

### 6. Conditional Access Policies (enterprise)

Some enterprise tenants enforce CAPs that detect "unusual access
patterns." A bearer token used from a service-worker context the
user isn't actively clicking in might trip CAP -- resulting in
forced re-auth, MFA prompts, or blocked calls. Unknown until
tested against a real enterprise tenant, but it's a real risk
worth flagging up front.

### 7. Latency vs COM

COM is sub-millisecond local IPC. The extension path is
relay -> native_host -> stdio -> BG SW -> executeScript ->
localStorage walk -> BG fetch -> network round-trip. Typical:
200-500ms. Fine for "list today's meetings" but tighter for
"fire toast 2 min before start" if we ever want sub-second
precision.

### 8. Multi-account confusion

Two OWA tabs with different signed-in accounts -> our
`listOwaTabs` picks the first one. May pull the wrong calendar.
Personal-vs-work users are the most likely to hit this. The prod
config should let users name the expected account, and the BG
SW should prefer the tab matching that account.

### 9. Browser-profile separation

Browser profiles each have their own extension install + their
own NMH registration. The probe works in the profile where it
was installed; if the user mostly uses a different profile,
nothing happens. Profile-switching users will need explicit
docs.

---

## UX seamlessness plan

The right mental model: **the OWA tab is a "token vault," not a
"live source."** Decouple the UI from live fetches. Treat the
extension path as a background sync mechanism that fills a local
cache, plus refresh-on-demand for when staleness actually matters.

### A. Local calendar cache -- the single biggest win

Cache fetched events in the prod app's SQLite store (next to
`models/session.py`). Background re-fetch every N minutes when
the extension is healthy. The UI always reads from the cache.
The toast scheduler operates from the cache.

This eliminates 90% of the perceived brittleness: even if the
extension is offline for an hour, the user still sees today's
calendar. We stamp "last refreshed Xm ago" so they know how
fresh it is.

The cache also makes the prod app's New Session dialog instant
-- no network round-trip on every dialog open.

### B. Tray status indicator + Test Connection page

Tray icon shows a colored dot:

| Color | Meaning |
| --- | --- |
| Green | Last fetch <10 min ago, token has >15 min left |
| Yellow | Stale (10-60 min) OR token expiring soon. Auto-retry in progress. |
| Red | Offline, expired, browser closed, no extension. |

Clicking the tray opens a Settings page showing exactly what's
wrong, with a single button per issue:

```
Calendar source: Chromium extension
[X] Extension responding         (last ping 5s ago)
[X] Outlook tab open             outlook.cloud.microsoft/calendar/view/week
[X] Token cached                 expires in 47 min
[X] Last fetch: 2 min ago        5 events for today

[Refresh now]   [Switch to Classic COM]   [Disable]
```

vs the failure case:

```
Calendar source: Chromium extension
[X] Extension responding         (last ping 5s ago)
[ ] Outlook tab open             no Outlook tab found  [Open Outlook]
[!] Token cached                 expired 12 min ago    [Open Outlook + sign in]
[ ] Last fetch: 16h ago          using cached data

[Refresh now]   [Switch to Classic COM]   [Disable]
```

### C. Auto-trigger token refresh before expiry

Add a verb `refresh-token` that injects a small MAIN-world
script using OWA's own MSAL instance. The probe-side
investigation is needed first -- need to find what global the
new OWA exposes its MSAL instance on (likely something under
`window.Owa` or `window.msalInstance`).

```javascript
// MAIN-world snippet -- runs inside the OWA page context
const inst = window.msalInstance || ...; // discover
const acct = inst.getActiveAccount();
await inst.acquireTokenSilent({
    scopes: ["https://outlook.office.com/.default"],
    account: acct,
});
// Token now refreshed in localStorage. Our BG SW can read it.
```

Schedule this on a BG-SW alarm when the cached token has <10
min left. User never sees an expiry-driven failure.

If `acquireTokenSilent` itself fails (refresh token expired),
fall back to: surface "your sign-in expired" with a button to
open OWA. Don't try to do this silently with login redirects --
user needs to know they had to re-authenticate.

### D. Auto-open background OWA tab (opt-in)

Setting: **"Keep Outlook open in background."** When on, if the
prod app starts and no OWA tab exists, the extension opens one
in a background tab (`chrome.tabs.create({active: false})`,
already supported). User stays signed-in, calendar stays warm,
no manual intervention.

Off by default -- opening tabs unbidden is annoying. But for
users who want frictionless calendar, it's the difference
between "always works" and "I have to remember to open Outlook."

### E. Pre-flight check on New Session dialog open

The integration's primary use case is the New Session dialog
populating today's meetings. Before that UI renders:

1. Check cache freshness. If <5 min old, render from cache
   (instant).
2. If stale, fire a background refresh AND render cache
   immediately + show a spinner near the calendar list. UI
   never blocks on the network.
3. If both cache empty AND refresh fails, show empty state +
   one specific actionable error.

### F. Specific actionable error messages

Each failure mode maps to a single sentence + a single button.
Below is the proposed mapping; the prod `outlook_calendar.py`
should expose `ErrorCode` constants so the UI layer can pattern-
match on them rather than parsing strings.

| Detected state | Message | Button |
| --- | --- | --- |
| No bridge connection | "Outlook extension not responding. Reinstall?" | Run installer |
| No OWA tab | "Open Outlook to sync calendar" | Open OWA |
| Token expired, refresh failed | "Outlook sign-in expired" | Open OWA + sign in |
| 403 with CAP message | "Your IT policy is blocking this fetch" | Help docs |
| 200 + HTML where JSON expected | "Outlook's API changed -- update needed" | Check for update |
| Cache empty + offline | "No internet connection" | (auto-retry, no button) |

The last row in particular -- detecting `200 + text/html` where
we expected `200 + application/json` is the canary for
"Microsoft rotated an endpoint." Surface that as **"the app
needs an update"** rather than letting users think calendar is
broken on their machine. Push them to a fix.

### G. Heartbeat daemon in the BG service worker

Independent of fetch requests, the BG SW writes a heartbeat
record every 5 min to `chrome.storage.session`:

```json
{
  "ts": 1780270000,
  "bridge_connected": true,
  "owa_tabs": ["outlook.cloud.microsoft/calendar/view/week"],
  "token_expires_in_sec": 2400,
  "token_audience": "https://outlook.office.com",
  "last_fetch_at": 1780268000,
  "last_fetch_ok": true,
  "last_fetch_events": 5
}
```

The relay reads this on demand. The Settings page renders it
without making a live probe. The tray icon's color is derived
from these fields. The prod app's tray + settings UI become
synchronous reads with no waiting.

### H. Setup wizard for first-time enable

When the user picks "Chromium extension" in Settings, a 4-step
wizard:

1. **Install extension** -- one-click "Open extensions page"
   + step-by-step instructions; auto-detects when the
   extension shows up.
2. **Register native host** -- one-click button (runs the
   installer's `install_host.py` equivalent).
3. **Sign in to Outlook** -- one-click "Open Outlook" button;
   detects when an authenticated tab appears.
4. **Test connection** -- runs `bg-fetch` against current
   time, shows green check or specific failure.

Mirrors the existing synthesis-automation install wizard. Users
already know the rhythm.

### I. Manual fallback options

For the rare case where even the extension path is broken AND
COM isn't available (user is on New Outlook + IT blocked
something), expose:

- **Cached calendar view-only** -- last fetched events, clearly
  marked as stale.
- **Manual entry** -- the New Session dialog already accepts a
  freeform meeting title; just don't pre-populate the picker.
- **ICS file watch dir** -- user exports their calendar to a
  folder, app parses. (Documented in the original issue
  description as Option A.)

These are *not* automatic fallbacks -- they're "if everything
else is broken, here are the manual paths." The user picks
them explicitly.

---

## End-to-end UX for the user who never thinks about it

```
8am  - User starts laptop, opens browser. Edge restores session.
       OWA tab is open.
       Meeting Notetaker (always running) picks up the extension
       within 30s.
       Background sync pulls today's calendar into local cache.
       Tray icon: green.

8:55 - User opens Meeting Notetaker to start their 9am call.
       New Session dialog renders instantly from cache.
       User picks the 9am Team standup. Recording starts.

9:30 - BG SW notices token is expiring soon. Runs silent refresh
       via MSAL.acquireTokenSilent. User never knows.

12pm - User closes browser to go to lunch. Tray icon: yellow.
       Calendar still works (cache + last fetch was 30 min ago).

1pm  - User reopens browser, OWA tab loads. Tray icon: green
       within 30s. Background sync picks up any new meetings.
```

End-to-end UX for the user when something breaks:

```
9am  - User starts laptop. No browser open yet.
       Meeting Notetaker tray icon: yellow ("Outlook offline").
       Tooltip: "Open Outlook to sync calendar."

       User clicks Tray icon -> Settings -> Calendar source.
       Page shows:
         [X] Extension installed
         [ ] Outlook not running         [Open Outlook]
         [-] Calendar cache: from yesterday (16h old)

       User clicks Open Outlook. OWA loads. Tray icon goes
       green within ~30s.
```

vs today's "no events show up, user has to guess what went
wrong."

---

## Implementation priority for the prod merge

Listed in order. Each item is independently shippable:

1. **Cache layer + tray indicator** -- decouples the UI from
   live fetches. Single biggest UX improvement. Required for
   anything below to feel polished.
2. **Setup wizard** -- one-time-cost item that makes the
   feature actually adoptable.
3. **Heartbeat daemon** -- enables the Settings page +
   tray-icon coloring to be synchronous reads. Cheap to add
   once the cache is in place.
4. **Token refresh** -- eliminates the most common runtime
   failure. Needs the upstream investigation into OWA's
   MSAL global first.
5. **Auto-open background tab** -- opt-in setting. Polishing
   for users who want maximum frictionlessness.
6. **Schema-rotation detection** -- the "200 + HTML where
   JSON expected" surface. Future-proofing against Microsoft
   rotations.
7. **Multi-account support** -- only when the multi-account
   case shows up in real usage. Don't over-engineer.

---

## Out of scope for the prod merge

Things the probe could be extended to do but that aren't
necessary for a useful Outlook integration:

- **Calendar write** (creating meetings from the app). Users
  create meetings in Outlook directly; we just read.
- **Multi-calendar** (shared calendars, group calendars). One
  per user is the 80% case.
- **Recurring-meeting expansion server-side.** Outlook REST
  v2.0 already returns occurrence-expanded events; we don't
  need to do this ourselves.
- **Attachment download.** The probe has the verb but it's
  unused. Add only if a prod feature wants invite attachments.
