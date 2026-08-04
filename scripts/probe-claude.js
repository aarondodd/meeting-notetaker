// Meeting Notetaker -- claude.ai automation probe.
//
// Paste this WHOLE file into the DevTools console of a claude.ai tab
// that shows the chat composer (any new or existing conversation).
// It probes every DOM surface the browser extension depends on,
// prints a human-readable summary, and copies a JSON report to the
// clipboard. Paste the JSON back to whoever is debugging.
//
// Two entry points get installed on window:
//   mnProbe()      -- read-only. Discovers selectors, editor API,
//                     send-button candidates, response DOM, and
//                     tests every known paste primitive against an
//                     empty composer with unique per-primitive tags.
//                     Does NOT click Send, does NOT use quota.
//                     Composer will contain some throwaway tags at
//                     the end; delete or refresh to clear.
//   mnProbeSend()  -- end-to-end. Runs mnProbe(), then pastes a
//                     unique test prompt ("PROBE_<n>: reply with
//                     the single word ACK") using the best paste
//                     primitive found, clicks Send, waits for the
//                     response, and checks that the response scraper
//                     can find and read it. USES QUOTA (one small
//                     turn per run).
//
// The probe is defensive: every check is wrapped so one failure
// doesn't abort the run. Every discovered selector is recorded with
// the count of matching elements and, where relevant, key attributes.
//
// Add new tests here whenever a new failure mode surfaces. This
// script is the canonical source of truth for "what does the
// extension look at on claude.ai".

(function installProbe() {
  const VERSION = "1.0.0";
  const REPO_HINT = "meeting-notetaker/scripts/probe-claude.js";

  // Selectors mirrored from extension/content/claude.js. Keep this
  // list in sync when the extension changes its own selectors.
  const COMPOSER_SELECTORS = [
    'div[contenteditable="true"][data-testid="chat-input"]',
    'div[contenteditable="true"][role="textbox"]',
    'div[contenteditable="true"]',
    'textarea[data-testid="chat-input"]',
    'textarea',
  ];
  const SEND_BUTTON_SELECTORS = [
    'button[aria-label="Send Message"]',
    'button[aria-label="Send message"]',
    'button[aria-label="Send"]',
    'button[data-testid="send-button"]',
    'button[type="submit"]',
  ];
  const ASSISTANT_SELECTORS = [
    '[data-message-author-role="assistant"]',
    '[data-testid*="assistant" i]',
    'div[class*="font-claude-response"]',
    'div[class*="assistant"]',
  ];
  const USER_MSG_SELECTORS = [
    '[data-message-author-role="user"]',
    '[data-testid="user-message"]',
  ];

  // Small utilities -----------------------------------------------------

  const safe = (fn, fallback) => {
    try { return fn(); } catch (e) { return fallback === undefined ? { error: String(e) } : fallback; }
  };

  const summarize = (el) => {
    if (!el) return null;
    return {
      tag: el.tagName,
      testid: el.getAttribute?.("data-testid") || "",
      role: el.getAttribute?.("role") || "",
      ariaLabel: el.getAttribute?.("aria-label") || "",
      classes: (el.className || "").toString().slice(0, 120),
      visible: !!el.offsetParent,
      hasText: (el.innerText || el.value || "").length > 0,
      textPreview: (el.innerText || el.value || "").slice(0, 60),
    };
  };

  const wait = (ms) => new Promise((r) => setTimeout(r, ms));

  // Find the currently-loaded composer using the extension's selector
  // stack. Returns the first hit (matching extension behavior).
  const findComposer = () => {
    for (const sel of COMPOSER_SELECTORS) {
      const el = document.querySelector(sel);
      if (el) return { element: el, matched: sel };
    }
    return { element: null, matched: null };
  };

  // Non-destructive probes ---------------------------------------------

  const probeSelectors = () => {
    const composerMatches = COMPOSER_SELECTORS.map((sel) => {
      const els = Array.from(document.querySelectorAll(sel));
      return { selector: sel, count: els.length, first: summarize(els[0]) };
    });
    const sendMatches = SEND_BUTTON_SELECTORS.map((sel) => {
      const els = Array.from(document.querySelectorAll(sel));
      return {
        selector: sel,
        count: els.length,
        first: summarize(els[0]),
        disabled: els[0]?.disabled,
        ariaDisabled: els[0]?.getAttribute("aria-disabled"),
      };
    });
    const assistantMatches = ASSISTANT_SELECTORS.map((sel) => ({
      selector: sel,
      count: document.querySelectorAll(sel).length,
    }));
    const userMatches = USER_MSG_SELECTORS.map((sel) => ({
      selector: sel,
      count: document.querySelectorAll(sel).length,
    }));
    return { composerMatches, sendMatches, assistantMatches, userMatches };
  };

  const probeEditorApi = (composer) => {
    if (!composer) return { available: false, reason: "no composer" };
    const info = {
      available: false,
      hasEditor: !!composer.editor,
      editorType: typeof composer.editor,
      hasChain: typeof composer.editor?.chain === "function",
      hasCommands: typeof composer.editor?.commands === "object",
      hasView: !!composer.editor?.view,
      hasPmViewDesc: !!composer.pmViewDesc,
      classes: (composer.className || "").toString(),
      isTiptap: /tiptap|prosemirror/i.test((composer.className || "").toString()),
    };
    info.available = info.hasEditor && info.hasChain;
    return info;
  };

  // Test each paste primitive against the composer. Every test writes
  // a unique tag; the summary reports which tags actually landed in
  // the composer text. Non-destructive-ish: leaves tags in composer,
  // no send fires.
  const probePastePrimitives = (composer) => {
    if (!composer || !composer.isContentEditable) {
      return { skipped: true, reason: "no contenteditable composer" };
    }
    const results = [];
    const runTest = (name, tag, fn) => {
      const before = composer.innerText || "";
      let error = null;
      let defaultPrevented = null;
      try {
        const r = fn(tag);
        if (r && typeof r === "object") defaultPrevented = r.defaultPrevented;
      } catch (e) {
        error = String(e);
      }
      const after = composer.innerText || "";
      results.push({
        name,
        tag,
        tagLanded: after.includes(tag),
        beforeLen: before.length,
        afterLen: after.length,
        grew: after.length > before.length,
        defaultPrevented,
        error,
      });
    };

    composer.focus();

    runTest("A_ClipboardEvent_constructor", "PATH_A", (tag) => {
      const dt = new DataTransfer();
      dt.setData("text/plain", tag);
      const evt = new ClipboardEvent("paste", {
        clipboardData: dt, bubbles: true, cancelable: true,
      });
      composer.dispatchEvent(evt);
      return { defaultPrevented: evt.defaultPrevented };
    });

    runTest("B_Event_defineProperty", "PATH_B", (tag) => {
      const dt = new DataTransfer();
      dt.setData("text/plain", tag);
      const evt = new Event("paste", { bubbles: true, cancelable: true });
      Object.defineProperty(evt, "clipboardData", { value: dt });
      composer.dispatchEvent(evt);
      return { defaultPrevented: evt.defaultPrevented };
    });

    runTest("C_beforeinput_insertFromPaste", "PATH_C", (tag) => {
      const evt = new InputEvent("beforeinput", {
        inputType: "insertFromPaste",
        data: tag,
        bubbles: true, cancelable: true,
      });
      composer.dispatchEvent(evt);
      return { defaultPrevented: evt.defaultPrevented };
    });

    runTest("D_beforeinput_insertText", "PATH_D", (tag) => {
      const evt = new InputEvent("beforeinput", {
        inputType: "insertText",
        data: tag,
        bubbles: true, cancelable: true,
      });
      composer.dispatchEvent(evt);
      return { defaultPrevented: evt.defaultPrevented };
    });

    runTest("E_execCommand_insertText", "PATH_E", (tag) => {
      document.execCommand("insertText", false, tag);
    });

    runTest("F_editor_chain_insertContent", "PATH_F", (tag) => {
      if (!composer.editor?.chain) throw new Error("no editor.chain");
      composer.editor.chain().focus().insertContent(tag).run();
    });

    runTest("G_editor_commands_insertContent", "PATH_G", (tag) => {
      if (!composer.editor?.commands?.insertContent) throw new Error("no editor.commands.insertContent");
      composer.editor.commands.insertContent(tag);
    });

    runTest("H_editor_view_dispatch_insertText", "PATH_H", (tag) => {
      const view = composer.editor?.view;
      if (!view) throw new Error("no editor.view");
      const tr = view.state.tr.insertText(tag);
      view.dispatch(tr);
    });

    return { results, finalComposerText: (composer.innerText || "").slice(0, 300) };
  };

  const probeExtensionPresence = () => {
    return {
      mnSynth: typeof window.__mnSynth,
      mnStartSynthesis: typeof window.__mnStartSynthesis,
    };
  };

  const probeCopyButtons = () => {
    const all = Array.from(document.querySelectorAll("button"));
    const copyLike = all
      .filter((b) => {
        const label = (b.getAttribute("aria-label") || "").toLowerCase();
        const testid = (b.getAttribute("data-testid") || "").toLowerCase();
        const text = (b.innerText || "").trim().toLowerCase();
        return /copy/.test(label + testid + text);
      })
      .slice(0, 20)
      .map((b) => ({
        ariaLabel: b.getAttribute("aria-label"),
        testid: b.getAttribute("data-testid"),
        text: (b.innerText || "").trim().slice(0, 20),
        insideCode: !!b.closest("pre, code"),
      }));
    return { count: copyLike.length, samples: copyLike };
  };

  // Full read-only probe -----------------------------------------------

  window.mnProbe = function mnProbe() {
    console.log(`%c[mn-probe ${VERSION}] running read-only probe`, "font-weight: bold");
    const report = {
      version: VERSION,
      repoHint: REPO_HINT,
      timestamp: new Date().toISOString(),
      url: location.href,
      userAgent: navigator.userAgent,
      extension: safe(probeExtensionPresence),
    };
    const found = findComposer();
    report.composerChosen = {
      matchedSelector: found.matched,
      summary: summarize(found.element),
    };
    report.selectors = safe(probeSelectors);
    report.editorApi = safe(() => probeEditorApi(found.element));
    report.pastePrimitives = safe(() => probePastePrimitives(found.element));
    report.copyButtons = safe(probeCopyButtons);
    const json = JSON.stringify(report, null, 2);
    console.log(report);
    try { copy(json); console.log("%c[mn-probe] full JSON copied to clipboard", "color: green"); }
    catch (_e) { console.log("[mn-probe] paste this JSON back:"); console.log(json); }
    return report;
  };

  // End-to-end probe with real send ------------------------------------

  window.mnProbeSend = async function mnProbeSend() {
    console.log(`%c[mn-probe ${VERSION}] running END-TO-END probe (uses quota)`, "font-weight: bold");
    const report = await Promise.resolve(window.mnProbe());
    const paths = report.pastePrimitives?.results || [];
    const winning = paths.find((p) => p.tagLanded && !p.error);
    if (!winning) {
      report.e2e = { status: "no paste primitive landed a tag; cannot try send" };
      console.warn("[mn-probe] no paste primitive worked; skipping send test");
      return report;
    }
    console.log(`[mn-probe] winning paste primitive: ${winning.name}`);

    const composer = findComposer().element;

    // Clear composer via editor API if available (cleanest).
    if (composer.editor?.commands?.clearContent) {
      try { composer.editor.commands.clearContent(); } catch (_e) {}
    }

    const stamp = `PROBE_${Date.now().toString(36)}`;
    const prompt = `${stamp}: please reply with only the single word ACK`;

    // Re-run the winning primitive with the real prompt.
    composer.focus();
    const primitiveFns = {
      A_ClipboardEvent_constructor: (t) => {
        const dt = new DataTransfer(); dt.setData("text/plain", t);
        const evt = new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true });
        composer.dispatchEvent(evt);
      },
      B_Event_defineProperty: (t) => {
        const dt = new DataTransfer(); dt.setData("text/plain", t);
        const evt = new Event("paste", { bubbles: true, cancelable: true });
        Object.defineProperty(evt, "clipboardData", { value: dt });
        composer.dispatchEvent(evt);
      },
      C_beforeinput_insertFromPaste: (t) => {
        composer.dispatchEvent(new InputEvent("beforeinput", {
          inputType: "insertFromPaste", data: t, bubbles: true, cancelable: true,
        }));
      },
      D_beforeinput_insertText: (t) => {
        composer.dispatchEvent(new InputEvent("beforeinput", {
          inputType: "insertText", data: t, bubbles: true, cancelable: true,
        }));
      },
      E_execCommand_insertText: (t) => document.execCommand("insertText", false, t),
      F_editor_chain_insertContent: (t) => composer.editor.chain().focus().insertContent(t).run(),
      G_editor_commands_insertContent: (t) => composer.editor.commands.insertContent(t),
      H_editor_view_dispatch_insertText: (t) => {
        const view = composer.editor.view;
        view.dispatch(view.state.tr.insertText(t));
      },
    };
    try {
      primitiveFns[winning.name](prompt);
    } catch (e) {
      report.e2e = { status: "winning primitive threw when reused", error: String(e) };
      return report;
    }

    // Snapshot pre-send state.
    const preSend = {
      composerText: (composer.innerText || "").slice(0, 200),
      assistantCounts: ASSISTANT_SELECTORS.map((s) => ({ s, n: document.querySelectorAll(s).length })),
    };

    // Find and click send.
    let sendBtn = null;
    for (const sel of SEND_BUTTON_SELECTORS) {
      const el = document.querySelector(sel);
      if (el) { sendBtn = el; break; }
    }
    if (!sendBtn) {
      report.e2e = { status: "no send button matched", preSend };
      return report;
    }
    sendBtn.click();
    console.log("[mn-probe] send clicked; waiting up to 60s for response...");

    // Wait for a new assistant message to appear + settle.
    const startCount = document.querySelectorAll('[data-message-author-role="assistant"]').length;
    const deadline = Date.now() + 60_000;
    let stopSeen = false;
    let assistantEl = null;
    while (Date.now() < deadline) {
      await wait(500);
      const now = document.querySelectorAll('[data-message-author-role="assistant"]');
      if (now.length > startCount) {
        assistantEl = now[now.length - 1];
        if (!stopSeen) stopSeen = true;
      }
      // Stop button gone AND we have new assistant = probably done.
      const anyStop = Array.from(document.querySelectorAll("button"))
        .some((b) => /stop/i.test((b.getAttribute("aria-label") || "") + (b.getAttribute("data-testid") || "")));
      if (assistantEl && !anyStop) break;
    }

    if (!assistantEl) {
      report.e2e = { status: "send clicked but no new assistant message appeared within 60s", stamp, preSend };
      return report;
    }

    const respText = (assistantEl.innerText || "").trim();
    report.e2e = {
      status: "response received",
      stamp,
      responsePreview: respText.slice(0, 200),
      containsAck: /ack/i.test(respText),
      assistantElSummary: summarize(assistantEl),
    };
    const json = JSON.stringify(report, null, 2);
    console.log(report);
    try { copy(json); console.log("%c[mn-probe] full JSON copied to clipboard", "color: green"); }
    catch (_e) { console.log(json); }
    return report;
  };

  console.log(
    `%c[mn-probe ${VERSION}] installed. Run mnProbe() for read-only, mnProbeSend() for end-to-end.`,
    "color: cyan; font-weight: bold",
  );
})();
