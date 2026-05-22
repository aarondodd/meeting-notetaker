// M365 Copilot content script.
//
// PLACEHOLDER -- not implemented in v0.6.3. The settings dropdown lists
// Copilot but the background service worker rejects the synthesize
// request with code "unknown" + a clear message before any tab is
// opened. When this lands, mirror the structure of claude.js: pick
// the composer, paste, submit, watch streaming, scrape -- the proxy-
// interstitial handling in common.js applies unchanged.

(function () {
  // Intentionally empty in this build. Leaving the file so the
  // manifest match patterns + scripting.executeScript don't fail to
  // resolve on Copilot tabs.
  const helpers = window.__mnSynth;
  if (!helpers) return;

  window.__mnStartSynthesis = function (requestId) {
    const port = chrome.runtime.connect({ name: `mn-synth-${requestId}` });
    port.onMessage.addListener((msg) => {
      if (msg && msg.type === "SYNTHESIZE_START") {
        try {
          port.postMessage({
            type: "ERROR",
            code: "unknown",
            detail: "Copilot automation is not implemented yet (v0.6.3).",
          });
        } catch (_) {}
      }
    });
  };
})();
