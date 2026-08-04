// Runs in the claude.ai page's JS realm (not the extension's isolated
// content script realm). Content scripts can't see DOM element expando
// properties set by page scripts (e.g. composer.editor attached by
// TipTap), so certain operations MUST run in the page realm. This
// script exposes a tiny request/response protocol over CustomEvents
// so the isolated-realm content script can drive page-realm APIs.
//
// Protocol:
//   isolated -> page  window.dispatchEvent(new CustomEvent('mn-synth-page-request', { detail: { id, action, args } }))
//   page -> isolated  window.dispatchEvent(new CustomEvent('mn-synth-page-response', { detail: { id, result } }))
//
// Actions:
//   'paste'  { composerSelector, text }  -> { ok, method } | { ok:false, error }
//   'clear'  { composerSelector }        -> { ok } | { ok:false, error }

(function () {
  if (window.__mnSynthPageWorld) return;
  window.__mnSynthPageWorld = { installed: true };

  function findComposer(selector) {
    return document.querySelector(selector);
  }

  function paste(args) {
    const composer = findComposer(args.composerSelector);
    if (!composer) return { ok: false, error: "composer not found" };
    // TipTap editor API. First choice because it drives ProseMirror
    // through the framework's public commands (schema-correct, view
    // updates or at least state updates cleanly).
    if (composer.editor && typeof composer.editor.chain === "function") {
      try {
        composer.editor.chain().focus().insertContent(args.text).run();
        return { ok: true, method: "editor.chain" };
      } catch (e) {
        // fall through
      }
    }
    if (composer.editor?.commands?.insertContent) {
      try {
        composer.editor.commands.insertContent(args.text);
        return { ok: true, method: "editor.commands" };
      } catch (e) {
        // fall through
      }
    }
    // Raw ProseMirror view dispatch. Bypasses TipTap entirely, works
    // as long as pmViewDesc / editor.view is present.
    const view = composer.editor?.view;
    if (view && view.state && typeof view.dispatch === "function") {
      try {
        view.dispatch(view.state.tr.insertText(args.text));
        return { ok: true, method: "view.dispatch" };
      } catch (e) {
        return { ok: false, error: String(e), tried: "view.dispatch" };
      }
    }
    return { ok: false, error: "no page-realm paste primitive available" };
  }

  function clearComposer(args) {
    const composer = findComposer(args.composerSelector);
    if (!composer) return { ok: false, error: "composer not found" };
    if (composer.editor?.commands?.clearContent) {
      try {
        composer.editor.commands.clearContent();
        return { ok: true };
      } catch (e) {
        return { ok: false, error: String(e) };
      }
    }
    return { ok: false, error: "no clearContent command" };
  }

  window.addEventListener("mn-synth-page-request", (evt) => {
    const { id, action, args } = evt.detail || {};
    let result;
    try {
      if (action === "paste") result = paste(args || {});
      else if (action === "clear") result = clearComposer(args || {});
      else result = { ok: false, error: "unknown action: " + action };
    } catch (e) {
      result = { ok: false, error: String(e) };
    }
    window.dispatchEvent(new CustomEvent("mn-synth-page-response", {
      detail: { id, result },
    }));
  });
})();
