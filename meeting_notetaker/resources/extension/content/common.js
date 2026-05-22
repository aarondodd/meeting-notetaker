// Shared helpers loaded before the per-target content scripts.
// Exposed on window so the per-target script can call them without
// ES module gymnastics (MV3 content scripts can be modules but the
// extra build step isn't worth it for a 100-line helper file).

(function () {
  const STATUS = {
    OPENING_TAB: "opening_tab",
    AWAITING_LOGIN: "awaiting_login",
    PROXY_ACK_NEEDED: "proxy_ack_needed",
    PROXY_ACK_CLEARED: "proxy_ack_cleared",
    PASTING: "pasting",
    AWAITING_RESPONSE: "awaiting_response",
    RESPONSE_STREAMING: "response_streaming",
    DONE: "done",
  };

  // FHB outbound-proxy interstitial markers. Aaron flagged that the
  // proxy gates AI traffic with a PROCEED button that the user must
  // click to acknowledge responsible AI use. The page replaces the
  // intended chat URL with an interstitial; after the user clicks
  // PROCEED the original URL is loaded.
  //
  // We don't know the exact DOM the proxy serves -- markers below are
  // a defensive heuristic: any of these match -> assume interstitial.
  // The toast we render is purely informational; we never click the
  // button on the user's behalf, that's the human-in-the-loop step
  // Aaron wants to preserve.
  const INTERSTITIAL_MARKERS = [
    /\b(proceed|acknowledge|continue)\b/i,
    /responsible (?:ai|use)/i,
    /ai (?:usage|use) policy/i,
    /this site has been blocked/i,
    /by clicking proceed/i,
  ];

  function looksLikeInterstitial() {
    // Heuristic 1: page is not on the expected target domain.
    // (per-target content scripts pass their expected hostname.)
    //
    // Heuristic 2: a PROCEED-style button is visible and the chat
    // composer isn't on the page yet.
    const text = document.body ? document.body.innerText || "" : "";
    if (!text) return false;
    let markerHits = 0;
    for (const re of INTERSTITIAL_MARKERS) {
      if (re.test(text)) markerHits += 1;
    }
    if (markerHits < 2) return false;
    // A button or link with PROCEED-like text confirms it.
    const buttons = document.querySelectorAll("button, a, input[type=button], input[type=submit]");
    for (const b of buttons) {
      const label = (b.innerText || b.value || "").trim();
      if (/^\s*(proceed|continue|acknowledge|i agree|accept)\s*$/i.test(label)) {
        return true;
      }
    }
    return false;
  }

  let toast = null;
  function showToast(text) {
    if (toast) {
      toast.innerText = text;
      return;
    }
    toast = document.createElement("div");
    toast.id = "mn-synth-toast";
    Object.assign(toast.style, {
      position: "fixed",
      top: "12px",
      right: "12px",
      zIndex: "2147483647",
      padding: "10px 14px",
      background: "#1f2937",
      color: "#f9fafb",
      font: "13px/1.4 -apple-system, system-ui, sans-serif",
      boxShadow: "0 4px 14px rgba(0,0,0,0.3)",
      borderRadius: "6px",
      maxWidth: "320px",
      pointerEvents: "none",
    });
    toast.innerText = text;
    document.documentElement.appendChild(toast);
  }

  function clearToast() {
    if (toast) {
      toast.remove();
      toast = null;
    }
  }

  // Poll for the interstitial to clear. Once the URL navigates away
  // (or the marker is no longer present), call `onCleared` so the
  // per-target script can resume the synthesis.
  function watchForInterstitialClear(initialHref, onCleared, opts = {}) {
    const intervalMs = opts.intervalMs || 750;
    const maxMs = opts.maxMs || 5 * 60 * 1000; // 5 min
    const started = Date.now();
    const handle = setInterval(() => {
      if (Date.now() - started > maxMs) {
        clearInterval(handle);
        clearToast();
        onCleared({ timedOut: true });
        return;
      }
      const hrefChanged = location.href !== initialHref;
      const stillBlocking = looksLikeInterstitial();
      if (hrefChanged || !stillBlocking) {
        clearInterval(handle);
        clearToast();
        onCleared({ timedOut: false });
      }
    }, intervalMs);
  }

  // Wait for a selector to appear in the DOM. Resolves with the
  // element on hit, null on timeout.
  function waitForSelector(selector, opts = {}) {
    return new Promise((resolve) => {
      const intervalMs = opts.intervalMs || 100;
      const timeoutMs = opts.timeoutMs || 30000;
      const deadline = Date.now() + timeoutMs;
      const tick = () => {
        const el = document.querySelector(selector);
        if (el) {
          resolve(el);
          return;
        }
        if (Date.now() > deadline) {
          resolve(null);
          return;
        }
        setTimeout(tick, intervalMs);
      };
      tick();
    });
  }

  // Generic "wait until predicate stops returning truthy for N ms",
  // useful for "streaming complete" detection: predicate returns
  // truthy while a stop button is visible, falsey when generation
  // ends. Resolves with the elapsed time once predicate has been
  // false for `stableMs` consecutive ms, or null on timeout.
  function waitForStableFalse(predicate, opts = {}) {
    return new Promise((resolve) => {
      const intervalMs = opts.intervalMs || 250;
      const stableMs = opts.stableMs || 1500;
      const timeoutMs = opts.timeoutMs || 5 * 60 * 1000;
      const started = Date.now();
      let firstFalseAt = null;
      const handle = setInterval(() => {
        const now = Date.now();
        if (now - started > timeoutMs) {
          clearInterval(handle);
          resolve(null);
          return;
        }
        const truthy = !!predicate();
        if (truthy) {
          firstFalseAt = null;
        } else if (firstFalseAt === null) {
          firstFalseAt = now;
        } else if (now - firstFalseAt >= stableMs) {
          clearInterval(handle);
          resolve(now - started);
        }
      }, intervalMs);
    });
  }

  // Convert plain text to HTML for the chat textarea. Most LLM web
  // UIs strip pasted formatting from a plain string anyway, but
  // dispatch as a paste event so React-based composers register the
  // change properly (setting .value alone is silently ignored).
  function pasteIntoComposer(composer, text) {
    if (!composer) return false;
    composer.focus();
    // ContentEditable (Claude.ai) vs <textarea>. Try both.
    if (composer.isContentEditable) {
      // execCommand is deprecated but still the only way to programmatically
      // dispatch an actual input event that React/Lexical editors observe.
      document.execCommand("insertText", false, text);
      return true;
    }
    if ("value" in composer) {
      const nativeSetter = Object.getOwnPropertyDescriptor(
        Object.getPrototypeOf(composer),
        "value",
      ).set;
      nativeSetter.call(composer, text);
      composer.dispatchEvent(new Event("input", { bubbles: true }));
      composer.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    }
    return false;
  }

  window.__mnSynth = {
    STATUS,
    looksLikeInterstitial,
    showToast,
    clearToast,
    watchForInterstitialClear,
    waitForSelector,
    waitForStableFalse,
    pasteIntoComposer,
  };
})();
