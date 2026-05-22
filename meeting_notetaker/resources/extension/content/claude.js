// Claude.ai content script.
//
// Picks the composer, pastes the prompt, submits, watches for the
// streaming-complete indicator, scrapes the response. The DOM
// selectors target Claude's web UI as of 2026-05; they're brittle by
// nature -- Anthropic owns this page and may restructure it. The
// content script is designed to fail loudly (ERROR back to background)
// rather than partial-success.

(function () {
  const helpers = window.__mnSynth;
  if (!helpers) {
    console.error("mn-synth: common.js not loaded");
    return;
  }
  const { STATUS, looksLikeInterstitial, showToast, clearToast, watchForInterstitialClear,
    waitForSelector, waitForStableFalse, pasteIntoComposer } = helpers;

  // Selectors. Each is paired with a fallback so a single DOM rename
  // doesn't kill the whole flow.
  const COMPOSER_SELECTORS = [
    'div[contenteditable="true"][data-testid="chat-input"]',
    'div[contenteditable="true"]',
    'textarea[data-testid="chat-input"]',
    'textarea',
  ];
  const SEND_BUTTON_SELECTORS = [
    'button[aria-label="Send Message"]',
    'button[aria-label="Send message"]',
    'button[data-testid="send-button"]',
    'button[type="submit"]',
  ];
  // Claude's stop-generation button replaces the send button while
  // streaming. Its presence == "still generating".
  const STOP_BUTTON_SELECTORS = [
    'button[aria-label="Stop Response"]',
    'button[aria-label="Stop response"]',
    'button[data-testid="stop-button"]',
  ];
  // Response message containers. The latest assistant turn is the
  // last one matching this selector.
  const ASSISTANT_MESSAGE_SELECTORS = [
    'div[data-testid="assistant-message"]',
    'div[data-message-author-role="assistant"]',
    'div.assistant-message',
  ];

  function findFirst(selectors) {
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el) return el;
    }
    return null;
  }

  function findAll(selectors) {
    for (const sel of selectors) {
      const matches = document.querySelectorAll(sel);
      if (matches.length > 0) return Array.from(matches);
    }
    return [];
  }

  let connection = null;

  function status(event, detail = "") {
    if (!connection) return;
    try { connection.postMessage({ type: "STATUS", event, detail }); } catch (_) {}
  }
  function done(markdown) {
    if (!connection) return;
    try { connection.postMessage({ type: "RESULT", target: "claude", markdown }); } catch (_) {}
  }
  function fail(code, detail = "") {
    if (!connection) return;
    try { connection.postMessage({ type: "ERROR", code, detail }); } catch (_) {}
  }

  async function runSynthesis(requestId, prompt) {
    // If the proxy interstitial is in the way, surface a toast and
    // wait it out. The user does the human-in-the-loop click.
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
      // After ack, the page typically navigates. Wait for the
      // composer to (re)appear before continuing.
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

    // Submit. Try the send button first; fall back to Enter.
    const sendBtn = findFirst(SEND_BUTTON_SELECTORS);
    if (sendBtn && !sendBtn.disabled) {
      sendBtn.click();
    } else {
      composer.dispatchEvent(new KeyboardEvent("keydown", {
        key: "Enter",
        code: "Enter",
        bubbles: true,
      }));
    }
    status(STATUS.AWAITING_RESPONSE);

    // Wait for the stop button to appear (means generation started),
    // then for it to disappear (means generation ended).
    await waitForSelector(STOP_BUTTON_SELECTORS.join(","), { timeoutMs: 30000 });
    status(STATUS.RESPONSE_STREAMING);
    const elapsed = await waitForStableFalse(
      () => !!findFirst(STOP_BUTTON_SELECTORS),
      { intervalMs: 300, stableMs: 1500, timeoutMs: 10 * 60 * 1000 },
    );
    if (elapsed === null) {
      fail("timeout", "Claude response didn't finish within 10 minutes.");
      return;
    }

    // Scrape the latest assistant message. We grab innerText rather
    // than the rendered HTML; Claude's response is markdown-rendered
    // and the source text is preserved in the inner DOM. innerText
    // gives us the visible-character form which is what the user
    // would have copied manually.
    const messages = findAll(ASSISTANT_MESSAGE_SELECTORS);
    if (messages.length === 0) {
      fail("paste_failed", "Couldn't locate assistant response in the DOM.");
      return;
    }
    const latest = messages[messages.length - 1];
    const markdown = (latest.innerText || latest.textContent || "").trim();
    if (!markdown) {
      fail("paste_failed", "Assistant response is empty.");
      return;
    }
    status(STATUS.DONE);
    done(markdown);
  }

  // Entry point: init.js executes when the tab finishes loading; it
  // opens a port back to the service worker named by request id, and
  // the worker pushes SYNTHESIZE_START down it.
  window.__mnStartSynthesis = function (requestId) {
    if (connection) {
      // Already running; ignore re-trigger.
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
      connection = null;
    });
  };
})();
