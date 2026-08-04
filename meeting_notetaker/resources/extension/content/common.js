// Shared helpers loaded before the per-target content scripts.
// Exposed on window so the per-target script can call them without
// ES module gymnastics (MV3 content scripts can be modules but the
// extra build step isn't worth it for a 100-line helper file).

(function () {
  // Distinctive load-time marker so we can tell at a glance which
  // build of common.js the tab actually has. Bump the string whenever
  // you push a new build so a stale content script is obvious.
  console.log(
    "%c[mn-synth] common.js loaded, build 2026-08-04-d (attr logging)",
    "color: cyan; font-weight: bold",
  );

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

  // Detect that the current page is asking the user to sign in,
  // rather than showing a chat composer. Heuristic: URL contains
  // /login or /sign-in, OR the page body has "Sign in" / "Continue
  // with Google" buttons + no composer in sight. The background
  // script's tabs.onUpdated listener will re-fire __mnStartSynthesis
  // when navigation lands on a non-login URL.
  function looksLikeLoginPage() {
    const url = location.href.toLowerCase();
    if (/\/(login|sign[_-]?in|signin|auth)\b/.test(url)) return true;
    // Look for sign-in CTA buttons that aren't typical of a logged-in chat UI.
    const buttons = document.querySelectorAll("button, a");
    let signinHits = 0;
    for (const b of buttons) {
      const text = (b.innerText || b.textContent || "").trim().toLowerCase();
      if (
        text === "log in" ||
        text === "sign in" ||
        /^continue with (google|apple|email)/.test(text) ||
        /^sign in with /.test(text)
      ) {
        signinHits += 1;
      }
    }
    return signinHits >= 2;
  }

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

  // Locate the "stop response" button by attribute or visible-text
  // match. Aaron confirmed (2026-05-22) that Claude.ai's composer
  // shows a stop button while generating and a submit button when
  // idle -- a binary, deterministic state signal that the previous
  // text-growth heuristics couldn't match.
  function findGenericStopButton(root) {
    root = root || document;
    const buttons = root.querySelectorAll("button");
    for (const b of buttons) {
      // aria-label / data-testid / title with "stop" in it.
      for (const attr of ["aria-label", "data-testid", "title"]) {
        const v = (b.getAttribute(attr) || "").toLowerCase();
        if (v && /\bstop\b/.test(v)) return b;
      }
      // Visible button text exactly "Stop" or starts with "Stop ".
      const text = (b.innerText || b.textContent || "").trim().toLowerCase();
      if (text === "stop" || /^stop\b/.test(text)) return b;
    }
    return null;
  }

  // Wait for the response to complete. Primary signal: the stop
  // button toggles in/out as Claude generates/finishes. Fallback: if
  // we never see a stop button at all (selectors / attribute names
  // have drifted), use the text-growth heuristic with stricter gates.
  //
  // Returns {elapsedMs, growthChars, growthEvents, growthSpanMs,
  //          signal, sawStopButton} on success; null on timeout.
  //
  // ``signal`` describes which path resolved -- helps future
  // diagnostics distinguish stop-button-toggle from text-growth
  // fallback.
  function waitForResponseStreaming(opts = {}) {
    return new Promise((resolve) => {
      const settleMs = opts.settleMs || 2000;
      const minStreamingDurationMs = opts.minStreamingDurationMs || 1500;
      // Text-growth fallback gates -- only consulted when the stop
      // button is never observed during the whole wait.
      const minGrowthChars = opts.minGrowthChars || 200;
      const minGrowthEvents = opts.minGrowthEvents || 3;
      const minGrowthSpanMs = opts.minGrowthSpanMs || 3000;
      const fallbackSettleMs = opts.fallbackSettleMs || 5000;
      const timeoutMs = opts.timeoutMs || 10 * 60 * 1000;
      const pollMs = opts.pollMs || 300;
      const onTick = opts.onTick || (() => {});

      const startLen = (document.body.innerText || "").length;
      const started = Date.now();
      let lastLen = startLen;
      const growthEventTimes = [];

      // Stop-button state machine.
      let stopButtonFirstSeenAt = null;
      let stopButtonGoneAt = null;
      let stopButtonGoneStreakStart = null;

      const tick = () => {
        const now = Date.now();
        const elapsed = now - started;

        // Text growth tracking (used both for diagnostics + fallback).
        const cur = (document.body.innerText || "").length;
        const growth = cur - startLen;
        if (cur > lastLen) {
          growthEventTimes.push(now);
          lastLen = cur;
        }

        // Stop button state machine.
        const stop = findGenericStopButton();
        if (stop) {
          if (stopButtonFirstSeenAt === null) {
            stopButtonFirstSeenAt = now;
          }
          stopButtonGoneStreakStart = null;
          stopButtonGoneAt = null;
        } else if (stopButtonFirstSeenAt !== null) {
          // We've seen the stop button before; now it's missing.
          // Track how long it's been gone -- transient disappearances
          // during streaming are tolerated; we require a sustained
          // absence to declare done.
          if (stopButtonGoneStreakStart === null) {
            stopButtonGoneStreakStart = now;
          }
          stopButtonGoneAt = now;
        }

        onTick(
          elapsed,
          growth,
          growthEventTimes.length,
          !!stop,
          stopButtonFirstSeenAt !== null,
        );

        if (elapsed > timeoutMs) {
          resolve(null);
          return;
        }

        // Primary path: stop button appeared (Claude started) AND has
        // been gone for >= settleMs (Claude done).
        if (
          stopButtonFirstSeenAt !== null &&
          stopButtonGoneStreakStart !== null
        ) {
          const goneFor = now - stopButtonGoneStreakStart;
          const streamedFor = stopButtonGoneStreakStart - stopButtonFirstSeenAt;
          if (goneFor >= settleMs && streamedFor >= minStreamingDurationMs) {
            resolve({
              elapsedMs: elapsed,
              growthChars: growth,
              growthEvents: growthEventTimes.length,
              growthSpanMs:
                growthEventTimes.length > 1
                  ? growthEventTimes[growthEventTimes.length - 1] -
                    growthEventTimes[0]
                  : 0,
              signal: `stop-button (streamed ${streamedFor}ms, gone ${goneFor}ms)`,
              sawStopButton: true,
            });
            return;
          }
        }

        // Fallback: stop button never seen anywhere on the page even
        // 8 seconds in -- the selector heuristic isn't matching this
        // Claude UI version. Fall back to text-growth-based settle
        // with much stricter gates (5s settle, 200 chars, 3+ events
        // spanning 3+ seconds).
        if (
          stopButtonFirstSeenAt === null &&
          elapsed > 8000 &&
          growth >= minGrowthChars &&
          growthEventTimes.length >= minGrowthEvents
        ) {
          const span =
            growthEventTimes[growthEventTimes.length - 1] -
            growthEventTimes[0];
          const sinceLast =
            now - growthEventTimes[growthEventTimes.length - 1];
          if (span >= minGrowthSpanMs && sinceLast >= fallbackSettleMs) {
            resolve({
              elapsedMs: elapsed,
              growthChars: growth,
              growthEvents: growthEventTimes.length,
              growthSpanMs: span,
              signal: `text-growth fallback (no stop button found)`,
              sawStopButton: false,
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
  // Content scripts run in an isolated JS realm where DOM expando
  // properties added by the page (e.g. TipTap's composer.editor) are
  // hidden. To reach composer.editor.chain(), the paste code must run
  // in the page realm. We do that by asking the background service
  // worker to invoke chrome.scripting.executeScript with world:'MAIN'.
  // This is Chrome's canonical MV3 primitive for this exact case --
  // no <script> injection, no CustomEvent bridge, no CSP concerns. #127.
  const PAGE_WORLD_COMPOSER_SELECTOR = 'div[contenteditable="true"][data-testid="chat-input"]';
  function callPageWorld(action, args) {
    return new Promise((resolve) => {
      const payload = { type: "PASTE_IN_PAGE", ...args };
      if (action !== "paste") {
        // Only paste is supported over this transport today.
        resolve({ ok: false, error: "unsupported action " + action });
        return;
      }
      try {
        chrome.runtime.sendMessage(payload, (reply) => {
          if (chrome.runtime.lastError) {
            console.warn("mn-synth: PASTE_IN_PAGE reply error:", chrome.runtime.lastError.message);
            resolve({ ok: false, error: chrome.runtime.lastError.message });
            return;
          }
          resolve(reply || { ok: false, error: "no reply from background" });
        });
      } catch (e) {
        console.warn("mn-synth: PASTE_IN_PAGE dispatch threw:", e);
        resolve({ ok: false, error: String(e) });
      }
    });
  }

  async function pasteIntoComposer(composer, text) {
    console.log(
      "[mn-synth] pasteIntoComposer entry",
      "hasComposer:", !!composer,
      "tag:", composer?.tagName,
      "testid:", composer?.getAttribute?.("data-testid") || "",
      "role:", composer?.getAttribute?.("role") || "",
      "attrCE:", composer?.getAttribute?.("contenteditable"),
      "isCE:", composer?.isContentEditable,
      "classes:", (composer?.className || "").toString().slice(0, 100),
      "len:", text?.length,
    );
    if (!composer) return false;
    composer.focus();

    // Branch on contentEditable. textarea path stays unchanged.
    if (composer.isContentEditable) {
      // Path 0: TipTap Editor API. Claude swapped composer from
      // Lexical to TipTap/ProseMirror around 2026-08 (#127). The
      // synthetic ClipboardEvent path below still gets
      // defaultPrevented==true from TipTap's paste handler AND the
      // internal document state does update (text persists across a
      // page refresh), but the view refuses to re-render without a
      // trusted user gesture and no message actually sends. Calling
      // the editor's own commands avoids the synthetic-event path
      // entirely; the composer stays visually blank until refresh but
      // the subsequent Send-button click DOES POST the message to
      // Claude's backend, so end-to-end automation works.
      // Path 0: page-realm TipTap Editor API via chrome.scripting.
      // executeScript(world: 'MAIN'), routed through the background
      // service worker. Content scripts can't reach composer.editor
      // from the isolated realm; the background can invoke code that
      // runs in the page realm where the editor instance lives. #127.
      console.log("mn-synth: attempting path 0 (executeScript world:MAIN)");
      const pageRes = await callPageWorld("paste", {
        composerSelector: PAGE_WORLD_COMPOSER_SELECTOR,
        text,
      });
      console.log("mn-synth: path 0 reply:", pageRes);
      if (pageRes && pageRes.ok) {
        console.log("mn-synth: path 0 succeeded via", pageRes.method);
        return true;
      }
      console.warn(
        "mn-synth: path 0 failed, falling through to synthetic paste:",
        pageRes?.error || "unknown",
      );

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
      console.log("[mn-synth] path 3 (execCommand line-by-line) completed");
      return true;
    }

    console.log("[mn-synth] composer.isContentEditable was falsy, checking textarea path");

    if ("value" in composer) {
      console.log("[mn-synth] taking textarea 'value' path");
      const nativeSetter = Object.getOwnPropertyDescriptor(
        Object.getPrototypeOf(composer),
        "value",
      ).set;
      nativeSetter.call(composer, text);
      composer.dispatchEvent(new Event("input", { bubbles: true }));
      composer.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    }
    console.warn("[mn-synth] composer had neither contentEditable nor value; returning false");
    return false;
  }

  // Find Claude's response-level Copy button. Claude renders two
  // kinds of Copy affordances:
  //
  //   1. The response-level Copy button in the reaction bar BELOW
  //      the message. Clicking this copies the full response as
  //      markdown -- this is what we want.
  //
  //   2. Inline "Copy" buttons inside <pre><code>...</code></pre>
  //      blocks for each code fence in the response. Clicking these
  //      copies just that code snippet (10-200 chars) -- NOT what we
  //      want. Synthesis output typically contains code fences, so
  //      these inline buttons frequently appear in the DOM before
  //      the response-level button.
  //
  // Aaron's 2026-05-22 repro: the original regex matched anything
  // starting with "Copy" (so "Copy", "Copy code", "Copy link" all
  // qualified). The inline "Copy code" button appeared first in DOM
  // order and got clicked instead of the response Copy -- clipboard
  // came back with a short snippet, failed the >=20 char gate,
  // surfaced the spurious "permission needed" dialog.
  //
  // Fix: exact label match for "copy" / "copy message" / "copy
  // response" only; reject buttons whose ancestor chain contains
  // <pre> or <code> (code-fence Copy buttons).
  function findCopyButtonForMessage(messageEl) {
    const looksLikeResponseCopy = (btn) => {
      // Exact-match labels only -- "Copy code" / "Copy link" /
      // "Copy URL" / "Copy markdown" (anything beyond the bare
      // word) is excluded.
      const RESPONSE_LABELS = new Set([
        "copy",
        "copy message",
        "copy response",
      ]);
      for (const attr of ["aria-label", "data-testid", "title"]) {
        const v = (btn.getAttribute(attr) || "").toLowerCase().trim();
        if (RESPONSE_LABELS.has(v)) return true;
      }
      const text = (btn.innerText || btn.textContent || "").trim().toLowerCase();
      if (text === "copy") return true;
      return false;
    };

    const isInsideCodeBlock = (btn) => {
      let p = btn.parentElement;
      while (p && p !== document.body) {
        const tag = p.tagName.toLowerCase();
        if (tag === "pre" || tag === "code") return true;
        p = p.parentElement;
      }
      return false;
    };

    const accept = (btn) => looksLikeResponseCopy(btn) && !isInsideCodeBlock(btn);

    // Strategy 1: search sibling nodes AFTER the message element.
    // Claude renders the reaction bar as a sibling of the message
    // bubble (an action row beneath it); searching siblings first
    // catches that case directly.
    let sibling = messageEl.nextElementSibling;
    for (let i = 0; i < 6 && sibling; i += 1) {
      for (const b of sibling.querySelectorAll("button")) {
        if (accept(b)) return b;
      }
      sibling = sibling.nextElementSibling;
    }

    // Strategy 2: walk up from the message scope looking for the
    // reaction bar. The bar might be a parent's child rather than a
    // direct sibling, depending on the DOM layout.
    let scope = messageEl;
    for (let depth = 0; depth < 6 && scope; depth += 1) {
      for (const b of scope.querySelectorAll("button")) {
        if (accept(b)) return b;
      }
      scope = scope.parentElement;
    }

    // Strategy 3 (last-ditch): scan the whole page for response-Copy
    // buttons and pick the last (most recent assistant turn).
    const all = Array.from(document.querySelectorAll("button")).filter(accept);
    if (all.length > 0) return all[all.length - 1];
    return null;
  }

  // Convert a rendered DOM element to a Markdown approximation.
  // Used as a fallback when Claude's copy button can't be located /
  // clicked / read from the clipboard. Covers the structures we
  // expect in meeting-synthesis output: headings, paragraphs, lists,
  // code blocks, inline code, bold/italic, blockquotes, links, HR,
  // basic tables. Unknown elements emit their inner text.
  function htmlToMarkdown(root) {
    if (!root) return "";

    const out = [];
    const walk = (node, listCtx) => {
      if (node.nodeType === Node.TEXT_NODE) {
        // Collapse whitespace inside text nodes but preserve a single
        // space if the original had any.
        out.push(node.textContent);
        return;
      }
      if (node.nodeType !== Node.ELEMENT_NODE) {
        return;
      }
      const tag = node.tagName.toLowerCase();
      const children = node.childNodes;

      const emitChildren = (ctx) => {
        for (const c of children) walk(c, ctx);
      };
      const captureInner = (ctx) => {
        const saved = out.length;
        emitChildren(ctx);
        const text = out.splice(saved).join("");
        return text;
      };

      switch (tag) {
        case "p":
          emitChildren(listCtx);
          out.push("\n\n");
          return;
        case "br":
          out.push("\n");
          return;
        case "strong":
        case "b": {
          const inner = captureInner(listCtx).trim();
          if (inner) out.push("**" + inner + "**");
          return;
        }
        case "em":
        case "i": {
          const inner = captureInner(listCtx).trim();
          if (inner) out.push("*" + inner + "*");
          return;
        }
        case "code": {
          // Inline code, unless we're inside a <pre> (handled below).
          if (node.parentElement && node.parentElement.tagName.toLowerCase() === "pre") {
            emitChildren(listCtx);
          } else {
            const inner = captureInner(listCtx);
            out.push("`" + inner + "`");
          }
          return;
        }
        case "pre": {
          // Fenced code block. Try to detect language from a child
          // <code class="language-foo"> hint.
          let lang = "";
          const codeChild = node.querySelector("code");
          if (codeChild && codeChild.className) {
            const m = codeChild.className.match(/language-(\w+)/);
            if (m) lang = m[1];
          }
          const text = (codeChild ? codeChild.textContent : node.textContent) || "";
          out.push("\n```" + lang + "\n" + text.replace(/\n$/, "") + "\n```\n\n");
          return;
        }
        case "h1":
        case "h2":
        case "h3":
        case "h4":
        case "h5":
        case "h6": {
          const level = parseInt(tag.slice(1), 10);
          const inner = captureInner(listCtx).trim();
          out.push("\n" + "#".repeat(level) + " " + inner + "\n\n");
          return;
        }
        case "ul":
        case "ol": {
          out.push("\n");
          const items = Array.from(node.children).filter(
            (c) => c.tagName.toLowerCase() === "li",
          );
          items.forEach((li, idx) => {
            const marker = tag === "ol" ? `${idx + 1}. ` : "- ";
            // Walk THIS li's children, not the parent list's. The
            // outer captureInner() iterates `children` which is bound
            // to the <ul>/<ol>'s childNodes -- using it here would
            // emit every <li> for every iteration and produce
            // N-times-duplicated output (Aaron's 2026-05-22 repro
            // hit this with 6 decisions rendered 6 times each).
            const saved = out.length;
            for (const c of li.childNodes) {
              walk(c, { ...listCtx, inList: tag });
            }
            const inner = out.splice(saved).join("").trim();
            // Naive: prefix each non-empty line with the marker on
            // the first line; subsequent lines (e.g. wrapped text)
            // get a 2-space indent.
            const lines = inner.split("\n");
            const first = lines.shift() || "";
            out.push(marker + first + "\n");
            for (const ln of lines) {
              if (ln.trim()) out.push("  " + ln + "\n");
            }
          });
          out.push("\n");
          return;
        }
        case "li":
          // Handled by parent ul/ol; emitting bare children covers
          // edge cases where li appears outside a list (rare).
          emitChildren(listCtx);
          return;
        case "blockquote": {
          const inner = captureInner(listCtx).trim();
          out.push("\n");
          for (const ln of inner.split("\n")) {
            out.push("> " + ln + "\n");
          }
          out.push("\n");
          return;
        }
        case "hr":
          out.push("\n---\n\n");
          return;
        case "a": {
          const href = node.getAttribute("href") || "";
          const inner = captureInner(listCtx);
          if (href && inner && href !== inner) {
            out.push("[" + inner + "](" + href + ")");
          } else {
            out.push(inner);
          }
          return;
        }
        case "table": {
          out.push("\n" + tableToMarkdown(node) + "\n\n");
          return;
        }
        case "script":
        case "style":
          // Ignore non-content elements.
          return;
        default:
          emitChildren(listCtx);
          return;
      }
    };

    walk(root, {});
    // Tidy: collapse 3+ blank lines down to 2.
    return out.join("").replace(/\n{3,}/g, "\n\n").trim();
  }

  function tableToMarkdown(table) {
    const rows = Array.from(table.querySelectorAll("tr"));
    if (rows.length === 0) return table.innerText || "";
    const cellsOf = (row) =>
      Array.from(row.querySelectorAll("th, td")).map((c) =>
        (c.innerText || c.textContent || "").trim().replace(/\|/g, "\\|"),
      );
    const lines = [];
    const header = cellsOf(rows[0]);
    if (header.length === 0) return table.innerText || "";
    lines.push("| " + header.join(" | ") + " |");
    lines.push("| " + header.map(() => "---").join(" | ") + " |");
    for (let i = 1; i < rows.length; i += 1) {
      const cells = cellsOf(rows[i]);
      // Pad to header width.
      while (cells.length < header.length) cells.push("");
      lines.push("| " + cells.join(" | ") + " |");
    }
    return lines.join("\n");
  }

  window.__mnSynth = {
    STATUS,
    looksLikeInterstitial,
    looksLikeLoginPage,
    showToast,
    clearToast,
    watchForInterstitialClear,
    waitForSelector,
    waitForStableFalse,
    waitForResponseStreaming,
    findGenericStopButton,
    findCopyButtonForMessage,
    htmlToMarkdown,
    pasteIntoComposer,
  };
})();
