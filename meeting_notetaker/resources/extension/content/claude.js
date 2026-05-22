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
    waitForSelector, waitForDomSettled, pasteIntoComposer } = helpers;

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
    if (!connection) {
      // Worker died during the wait. As a last-ditch retry, open a
      // fresh port and send. This works because the SYNTHESIZE_START
      // listener in background.js keeps `inflight` keyed by request
      // id; even a new worker incarnation can route the late result.
      try {
        const port = chrome.runtime.connect({
          name: `mn-synth-late-${_requestId}`,
        });
        port.postMessage({ type: "RESULT", target: "claude", markdown });
      } catch (e) {
        console.error("mn-synth: late-result send failed", e);
      }
      return;
    }
    try {
      connection.postMessage({ type: "RESULT", target: "claude", markdown });
    } catch (e) {
      console.error("mn-synth: postMessage RESULT failed", e);
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

    // Wait for streaming to settle. Selector-agnostic via
    // MutationObserver: detect that the DOM has stopped changing for
    // 2.5 seconds. minWaitMs=4000 protects against scraping while
    // Claude is still spinning up the response (the first few
    // hundred ms after submit can look quiet because the composer
    // clear + assistant-bubble-create is two discrete bursts).
    //
    // Heartbeat status every ~10s keeps the port active so Chrome's
    // service-worker kill timer doesn't drop the result.
    let lastHeartbeat = Date.now();
    const HEARTBEAT_MS = 10000;
    const elapsed = await waitForDomSettled({
      settleMs: 2500,
      timeoutMs: 10 * 60 * 1000,
      minWaitMs: 4000,
      onTick: (ms) => {
        if (Date.now() - lastHeartbeat >= HEARTBEAT_MS) {
          lastHeartbeat = Date.now();
          status(STATUS.RESPONSE_STREAMING, `~${Math.floor(ms / 1000)}s elapsed`);
        }
      },
    });
    if (elapsed === null) {
      fail("timeout", "Claude response didn't settle within 10 minutes.");
      return;
    }
    status(STATUS.RESPONSE_STREAMING, `settled after ${Math.floor(elapsed / 1000)}s`);

    // Scrape. If the stop button is still visible at this point,
    // give it another 5 seconds to finish; some chat UIs replace
    // the stop button with the send button before the final tokens
    // render.
    if (findStopButton()) {
      await new Promise((r) => setTimeout(r, 5000));
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
    const markdown = (messageEl.innerText || messageEl.textContent || "").trim();
    if (!markdown) {
      fail("paste_failed", "Found the assistant container but its text was empty.");
      return;
    }
    status(STATUS.DONE, `${markdown.length} chars`);
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
