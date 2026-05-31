// MN OWA Calendar Probe -- service worker.
//
// This extension is a sandbox for issue #69 Option C. It does not interact
// with the production Meeting Notetaker app in any way. The native-host
// name + extension ID are intentionally distinct from the prod
// synthesis bridge so both can coexist on the same machine.
//
// Lifecycle:
//   1. Relay app starts and registers com.meeting_notetaker.probe under
//      HKCU.
//   2. User opens outlook.office.com in Chrome with this extension
//      installed.
//   3. Relay app sends a request over its TCP loopback bridge; the
//      native host forwards it to us via stdio; we forward it to the
//      content script on the OWA tab; the content script calls
//      OWA's internal endpoint with the user's cookies; the response
//      flows back the same chain.
//
// The MV3 service worker can be killed by Chrome between requests.
// We keep no critical in-memory state -- every request is self-
// contained, request_id keyed.

const NATIVE_HOST_NAME = "com.meeting_notetaker.probe";

const EXTENSION_VERSION = chrome.runtime.getManifest().version;

let port = null;

function ensurePort() {
  if (port) return port;
  port = chrome.runtime.connectNative(NATIVE_HOST_NAME);
  port.onMessage.addListener(handleAppMessage);
  port.onDisconnect.addListener(() => {
    const err = chrome.runtime.lastError;
    if (err) console.warn("mn-probe bg: native host disconnected:", err.message);
    port = null;
  });
  console.log("mn-probe bg: native port opened");
  return port;
}

function sendToApp(payload) {
  try {
    const p = ensurePort();
    p.postMessage(payload);
    console.log(
      "mn-probe bg: -> app",
      payload.type,
      "rid=" + (payload.request_id || ""),
    );
  } catch (e) {
    console.warn("mn-probe bg: sendToApp failed:", e);
    port = null;
  }
}

// ---------------------------------------------------------------------------
// App -> extension routing

async function handleAppMessage(msg) {
  if (!msg || typeof msg !== "object") return;
  console.log("mn-probe bg: from app", msg.type, "rid=" + (msg.request_id || ""));
  switch (msg.type) {
    case "bridge_ready":
      chrome.storage.session.set({ appVersion: msg.app_version || "" });
      return;
    case "ping":
      sendToApp({
        type: "pong",
        request_id: msg.request_id || "",
        extension_version: EXTENSION_VERSION,
      });
      return;
    case "owa_request":
      // Generic verb dispatcher. msg.verb is one of:
      //   calendar.fetch, people.lookup, attachments.list,
      //   attachments.fetch.
      // msg.params carries verb-specific arguments.
      await dispatchOwaRequest(msg);
      return;
    default:
      console.warn("mn-probe bg: unhandled app message type:", msg.type);
  }
}

// Find an OWA tab to delegate to. If none is open, we create one and
// wait for it to settle before sending the request. The probe never
// asks the user to "click here first" -- the verbs work as long as
// the user is signed in to outlook.office.com somewhere recent enough
// that the session cookies are still valid.
async function findOrOpenOwaTab() {
  const matches = [
    "*://outlook.office.com/*",
    "*://outlook.office365.com/*",
  ];
  let tabs = await chrome.tabs.query({ url: matches });
  if (tabs.length > 0) return tabs[0];

  console.log("mn-probe bg: no OWA tab found; opening one");
  const created = await chrome.tabs.create({
    url: "https://outlook.office.com/mail/",
    active: false,
  });
  // Wait for the tab to finish loading so the content script has had
  // time to install its message handler.
  await new Promise((resolve) => {
    const handler = (id, info) => {
      if (id !== created.id) return;
      if (info.status !== "complete") return;
      chrome.tabs.onUpdated.removeListener(handler);
      resolve();
    };
    chrome.tabs.onUpdated.addListener(handler);
  });
  // Plus a short settle delay -- document_idle is the *start* of the
  // OWA SPA, not the end of its hydration. The content script is
  // resilient to early calls (it polls for the OWA auth cookie) but
  // a small buffer cuts down on retries.
  await new Promise((r) => setTimeout(r, 1500));
  return created;
}

async function dispatchOwaRequest(msg) {
  const requestId = msg.request_id || "";
  let tab;
  try {
    tab = await findOrOpenOwaTab();
  } catch (e) {
    sendToApp({
      type: "owa_error",
      request_id: requestId,
      code: "no_tab",
      detail: String(e),
    });
    return;
  }

  // Pass the verb + params to the content script. The content script
  // performs the fetch with the user's session cookies, packages the
  // result (or error), and sends it back over chrome.runtime.sendMessage.
  try {
    const response = await chrome.tabs.sendMessage(tab.id, {
      type: "OWA_FETCH",
      request_id: requestId,
      verb: msg.verb,
      params: msg.params || {},
    });
    if (!response) {
      sendToApp({
        type: "owa_error",
        request_id: requestId,
        code: "no_response",
        detail: "content script returned undefined",
      });
      return;
    }
    sendToApp({
      type: "owa_response",
      request_id: requestId,
      verb: msg.verb,
      ok: response.ok === true,
      status: response.status || 0,
      url: response.url || "",
      body: response.body || null,
      headers: response.headers || {},
      error: response.error || "",
      owa_build: response.owa_build || "",
    });
  } catch (e) {
    sendToApp({
      type: "owa_error",
      request_id: requestId,
      code: "send_failed",
      detail: String(e && e.message ? e.message : e),
    });
  }
}

// ---------------------------------------------------------------------------
// Popup helpers

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  // Two callers: the popup (no sender.tab) asking for state, and the
  // content script forwarding fetched JSON. We disambiguate on the type.
  if (msg && msg.type === "POPUP_STATUS") {
    sendResponse({
      connected: port !== null,
      extensionVersion: EXTENSION_VERSION,
    });
    return true;
  }
  if (msg && msg.type === "POPUP_FETCH_NOW") {
    // Manual test trigger from the popup. Issues a calendar.fetch for
    // today against the local TZ via the same path the relay uses.
    const now = new Date();
    const start = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 0, 0, 0);
    const end = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 23, 59, 59);
    dispatchOwaRequest({
      request_id: "popup-" + Date.now(),
      verb: "calendar.fetch",
      params: {
        start_iso: start.toISOString(),
        end_iso: end.toISOString(),
      },
    }).then(() => sendResponse({ ok: true }))
      .catch((e) => sendResponse({ ok: false, error: String(e) }));
    return true;
  }
});

// Heartbeat: keep the native port reattached. Same pattern as the prod
// synthesis bridge (every 30s alarm + onStartup/onInstalled).
ensurePort();
try {
  chrome.alarms.create("probe-retry", { periodInMinutes: 0.5 });
} catch (_) { /* harmless if alarms permission missing */ }
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "probe-retry" && port === null) ensurePort();
});
chrome.runtime.onStartup.addListener(() => ensurePort());
chrome.runtime.onInstalled.addListener(() => ensurePort());
