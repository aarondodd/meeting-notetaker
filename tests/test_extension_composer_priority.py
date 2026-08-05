"""Static regressions for #131 -- composer selector picks ambient
textarea before TipTap div mounts, causing 10-min silent hangs.

The extension code is plain JS with no JS test infrastructure. These
tests read the source files and grep for the specific structural things
the #131 fix relies on, so a future refactor that reintroduces the bug
fails a Python test rather than another 10-min live-console debug loop.

Related: tests/test_automation_installer.py already uses the same
read-and-assert pattern against manifest.json and other extension
files.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_EXT_DIR = Path(__file__).resolve().parent.parent / "meeting_notetaker" / "resources" / "extension"
_COMMON_JS = _EXT_DIR / "content" / "common.js"
_CLAUDE_JS = _EXT_DIR / "content" / "claude.js"


@pytest.fixture(scope="module")
def common_js_source() -> str:
    return _COMMON_JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def claude_js_source() -> str:
    return _CLAUDE_JS.read_text(encoding="utf-8")


def test_wait_for_selector_priority_defined(common_js_source: str) -> None:
    """The priority-walking helper must exist. Without it, callers fall
    back to comma-joining a selector list into one querySelector call,
    which returns the first match in DOM order rather than the first
    matching selector in list order (the #131 root cause)."""
    assert "function waitForSelectorPriority(" in common_js_source, (
        "waitForSelectorPriority helper missing from content/common.js -- see #131"
    )
    # And it must iterate the list per tick, not build one query.
    fn_start = common_js_source.index("function waitForSelectorPriority(")
    fn_body = common_js_source[fn_start : fn_start + 1500]
    assert "for (const sel of selectors)" in fn_body, (
        "waitForSelectorPriority must walk the selector list per tick; the "
        "whole point of #131 is not collapsing the list into one query"
    )
    assert ".join(" not in fn_body, (
        "waitForSelectorPriority must not join selectors -- that reintroduces "
        "the #131 comma-collapse bug"
    )


def test_wait_for_selector_priority_exported(common_js_source: str) -> None:
    """The helper must be on the __mnSynth export object so the
    per-target scripts (claude.js, copilot.js) can destructure it."""
    export_start = common_js_source.index("window.__mnSynth = {")
    export_body = common_js_source[export_start : export_start + 1000]
    assert "waitForSelectorPriority," in export_body, (
        "waitForSelectorPriority missing from __mnSynth exports"
    )


def test_composer_selectors_have_no_bare_textarea(claude_js_source: str) -> None:
    """The bare `'textarea'` catch-all was the trap in #131: any
    ambient textarea on the page (page telemetry, hidden a11y widget,
    off-screen modal remnant) matched first, routed the paste to the
    wrong element, and the flow hung for 10 min waiting on a response
    that was never requested. `textarea[data-testid="chat-input"]` is
    specific enough to catch the real-textarea case without matching
    ambient elements."""
    start = claude_js_source.index("const COMPOSER_SELECTORS = [")
    end = claude_js_source.index("];", start)
    block = claude_js_source[start : end + 2]
    assert "'textarea'" not in block and '"textarea"' not in block, (
        "COMPOSER_SELECTORS must not contain a bare `textarea` catch-all -- see #131"
    )
    # The specific-textarea selector should still be there so real
    # textarea composers still work.
    assert 'textarea[data-testid="chat-input"]' in block, (
        "COMPOSER_SELECTORS should still include textarea[data-testid=\"chat-input\"] "
        "for chat UIs that legitimately use textareas"
    )


def test_composer_probe_uses_priority_walker(claude_js_source: str) -> None:
    """The composer wait must call waitForSelectorPriority against the
    COMPOSER_SELECTORS list, NOT waitForSelector against a comma-joined
    string (the #131 buggy pattern)."""
    assert "waitForSelectorPriority(COMPOSER_SELECTORS" in claude_js_source, (
        "composer probe must use waitForSelectorPriority(COMPOSER_SELECTORS, ...)"
    )
    assert "waitForSelector(COMPOSER_SELECTORS.join(" not in claude_js_source, (
        "waitForSelector(COMPOSER_SELECTORS.join(',')) is the #131 buggy pattern; "
        "use waitForSelectorPriority(COMPOSER_SELECTORS, ...) instead"
    )


def test_paste_into_composer_verifies_landing(common_js_source: str) -> None:
    """pasteIntoComposer must measure post-paste composer growth and
    return false when nothing landed. Every paste path can report
    success optimistically; that success is worthless if the composer
    is still empty. Returning true unconditionally is the exact
    'success check measured the wrong thing' pattern that hit us on
    the TipTap fix (#127) and again on #131."""
    fn_start = common_js_source.index("async function pasteIntoComposer(")
    fn_end = common_js_source.index("\n  }\n", fn_start)
    fn_body = common_js_source[fn_start:fn_end]
    # Must snapshot content length before the paste attempt.
    assert "beforeLen" in fn_body, (
        "pasteIntoComposer must snapshot composer content length BEFORE the paste attempt"
    )
    # Must measure growth after.
    assert "afterLen" in fn_body, (
        "pasteIntoComposer must measure composer content length after the paste attempt"
    )
    # Must have a threshold check that can return false.
    assert "return false" in fn_body, (
        "pasteIntoComposer must be able to return false when the paste didn't land"
    )
    assert "#131" in fn_body, (
        "pasteIntoComposer's assertion should reference #131 for future readers"
    )


def test_paste_verification_threshold_is_reasonable(common_js_source: str) -> None:
    """The threshold function should scale with text length but cap at
    a floor + ceiling so a 30k-char prompt doesn't need a byte-perfect
    roundtrip (TipTap collapses whitespace, may wrap in list nodes)
    while an empty composer easily fails."""
    assert "function pasteVerificationThreshold(" in common_js_source
    fn_start = common_js_source.index("function pasteVerificationThreshold(")
    fn_body = common_js_source[fn_start : fn_start + 400]
    # Scales with length, has both a floor and a cap.
    assert "Math.max" in fn_body and "Math.min" in fn_body, (
        "pasteVerificationThreshold should combine a floor and a cap around a "
        "text-length-relative value"
    )


def test_extension_manifest_version_bumped(common_js_source: str) -> None:
    """Chrome caches content-script bytecode aggressively; a version
    bump is what forces a re-parse when the operator runs Load
    unpacked. If someone edits the extension without bumping the
    manifest, dev-loop feedback silently uses the previous build."""
    manifest_text = (_EXT_DIR / "manifest.json").read_text(encoding="utf-8")
    # v0.7.14 is the #131 build. Anything strictly greater is also
    # fine (future work continues to bump); this just guards against
    # accidentally shipping the manifest at the pre-#131 v0.7.13.
    import json

    version = json.loads(manifest_text)["version"]
    parts = tuple(int(p) for p in version.split("."))
    assert parts >= (0, 7, 14), (
        f"manifest.json version {version} is behind the #131 baseline of 0.7.14; "
        "bump the patch when you push a new extension build so Chrome force-reloads"
    )
    # Load marker should also reference #131 so `chrome://extensions` +
    # DevTools console gives an at-a-glance signal that the current
    # build is the fix.
    assert "#131" in common_js_source[:2000], (
        "common.js load marker should reference #131 so DevTools console shows "
        "which build is live"
    )
