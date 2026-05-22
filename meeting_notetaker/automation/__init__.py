"""Synthesis automation: route the synthesis prompt through a Chrome
extension that drives an approved web LLM (Claude.ai / M365 Copilot).

The app stays in charge: it renders the same prompt the Generate button
renders, hands it to the extension via Chrome native messaging, the
extension pastes into the LLM chat and scrapes the streamed response,
and the result lands in the synthesis tab through the same write path
the Paste Response Back button uses.

No audio or transcript ever leaves the user's machine without going
through the browser tab the user can see -- the LLM contract is the
same as the manual copy/paste flow, just with the clicks removed.
"""
