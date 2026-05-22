// Service worker.
//
// Lifecycle:
//   1. App side starts; bridge.json is up to date.
//   2. User clicks Send in the Meeting Notetaker app.
//   3. App writes a `synthesize` message to the bridge socket.
//   4. We receive the message via the native-messaging port, find or
//      open the target's chat tab, send a SYNTHESIZE_START message to
//      the content script with the prompt, wait for status/result
//      messages back, forward them to the app.
//   5. On result, app writes notes.md and refreshes the synthesis tab.
//
// Native messaging port is created lazily on the first outbound app
// message AND maintained while there's an in-flight synthesis. The
// service worker can be killed by Chrome between syntheses; we don't
// rely on keeping the port hot.

const NATIVE_HOST_NAME = "com.meeting_notetaker.bridge";

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

const TARGETS = {
  claude: {
    newChatUrl: "https://claude.ai/new",
    matches: /^https:\/\/(?:[a-z0-9-]+\.)?claude\.ai\//i,
  },
  copilot: {
    newChatUrl: "https://m365.cloud.microsoft/chat",
    matches: /^https:\/\/(?:m365\.cloud\.microsoft|copilot\.microsoft\.com)\//i,
  },
};

const EXTENSION_VERSION = chrome.runtime.getManifest().version;

// Active synthesis state. Keyed by request_id so concurrent sends are
// at least addressable, even if the UI only allows one at a time.
const inflight = new Map(); // request_id -> {tabId, target, port}

let port = null;

function ensurePort() {
  if (port) {
    return port;
  }
  port = chrome.runtime.connectNative(NATIVE_HOST_NAME);
  port.onMessage.addListener(handleAppMessage);
  port.onDisconnect.addListener(() => {
    const err = chrome.runtime.lastError;
    if (err) {
      // Most common: app not running, bridge.json missing.
      console.warn("native host disconnected:", err.message);
    }
    port = null;
    // Do NOT clear inflight here. The native port can drop and
    // reconnect (worker reboot, app restart) while a synthesis is
    // still mid-flight in the content script -- when the result
    // eventually arrives, we want to ferry it through the next
    // ensurePort() call.
  });
  return port;
}

function sendToApp(payload) {
  try {
    const p = ensurePort();
    p.postMessage(payload);
    console.log("mn-synth bg: -> native host", payload.type,
      "rid=" + (payload.request_id || ""));
  } catch (e) {
    console.warn("mn-synth bg: sendToApp failed:", e);
    port = null;
  }
}

function sendStatus(requestId, event, detail = "") {
  sendToApp({ type: "status", request_id: requestId, event, detail });
}

function sendResult(requestId, target, markdown) {
  sendToApp({ type: "result", request_id: requestId, target, markdown });
}

function sendError(requestId, code, detail = "") {
  sendToApp({ type: "error", request_id: requestId, code, detail });
}

// ---------------------------------------------------------------------------
// App -> extension routing

async function handleAppMessage(msg) {
  if (!msg || typeof msg !== "object") return;
  switch (msg.type) {
    case "bridge_ready":
      // Just informational; the app version may be useful in the popup.
      chrome.storage.session.set({ appVersion: msg.app_version || "" });
      return;
    case "ping":
      sendToApp({
        type: "pong",
        request_id: msg.request_id || "",
        extension_version: EXTENSION_VERSION,
      });
      return;
    case "synthesize":
      await startSynthesis(msg);
      return;
    case "cancel":
      cancelSynthesis(msg.request_id);
      return;
    case "error":
      // Bridge unavailable, etc. Nothing we can do except surface in
      // the popup; the app already knows.
      console.warn("app side reports error:", msg);
      return;
    default:
      console.warn("unhandled app message type:", msg.type);
  }
}

// In-flight synthesis context (request id -> {prompt, target, newChat, ...})
// is mirrored to chrome.storage.session so a service worker that dies
// during the streaming wait and respawns can still route a late
// RESULT message through the module-top onConnect handler.
async function persistInflight(requestId, ctx) {
  inflight.set(requestId, ctx);
  try {
    await chrome.storage.session.set({ [`inflight:${requestId}`]: ctx });
  } catch (e) {
    console.warn("inflight persist failed:", e);
  }
}

async function loadInflight(requestId) {
  const cached = inflight.get(requestId);
  if (cached) return cached;
  try {
    const key = `inflight:${requestId}`;
    const data = await chrome.storage.session.get(key);
    if (data && data[key]) {
      inflight.set(requestId, data[key]);
      return data[key];
    }
  } catch (e) {
    console.warn("inflight load failed:", e);
  }
  return null;
}

async function clearInflight(requestId) {
  inflight.delete(requestId);
  try {
    await chrome.storage.session.remove(`inflight:${requestId}`);
  } catch (_) {}
}

// Module-top onConnect handler. Survives service-worker restarts
// (the listener re-registers on every worker boot). Routes any port
// named mn-synth-<rid> or mn-synth-late-<rid> to handleContentMessage.
// The "late" variant is opened by the content script if its original
// port disconnected (worker died mid-synthesis); we don't need to
// send SYNTHESIZE_START on late ports because the synthesis is
// already running -- we just need to receive the RESULT.
chrome.runtime.onConnect.addListener(async (port) => {
  const m = /^mn-synth-(late-)?([a-zA-Z0-9_-]+)$/.exec(port.name);
  if (!m) return;
  const isLate = !!m[1];
  const requestId = m[2];
  port.onMessage.addListener((msg) => handleContentMessage(requestId, msg, port));
  if (isLate) return;
  // Original port: send SYNTHESIZE_START with the captured prompt.
  const ctx = await loadInflight(requestId);
  if (!ctx) {
    console.warn("mn-synth: no inflight ctx for", requestId);
    return;
  }
  port.postMessage({
    type: "SYNTHESIZE_START",
    requestId,
    target: ctx.target,
    prompt: ctx.prompt || "",
    newChat: ctx.newChat !== false,
  });
});

async function startSynthesis(msg) {
  const requestId = msg.request_id || "";
  const targetKey = msg.target || "claude";
  const target = TARGETS[targetKey];
  if (!target) {
    sendError(requestId, "unknown", `unknown target: ${targetKey}`);
    return;
  }
  if (targetKey === "copilot") {
    // Stub in v0.6.3. The Copilot content script is a placeholder.
    sendError(
      requestId,
      "unknown",
      "Copilot automation is not implemented in this build. Use Claude as the target, or fall back to manual copy/paste.",
    );
    return;
  }

  sendStatus(requestId, STATUS.OPENING_TAB);
  // The app can pass a chat_url override (e.g.
  // claude.ai/project/<id> when the user has configured a Claude
  // project). Empty == use the target's default URL.
  const tabUrl = (msg.chat_url && msg.chat_url.trim()) || target.newChatUrl;
  let tab;
  try {
    tab = await chrome.tabs.create({
      url: tabUrl,
      active: true,
    });
  } catch (e) {
    sendError(requestId, "no_tab", String(e));
    return;
  }
  // Persist the synthesis context. The module-top onConnect handler
  // reads this when the content script opens its port; storing in
  // chrome.storage.session means it survives a service-worker restart.
  await persistInflight(requestId, {
    tabId: tab.id,
    target: targetKey,
    prompt: msg.prompt || "",
    newChat: msg.new_chat !== false,
  });

  // Inject the start trigger after the tab settles. The content script
  // opens a port back to us; the module-top onConnect handler matches
  // on the port name and sends SYNTHESIZE_START.
  await waitForTabComplete(tab.id, requestId, targetKey);
}

function cancelSynthesis(requestId) {
  const state = inflight.get(requestId);
  if (!state) return;
  if (state.tabId !== undefined) {
    chrome.tabs.remove(state.tabId).catch(() => {});
  }
  inflight.delete(requestId);
}

// ---------------------------------------------------------------------------
// Content script -> extension

function handleContentMessage(requestId, m, _contentPort) {
  if (!m || typeof m !== "object") return;
  console.log("mn-synth bg: from content", m.type, "rid=" + requestId,
    m.type === "RESULT" ? `len=${(m.markdown || "").length}` : "");
  switch (m.type) {
    case "STATUS":
      sendStatus(requestId, m.event, m.detail || "");
      return;
    case "RESULT":
      console.log("mn-synth bg: forwarding RESULT to app, len=" + (m.markdown || "").length);
      sendResult(requestId, m.target || "claude", m.markdown || "");
      // Auto-close the synthesis tab if it's not the only tab in its
      // window. Avoids piling up an endless trail of Claude tabs
      // after a series of syntheses. Skips when it's the last tab so
      // we don't accidentally close Chrome entirely. Best-effort
      // (kept off the critical path; failure just leaves the tab
      // open which is the prior behavior). Closes on RESULT only --
      // leaves the tab open on ERROR so the user can inspect what
      // Claude actually rendered (especially relevant for the
      // clipboard-permission-needed path where the response is in
      // the tab even though we couldn't read it).
      closeSynthesisTabIfSafe(requestId);
      clearInflight(requestId);
      return;
    case "ERROR":
      console.log("mn-synth bg: forwarding ERROR to app", m.code);
      sendError(requestId, m.code || "unknown", m.detail || "");
      clearInflight(requestId);
      return;
    default:
      console.warn("mn-synth bg: unhandled content message:", m);
  }
}

async function closeSynthesisTabIfSafe(requestId) {
  const ctx = await loadInflight(requestId);
  if (!ctx || ctx.tabId === undefined) return;
  try {
    const tab = await chrome.tabs.get(ctx.tabId);
    if (!tab || tab.windowId === undefined) return;
    const tabsInWindow = await chrome.tabs.query({ windowId: tab.windowId });
    if (tabsInWindow.length <= 1) {
      console.log("mn-synth bg: skipping tab close -- only tab in window");
      return;
    }
    await chrome.tabs.remove(ctx.tabId);
    console.log("mn-synth bg: closed synthesis tab", ctx.tabId);
  } catch (e) {
    // Tab already closed by user, removed, etc. Harmless.
    console.log("mn-synth bg: tab close skipped:", e && e.message);
  }
}

// ---------------------------------------------------------------------------
// Tab plumbing

// Module-top tabs.onUpdated listener. Re-triggers __mnStartSynthesis
// on every status=complete event for a tab that's still associated
// with an inflight synthesis. This handles two cases Aaron hit:
//
//   1. Cold-start: the user clicks Send while Chrome wasn't running,
//      Chrome launches, content scripts take a moment to load. The
//      first executeScript can fire before window.__mnStartSynthesis
//      is defined; the polling fallback inside the injected func
//      handles that, and this listener re-fires on later completes.
//
//   2. Login redirect: claude.ai/new redirects to /login when the
//      user isn't signed in. Our first __mnStartSynthesis call runs
//      on the login page (no composer found), then the user signs
//      in, navigates back to a chat URL. That's a fresh page context;
//      this listener catches the new status=complete event and
//      re-fires __mnStartSynthesis on the fresh page. The content
//      script's __mnStartSynthesis is idempotent for cases where a
//      synthesis is already running.
chrome.tabs.onUpdated.addListener((updatedTabId, info, tab) => {
  if (info.status !== "complete") return;
  // Find an inflight context that owns this tab.
  let requestId = "";
  for (const [rid, ctx] of inflight.entries()) {
    if (ctx.tabId === updatedTabId) {
      requestId = rid;
      break;
    }
  }
  if (!requestId) return;
  console.log("mn-synth bg: tab complete for inflight rid=" + requestId + " url=" + (tab && tab.url));
  triggerSynthesisOnTab(updatedTabId, requestId);
});

function triggerSynthesisOnTab(tabId, requestId) {
  // Inject a function that polls for window.__mnStartSynthesis to
  // appear (cold-start: content scripts may not have loaded yet),
  // then calls it. The content script's __mnStartSynthesis is
  // idempotent -- a duplicate call while already running is a no-op.
  chrome.scripting
    .executeScript({
      target: { tabId },
      args: [requestId],
      func: async (rid) => {
        // Poll up to 10 seconds for the helper to register. The
        // content script's manifest-declared run_at is document_idle,
        // which usually fires before our executeScript -- but cold-
        // start tabs and login pages can be different.
        const deadline = Date.now() + 10000;
        while (Date.now() < deadline) {
          if (typeof window.__mnStartSynthesis === "function") {
            window.__mnStartSynthesis(rid);
            return;
          }
          await new Promise((r) => setTimeout(r, 200));
        }
        console.warn(
          "mn-synth: __mnStartSynthesis never appeared after 10s -- " +
          "content script may not have loaded for this URL",
        );
      },
    })
    .catch((e) => {
      console.warn("mn-synth bg: executeScript failed for tab " + tabId, e);
    });
}

function waitForTabComplete(tabId, requestId, _targetKey) {
  // Light wrapper around the tabs.onUpdated listener above. Resolves
  // on first complete event so the caller knows the initial load
  // landed; subsequent re-triggers (login navigation, redirects) are
  // handled by the same listener and don't need to block startSynthesis.
  return new Promise((resolve) => {
    const handler = (updatedTabId, info, _tab) => {
      if (updatedTabId !== tabId) return;
      if (info.status !== "complete") return;
      chrome.tabs.onUpdated.removeListener(handler);
      resolve();
    };
    chrome.tabs.onUpdated.addListener(handler);
  });
}

// ---------------------------------------------------------------------------
// Popup helpers (read by popup.js via chrome.runtime.sendMessage)

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "POPUP_STATUS") {
    chrome.storage.session.get(["appVersion"], (data) => {
      sendResponse({
        connected: port !== null,
        appVersion: data.appVersion || "",
        extensionVersion: EXTENSION_VERSION,
        inflightCount: inflight.size,
      });
    });
    return true; // async response
  }
  if (msg && msg.type === "POPUP_RECONNECT") {
    if (port) {
      try { port.disconnect(); } catch (_) { /* ignore */ }
      port = null;
    }
    // Force a probe so the popup gets fresh connection state.
    ensurePort();
    sendToApp({ type: "ping", request_id: "popup-probe" });
    sendResponse({ ok: true });
    return false;
  }
});

// Try to attach to the host at startup so the popup shows the right
// status on first open. If the app isn't running OR the user hasn't
// clicked Verify yet (native-messaging-host manifest + HKCU key absent),
// connectNative fails silently; onDisconnect fires and we're left
// without a port until something wakes us.
//
// Two scheduled retry paths catch the install-before-verify case Aaron
// hit:
//
//   * chrome.alarms.create("bridge-retry", {periodInMinutes: 1}) -- the
//     alarms API wakes the service worker even after Chrome has
//     suspended it for inactivity. Every minute we re-check whether
//     the port is up; if not, ensurePort() runs again. As soon as the
//     user clicks Verify on the app side, the next alarm tick
//     connects.
//
//   * chrome.runtime.onStartup / onInstalled -- belt-and-suspenders
//     for the Chrome-just-launched / extension-just-loaded paths.
ensurePort();
try {
  // 0.5 minutes = 30 seconds; Chrome's minimum periodInMinutes for
  // a recurring alarm. Tightened from 1.0 in v0.6.3 because Chrome's
  // service-worker idle-kill timer is ~30s, so an alarm firing every
  // 30s+ couldn't keep the worker continuously alive. Combined with
  // the app-side 25s ping (sent only when Chrome is running), the
  // port stays up indefinitely while both processes are alive.
  chrome.alarms.create("bridge-retry", { periodInMinutes: 0.5 });
} catch (_e) {
  // alarms permission may be missing in a stale install; harmless.
}
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "bridge-retry" && port === null) {
    ensurePort();
  }
});
chrome.runtime.onStartup.addListener(() => {
  ensurePort();
});
chrome.runtime.onInstalled.addListener(() => {
  ensurePort();
});
