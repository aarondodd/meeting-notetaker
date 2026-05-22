"""LLM target metadata.

The app side only needs to know which content script the extension
should load for a given target -- the heavy lifting (DOM selectors,
streaming-complete detection, response scrape) lives in the extension.
Keeping the app-side surface this small means swapping in M365 Copilot
later is just adding a new ``LLMTarget`` instance and a matching
``content/copilot.js`` (the content script for Copilot is already
scaffolded as a stub).

Aaron's stated approved targets are Claude.ai and M365 Copilot; both
are listed here. Only ``claude`` ships wired up in v0.6.3; ``copilot``
is plumbed (settings dropdown entry, target string round-trip) but the
extension's content script for Copilot is a stub that returns
``not_implemented``.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LLMTarget:
    """Description of an approved web LLM that the extension can drive."""

    key: str               # serialized into config.synthesis.llm_target
    label: str             # what the Settings dropdown shows
    new_chat_url: str      # URL the extension navigates to for a fresh chat
    domain_match: str      # pattern the content script registers against (manifest match)
    enabled: bool = True   # show in dropdown but reject the Send if False
    implemented: bool = True  # if False, "Send to <label>" surfaces "not yet implemented"


CLAUDE = LLMTarget(
    key="claude",
    label="Claude.ai",
    new_chat_url="https://claude.ai/new",
    domain_match="*://claude.ai/*",
    enabled=True,
    implemented=True,
)


COPILOT = LLMTarget(
    key="copilot",
    label="Microsoft 365 Copilot",
    # M365 Copilot's web entry point. The actual flow may need adjustment
    # when the Copilot content script lands; this URL is the seed the
    # extension navigates to when target=copilot.
    new_chat_url="https://m365.cloud.microsoft/chat",
    domain_match="*://m365.cloud.microsoft/*",
    enabled=True,
    implemented=False,  # content script is a stub in v0.6.3
)


ALL_TARGETS: tuple[LLMTarget, ...] = (CLAUDE, COPILOT)


def get_target(key: str) -> LLMTarget:
    """Look up by config key. Raises ValueError on unknown keys so a
    typo in config.toml surfaces loudly instead of silently falling
    back to Claude."""
    for target in ALL_TARGETS:
        if target.key == key:
            return target
    raise ValueError(
        f"unknown LLM target {key!r}; expected one of "
        f"{tuple(t.key for t in ALL_TARGETS)}"
    )
