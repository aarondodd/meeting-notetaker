# Troubleshooting synthesis automation

When Anthropic changes claude.ai's DOM (they do this on a roughly monthly
cadence), the browser extension's paste, send, and response-scraping
selectors can drift and the synthesis flow silently stalls. Rather than
diagnosing by round-trip ("run this snippet, tell me what you see, now
try this one..."), use the probe.

## The probe

Location: `scripts/probe-claude.js`. Single self-contained file, no
dependencies. Two entry points:

- `mnProbe()` -- read-only. Discovers every DOM surface the extension
  depends on, exercises every known paste primitive against a unique
  tag per primitive, reports which ones actually put text in the
  composer. Does NOT click Send and does NOT use quota. Leaves a few
  throwaway tags in the composer; refresh to clear.
- `mnProbeSend()` -- end-to-end. Runs `mnProbe()`, picks the paste
  primitive that landed a tag, uses it to write a short test prompt
  ("PROBE_xxx: reply with only the single word ACK"), clicks Send,
  and waits up to 60s for the response. Confirms the response scraper
  can locate the assistant message. Uses one small turn of quota per
  run.

Both entry points print a full JSON report to the console and copy it
to the clipboard.

## How to run

1. Open claude.ai in Chrome. Any conversation view with the composer
   visible works (a fresh `claude.ai/new` is fine).
2. Open DevTools (F12), Console tab.
3. Paste the entire contents of `scripts/probe-claude.js`.
4. Run `mnProbe()` (or `mnProbeSend()` for the end-to-end version).
5. Paste the resulting JSON to whoever is debugging.

## What the report tells you

- `composerChosen.matchedSelector`: which extension composer selector
  currently matches. If null, the composer selectors are all stale.
- `editorApi.available`: whether TipTap's Editor instance is reachable
  on the composer DOM node.
- `pastePrimitives.results[].tagLanded`: for each paste approach,
  whether the unique tag actually ended up in the composer text.
  Anything true here is a viable paste path.
- `selectors.sendMatches`: which of the extension's send-button
  selectors currently matches; whether the button is disabled.
- `selectors.assistantMatches` and `selectors.userMatches`: whether
  the response scraper's message selectors are still finding things.
- `copyButtons.samples`: response-level Copy buttons the extension
  might click to read the response.
- `e2e` (end-to-end run only): whether Send actually posted the
  message and whether Claude's response arrived + was locatable.

## Maintaining the probe

The probe is the canonical local truth for "what does the extension
look at on claude.ai". When the extension's own selectors change,
update the constants at the top of `probe-claude.js` (they're literal
copies of the extension's selector arrays). When a new failure mode
surfaces, add a probe function for it -- the file is designed to grow
with the failure modes we hit.
