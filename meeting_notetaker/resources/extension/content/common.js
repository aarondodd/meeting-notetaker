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
      const onTick = opts.onTick || (() => {});
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
        onTick(now - started, truthy);
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

  // Resolve once the page's visible text has been growing (Claude is
  // actively streaming) AND then stopped for `settleMs`. Designed to
  // distinguish a single one-shot growth burst (user-message bubble
  // rendering after submit -- avatar + timestamp + label add a few
  // dozen chars in one tick) from sustained streaming (Claude
  // emitting tokens across multiple seconds).
  //
  // Gating: settle requires ALL of:
  //   * total growth >= minGrowthChars (filters out trivial UI noise)
  //   * growth events recorded in >= minGrowthEvents distinct polls
  //     (one burst = 1 event; streaming = many events)
  //   * span between first and last growth event >= minGrowthSpanMs
  //     (a burst-then-pause looks like 1 event spanning 0ms)
  //   * no growth for settleMs after the last event
  //
  // Resolves with {elapsedMs, growthChars, growthEvents, growthSpanMs}
  // on settle, null on timeout. onTick(elapsedMs, growthChars,
  // growthEvents) fires every poll for heartbeat status messages
  // (keeps the MV3 service worker awake).
  function waitForResponseStreaming(opts = {}) {
    return new Promise((resolve) => {
      const settleMs = opts.settleMs || 3000;
      const minGrowthChars = opts.minGrowthChars || 200;
      const minGrowthEvents = opts.minGrowthEvents || 3;
      const minGrowthSpanMs = opts.minGrowthSpanMs || 2000;
      const timeoutMs = opts.timeoutMs || 10 * 60 * 1000;
      const pollMs = opts.pollMs || 350;
      const onTick = opts.onTick || (() => {});

      const startLen = (document.body.innerText || "").length;
      const started = Date.now();
      let lastLen = startLen;
      const growthEventTimes = []; // ms timestamps of polls that saw cur > lastLen

      const tick = () => {
        const now = Date.now();
        const elapsed = now - started;
        const cur = (document.body.innerText || "").length;
        const growth = cur - startLen;
        if (cur > lastLen) {
          growthEventTimes.push(now);
          lastLen = cur;
        }
        onTick(elapsed, growth, growthEventTimes.length);
        if (elapsed > timeoutMs) {
          resolve(null);
          return;
        }
        if (
          growth >= minGrowthChars &&
          growthEventTimes.length >= minGrowthEvents
        ) {
          const span =
            growthEventTimes[growthEventTimes.length - 1] - growthEventTimes[0];
          const sinceLast =
            now - growthEventTimes[growthEventTimes.length - 1];
          if (span >= minGrowthSpanMs && sinceLast >= settleMs) {
            resolve({
              elapsedMs: elapsed,
              growthChars: growth,
              growthEvents: growthEventTimes.length,
              growthSpanMs: span,
            });
            return;
          }
        }
        setTimeout(tick, pollMs);
      };
      tick();
    });
  }

  // Push text into the chat composer. The composer flavor matters:
  //
  // - <textarea> (Copilot, simple chat UIs): set .value via the native
  //   prototype setter so the React framework's onChange registers
  //   correctly.
  //
  // - ContentEditable Lexical/ProseMirror editor (Claude.ai): we cannot
  //   use document.execCommand("insertText", ...) on the whole string
  //   because Lexical treats it as a single text-node insertion and
  //   strips newlines, leaving only the first line. The reliable path
  //   is to dispatch a synthetic paste event with a DataTransfer
  //   carrying text/plain -- Lexical (and ProseMirror, and most modern
  //   rich-text editors) listens for paste and runs its own multi-line
  //   parser on the clipboard data.
  //
  // If the paste event is canceled by the editor (it will be if
  // handled), we trust the editor. If nothing observable changes after
  // the dispatch, we fall back to execCommand line-by-line with
  // insertLineBreak between -- a slower but always-works path.
  function pasteIntoComposer(composer, text) {
    if (!composer) return false;
    composer.focus();

    // Branch on contentEditable. textarea path stays unchanged.
    if (composer.isContentEditable) {
      const before = (composer.innerText || composer.textContent || "").length;

      // Path 1: synthetic paste with DataTransfer (Lexical / ProseMirror).
      let pasteHandled = false;
      try {
        const dt = new DataTransfer();
        dt.setData("text/plain", text);
        const evt = new ClipboardEvent("paste", {
          clipboardData: dt,
          bubbles: true,
          cancelable: true,
        });
        // Lexical's paste handler cancels the event when it processes
        // the clipboard data. defaultPrevented==true == editor handled.
        composer.dispatchEvent(evt);
        pasteHandled = evt.defaultPrevented;
      } catch (_e) {
        // DataTransfer / ClipboardEvent constructors may throw in
        // older Chromium; fall through to the line-by-line path.
      }

      if (pasteHandled) {
        return true;
      }

      // Path 2: did the composer text grow at all?
      const after = (composer.innerText || composer.textContent || "").length;
      if (after > before) {
        return true;
      }

      // Path 3: insertText line-by-line. Some editors silently drop
      // the synthetic paste; we still owe them a multi-line insert.
      const lines = text.split("\n");
      for (let i = 0; i < lines.length; i += 1) {
        if (i > 0) {
          // Lexical respects insertLineBreak as a soft return; some
          // chat UIs prefer a hard paragraph (insertParagraph). We
          // pick insertLineBreak because chat composers typically
          // treat Enter as Submit, and a soft-return survives that.
          document.execCommand("insertLineBreak", false, null);
        }
        if (lines[i]) {
          document.execCommand("insertText", false, lines[i]);
        }
      }
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
    waitForResponseStreaming,
    pasteIntoComposer,
  };
})();
