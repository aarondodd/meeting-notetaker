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
  let tab;
  try {
    tab = await chrome.tabs.create({
      url: target.newChatUrl,
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

// ---------------------------------------------------------------------------
// Tab plumbing

function waitForTabComplete(tabId, requestId, targetKey) {
  return new Promise((resolve) => {
    const listener = (updatedTabId, info, _tab) => {
      if (updatedTabId !== tabId) return;
      if (info.status === "complete") {
        chrome.tabs.onUpdated.removeListener(listener);
        // Kick the per-target content script. The content script
        // registers window.__mnStartSynthesis; we inject a tiny
        // function that calls it with the request id. Going through
        // executeScript means we don't have to encode the request id
        // in the URL where Claude might strip it.
        chrome.scripting
          .executeScript({
            target: { tabId },
            args: [requestId],
            func: (rid) => {
              if (typeof window.__mnStartSynthesis === "function") {
                window.__mnStartSynthesis(rid);
              } else {
                // Content script never loaded -- the chat domain
                // mismatched the manifest matches, most likely.
                console.warn("mn-synth: __mnStartSynthesis missing");
              }
            },
          })
          .catch((e) => {
            sendError(requestId, "paste_failed", `executeScript failed: ${e}`);
            inflight.delete(requestId);
          });
        resolve();
      }
    };
    chrome.tabs.onUpdated.addListener(listener);
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
  chrome.alarms.create("bridge-retry", { periodInMinutes: 1 });
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
