// Claude.ai content script.
//
// Picks the composer, pastes the prompt, submits, waits for streaming
// to settle, scrapes the response. Selectors for Claude's chat UI
// rotate on a roughly-monthly cadence (Anthropic owns the page), so
// we lean on selector-agnostic heuristics where possible:
//
//   * The composer: try a small list of known shapes, but ultimately
//     fall back to "the first contentEditable on the page".
//   * Streaming-complete: a MutationObserver-based "DOM hasn't changed
//     for N seconds" detector. Works regardless of whether the stop
//     button has any particular class or aria-label.
//   * Latest assistant message: try several increasingly broad
//     queries; pick the last match. If nothing matches, fall back
//     to "the longest direct child of the conversation container".
//
// Every checkpoint sends a STATUS message back to the service worker
// so the app's status bar reflects progress AND the port stays
// active. Chrome's MV3 service worker kill timer is reset by any
// port activity; long waits without heartbeats can drop the worker
// and lose the eventual result.

(function () {
  const helpers = window.__mnSynth;
  if (!helpers) {
    console.error("mn-synth: common.js not loaded");
    return;
  }
  const { STATUS, looksLikeInterstitial, showToast, clearToast, watchForInterstitialClear,
    waitForSelector, waitForResponseStreaming, pasteIntoComposer,
    findCopyButtonForMessage, htmlToMarkdown } = helpers;

  // Composer probes. We try data-testids and contenteditable in order;
  // the trailing 'div[contenteditable="true"]' is the catch-all that
  // works even if Claude has renamed every other attribute.
  const COMPOSER_SELECTORS = [
    'div[contenteditable="true"][data-testid="chat-input"]',
    'div[contenteditable="true"][role="textbox"]',
    'div[contenteditable="true"]',
    'textarea[data-testid="chat-input"]',
    'textarea',
  ];

  // Send-button probes. Mostly aria-label-based; the last entry tries
  // to find a button with an SVG arrow icon near the composer.
  const SEND_BUTTON_SELECTORS = [
    'button[aria-label="Send Message"]',
    'button[aria-label="Send message"]',
    'button[aria-label="Send"]',
    'button[data-testid="send-button"]',
    'button[type="submit"]',
  ];

  function findFirst(selectors) {
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el) return el;
    }
    return null;
  }

  // Find a button whose aria-label looks like a stop-generation
  // affordance. Fall back to a generic SVG-only button with an
  // adjacent class hint. Returns null if nothing matches -- callers
  // should treat that as "no stop button found, use DOM-settle
  // instead".
  function findStopButton() {
    const buttons = document.querySelectorAll("button");
    for (const b of buttons) {
      const label = (b.getAttribute("aria-label") || "").toLowerCase();
      if (label && /stop/.test(label)) return b;
      const testid = (b.getAttribute("data-testid") || "").toLowerCase();
      if (testid && /stop/.test(testid)) return b;
    }
    return null;
  }

  // Broad scrape for the latest assistant message. Strategy stack:
  //   1. Anything tagged with an explicit assistant role / testid.
  //   2. Anything in a "font-claude-response" or similar styling
  //      class container (Claude has historically used this).
  //   3. Look for the conversation container, then return its last
  //      message child that isn't tagged as the user's.
  // Each strategy returns either an element or null; we pick the
  // first non-null and grab its innerText.
  function findLatestAssistantMessage() {
    const strategies = [
      () => Array.from(document.querySelectorAll('[data-message-author-role="assistant"]')),
      () => Array.from(document.querySelectorAll('[data-testid*="assistant" i]')),
      () => Array.from(document.querySelectorAll('div[class*="font-claude-response"]')),
      () => Array.from(document.querySelectorAll('div[class*="assistant"]')),
      () => {
        // Heuristic: find every element flagged as a user message,
        // then walk up to a common ancestor and pick its last child
        // that isn't a user message. Covers the case where Claude
        // tags user messages but not assistant ones.
        const userMsgs = document.querySelectorAll(
          '[data-message-author-role="user"], [data-testid="user-message"]',
        );
        if (userMsgs.length === 0) return [];
        const lastUser = userMsgs[userMsgs.length - 1];
        let container = lastUser.parentElement;
        while (container && container !== document.body) {
          // Walk up to the conversation container -- expected to
          // have at least two message-like children.
          if (container.children.length >= 2) {
            const after = [];
            let seenLastUser = false;
            for (const child of container.children) {
              if (child === lastUser) {
                seenLastUser = true;
                continue;
              }
              if (seenLastUser) after.push(child);
            }
            if (after.length > 0) return [after[after.length - 1]];
          }
          container = container.parentElement;
        }
        return [];
      },
    ];
    for (const strat of strategies) {
      const matches = strat();
      if (matches && matches.length > 0) {
        return matches[matches.length - 1];
      }
    }
    return null;
  }

  let connection = null;

  function status(event, detail = "") {
    console.log("mn-synth:", event, detail);
    if (!connection) return;
    try { connection.postMessage({ type: "STATUS", event, detail }); } catch (_) {}
  }
  function done(markdown) {
    console.log("mn-synth: done; length=", markdown.length);
    // Diagnostic: a status message right before sending RESULT. If
    // the app sees this STATUS but not the RESULT, the failure is
    // between background-postMessage and the app's bridge -- not in
    // the content script. The status carries the byte count so we
    // can verify the size end-to-end.
    status(STATUS.DONE, `pre-send: ${markdown.length} chars about to send`);

    if (!connection) {
      console.warn("mn-synth: connection null, opening late-result port");
      try {
        const port = chrome.runtime.connect({
          name: `mn-synth-late-${_requestId}`,
        });
        port.postMessage({ type: "RESULT", target: "claude", markdown });
        console.log("mn-synth: late RESULT posted");
      } catch (e) {
        console.error("mn-synth: late-result send failed", e);
      }
      return;
    }
    try {
      connection.postMessage({ type: "RESULT", target: "claude", markdown });
      console.log("mn-synth: RESULT posted on primary port");
    } catch (e) {
      console.error("mn-synth: postMessage RESULT failed", e);
      // Fall back to a late port if the primary postMessage threw.
      try {
        const port = chrome.runtime.connect({
          name: `mn-synth-late-${_requestId}`,
        });
        port.postMessage({ type: "RESULT", target: "claude", markdown });
        console.log("mn-synth: RESULT fallback posted via late port");
      } catch (e2) {
        console.error("mn-synth: fallback late-result send also failed", e2);
      }
    }
  }
  function fail(code, detail = "") {
    console.error("mn-synth: fail", code, detail);
    if (!connection) return;
    try { connection.postMessage({ type: "ERROR", code, detail }); } catch (_) {}
  }

  let _requestId = "";

  async function runSynthesis(requestId, prompt) {
    _requestId = requestId;

    if (looksLikeInterstitial()) {
      status(STATUS.PROXY_ACK_NEEDED);
      showToast("Click PROCEED to acknowledge AI use, then synthesis continues automatically.");
      const initialHref = location.href;
      const result = await new Promise((resolve) => {
        watchForInterstitialClear(initialHref, resolve, {
          intervalMs: 750,
          maxMs: 5 * 60 * 1000,
        });
      });
      if (result.timedOut) {
        clearToast();
        fail("interstitial_timeout", "Proxy interstitial not cleared within 5 minutes.");
        return;
      }
      clearToast();
      status(STATUS.PROXY_ACK_CLEARED);
    }

    status(STATUS.PASTING);
    const composer = await waitForSelector(COMPOSER_SELECTORS.join(","), {
      timeoutMs: 30000,
    });
    if (!composer) {
      fail("not_logged_in", "Couldn't find Claude composer. Are you signed in?");
      return;
    }

    if (!pasteIntoComposer(composer, prompt)) {
      fail("paste_failed", "Composer rejected the pasted prompt.");
      return;
    }

    // Poll for the send button to enable post-paste. Lexical takes a
    // tick to recompute disabled-state after a multi-line insert.
    let submitted = false;
    const submitDeadline = Date.now() + 4000;
    while (Date.now() < submitDeadline) {
      const sendBtn = findFirst(SEND_BUTTON_SELECTORS);
      if (sendBtn && !sendBtn.disabled) {
        sendBtn.click();
        submitted = true;
        break;
      }
      await new Promise((r) => setTimeout(r, 100));
    }
    if (!submitted) {
      const enterInit = {
        key: "Enter",
        code: "Enter",
        keyCode: 13,
        which: 13,
        bubbles: true,
        cancelable: true,
      };
      composer.dispatchEvent(new KeyboardEvent("keydown", enterInit));
      composer.dispatchEvent(new KeyboardEvent("keypress", enterInit));
      composer.dispatchEvent(new KeyboardEvent("keyup", enterInit));
    }
    status(STATUS.AWAITING_RESPONSE);

    // Wait for the response to complete. v5 detector watches Claude's
    // composer-area stop-button toggle (Aaron's 2026-05-22 obs:
    // "the prompt textbox changes when it's thinking to a stop
    // button and disappears to a submit when done"). Binary,
    // deterministic, attribute-agnostic via inner-text + aria-label
    // regex match. Falls back to text-growth heuristic if the stop
    // button never appears (selectors drifted).
    //
    // Heartbeat status every 4s shows growth + event count + stop
    // button state so future failures are diagnosable at a glance.
    let lastHeartbeat = Date.now();
    const HEARTBEAT_MS = 4000;
    const result = await waitForResponseStreaming({
      settleMs: 2000,
      minStreamingDurationMs: 1500,
      minGrowthChars: 200,
      minGrowthEvents: 3,
      minGrowthSpanMs: 3000,
      fallbackSettleMs: 5000,
      timeoutMs: 10 * 60 * 1000,
      onTick: (ms, growth, events, stopVisible, stopEverSeen) => {
        if (Date.now() - lastHeartbeat >= HEARTBEAT_MS) {
          lastHeartbeat = Date.now();
          const stopFlag = stopVisible
            ? "STREAMING"
            : stopEverSeen
              ? "POST-STREAM"
              : "PRE-STREAM";
          status(
            STATUS.RESPONSE_STREAMING,
            `${Math.floor(ms / 1000)}s ${stopFlag} +${growth}chars ${events}events`,
          );
        }
      },
    });
    if (result === null) {
      fail("timeout", "Claude response didn't settle within 10 minutes.");
      return;
    }
    status(
      STATUS.RESPONSE_STREAMING,
      `settled (${result.signal}) after ${Math.floor(result.elapsedMs / 1000)}s, ` +
      `+${result.growthChars} chars, ${result.growthEvents} events`,
    );

    // If a stop button is still visible (e.g. the page hasn't yet
    // swapped it back to send), give it another moment to finish.
    if (findStopButton()) {
      await new Promise((r) => setTimeout(r, 3000));
    }

    const messageEl = findLatestAssistantMessage();
    if (!messageEl) {
      fail(
        "paste_failed",
        "Claude responded but I couldn't locate the assistant message in the DOM. " +
        "The page's structure may have changed; please report this with a screenshot of " +
        "the chat. Falling back to manual copy/paste.",
      );
      return;
    }

    // Extract the response as markdown by clicking Claude's own Copy
    // button and reading the clipboard. Claude knows how to serialize
    // its response (exactly what the user gets clicking Copy manually);
    // this is the only path that produces accurate markdown. The
    // previous DOM-walking fallback produced mangled output when
    // clipboard read failed -- duplicated list contents because the
    // walker's <li> traversal captured the parent list's children
    // instead of the li's own. Errors are strictly better than
    // garbage; we fail loudly so the user knows what to fix.
    const copyBtn = findCopyButtonForMessage(messageEl);
    if (!copyBtn) {
      fail(
        "clipboard_unavailable",
        "Couldn't find a Copy button on Claude's response. The page " +
        "structure may have changed; please report this. As a workaround " +
        "for this synthesis, copy + paste manually from the Claude tab.",
      );
      return;
    }

    let clip = "";
    let clipboardErrorDetail = "";
    try {
      copyBtn.click();
      // Give Claude a tick to populate the clipboard.
      await new Promise((r) => setTimeout(r, 250));
      clip = await navigator.clipboard.readText();
    } catch (e) {
      // Most likely cause: per-origin clipboard permission not
      // granted. Could also be a non-secure context (shouldn't happen
      // on claude.ai) or a transient focus issue.
      clipboardErrorDetail = String(e);
      console.warn("mn-synth: clipboard read failed", e);
    }

    if (!clip || clip.trim().length < 20) {
      const sizeNote =
        clip != null
          ? `clipboard returned ${clip.length} chars (expected hundreds)`
          : "clipboard read threw an exception";
      fail(
        "clipboard_unavailable",
        "Couldn't read Claude's response from the clipboard. The most " +
        "common cause is that Chrome's per-site clipboard permission " +
        "hasn't been granted to claude.ai yet. Fix: click Claude's own " +
        "Copy button once manually -- Chrome will ask you to allow " +
        "clipboard access. Click Allow, then try Send again. " +
        `Detail: ${sizeNote}${clipboardErrorDetail ? "; " + clipboardErrorDetail : ""}`,
      );
      return;
    }

    const markdown = clip.trim();
    status(STATUS.DONE, `${markdown.length} chars via claude-copy-button`);
    done(markdown);
  }

  // Entry point: background's executeScript -> __mnStartSynthesis(rid).
  window.__mnStartSynthesis = function (requestId) {
    if (connection) {
      return;
    }
    connection = chrome.runtime.connect({ name: `mn-synth-${requestId}` });
    connection.onMessage.addListener((msg) => {
      if (msg && msg.type === "SYNTHESIZE_START") {
        runSynthesis(msg.requestId, msg.prompt || "").catch((e) => {
          fail("unknown", String(e));
        });
      }
    });
    connection.onDisconnect.addListener(() => {
      // Service worker may have died. runSynthesis() will route the
      // eventual result through a fresh port if `connection` is null
      // by then.
      connection = null;
    });
  };
})();
