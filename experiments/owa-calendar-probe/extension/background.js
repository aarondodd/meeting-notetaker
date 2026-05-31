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

// Find every OWA tab the user has open, in most-recently-active order.
// Multiple tabs are common -- Mail + Calendar + Search are separate
// tabs in OWA's modern UI. We try each one's content script in turn;
// the first that ACKs handles the verb. This survives the case where
// some tabs are stale (didn't get re-injected after an extension
// reload) without forcing the user to close them.
async function listOwaTabs(preferred) {
  const matches = [
    "*://outlook.office.com/*",
    "*://outlook.office365.com/*",
    "*://outlook.cloud.microsoft/*",
  ];
  const tabs = await chrome.tabs.query({ url: matches });
  // Sort key: tabs whose URL contains the preferred substring (e.g.
  // "calendar") rise to the top. Then active tabs. Then most-recent.
  tabs.sort((a, b) => {
    if (preferred) {
      const aw = (a.url || "").includes(preferred) ? 0 : 1;
      const bw = (b.url || "").includes(preferred) ? 0 : 1;
      if (aw !== bw) return aw - bw;
    }
    const aa = a.active ? 0 : 1;
    const ba = b.active ? 0 : 1;
    if (aa !== ba) return aa - ba;
    return (b.lastAccessed || 0) - (a.lastAccessed || 0);
  });
  return tabs;
}

async function openFreshOwaTab() {
  console.log("mn-probe bg: no OWA tab found; opening one");
  const created = await chrome.tabs.create({
    url: "https://outlook.office.com/mail/",
    active: false,
  });
  await new Promise((resolve) => {
    const handler = (id, info) => {
      if (id !== created.id) return;
      if (info.status !== "complete") return;
      chrome.tabs.onUpdated.removeListener(handler);
      resolve();
    };
    chrome.tabs.onUpdated.addListener(handler);
  });
  // document_idle is the *start* of the OWA SPA hydration, not the
  // end. Short settle reduces retries against a half-booted script.
  await new Promise((r) => setTimeout(r, 1500));
  return created;
}

// Try sendMessage against each candidate tab. If a tab errors with
// "Receiving end does not exist" (stale content script after extension
// reload), move to the next. If we exhaust the list, open a fresh tab.
async function sendToFirstReadyTab(payload) {
  const candidates = await listOwaTabs();
  const tried = [];
  for (const tab of candidates) {
    try {
      const response = await chrome.tabs.sendMessage(tab.id, payload);
      if (response) {
        return { response, tab };
      }
      tried.push({
        tab_id: tab.id, url: tab.url || "", reason: "undefined_response",
      });
    } catch (e) {
      tried.push({
        tab_id: tab.id, url: tab.url || "",
        reason: String(e && e.message ? e.message : e),
      });
      console.log(
        "mn-probe bg: tab",
        tab.id,
        "didn't respond:",
        e && e.message,
      );
    }
  }
  // None of the existing tabs worked. Open a fresh one and try once
  // more. If THAT fails we give up and surface the diagnostics.
  let fresh;
  try {
    fresh = await openFreshOwaTab();
  } catch (e) {
    return {
      response: null,
      tab: null,
      tried,
      open_error: String(e && e.message ? e.message : e),
    };
  }
  try {
    const response = await chrome.tabs.sendMessage(fresh.id, payload);
    if (response) {
      return { response, tab: fresh };
    }
    tried.push({
      tab_id: fresh.id, url: fresh.url || "", reason: "fresh_undefined",
    });
  } catch (e) {
    tried.push({
      tab_id: fresh.id, url: fresh.url || "",
      reason: "fresh_failed: " + String(e && e.message ? e.message : e),
    });
  }
  return { response: null, tab: null, tried };
}

// Run a fetch in the page's MAIN world via chrome.scripting.executeScript.
// The OWA SPA's fetch interceptor adds the MSAL bearer token to every
// outbound request; running our fetch in the same context inherits
// that treatment for free.
async function mainWorldFetch(tabId, url, init) {
  const [result] = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    args: [url, init || {}],
    func: async (u, i) => {
      const t0 = performance.now();
      try {
        const opts = Object.assign({ credentials: "include" }, i);
        const r = await fetch(u, opts);
        const ct = r.headers.get("content-type") || "";
        const elapsed = Math.round(performance.now() - t0);
        // Capture every response header so callers can see auth
        // challenges (www-authenticate), correlation ids (request-id),
        // redirects, etc. Header values can be long; we don't cap.
        const headers = {};
        r.headers.forEach((v, k) => { headers[k] = v; });
        let body;
        if (ct.indexOf("application/json") >= 0 || ct.indexOf("text/json") >= 0) {
          body = await r.json();
        } else if (ct.indexOf("text/") === 0 || ct === "") {
          const t = await r.text();
          body = { _text: t.slice(0, 4000) };
        } else {
          const buf = await r.arrayBuffer();
          const bytes = new Uint8Array(buf);
          let bin = "";
          const CHUNK = 0x8000;
          for (let off = 0; off < bytes.length; off += CHUNK) {
            bin += String.fromCharCode.apply(null, bytes.subarray(off, off + CHUNK));
          }
          body = { _b64: btoa(bin), _bytes: buf.byteLength };
        }
        return {
          ok: r.ok, status: r.status, url: r.url, body, headers,
          content_type: ct, elapsed_ms: elapsed, error: "",
        };
      } catch (e) {
        return {
          ok: false, status: 0, url: u, body: null, headers: {},
          content_type: "", elapsed_ms: Math.round(performance.now() - t0),
          error: String(e && e.message ? e.message : e),
        };
      }
    },
  });
  return (result && result.result) || {
    ok: false, status: 0, body: null, error: "executeScript_returned_nothing",
  };
}

async function dispatchOwaRequest(msg) {
  const requestId = msg.request_id || "";

  // The diagnose-main verb skips the content script and runs all
  // probes via MAIN-world executeScript. Use it to confirm whether
  // a given OWA endpoint accepts the page's bearer-tokened fetches.
  if (msg.verb === "diagnose-main") {
    return runDiagnoseMain(requestId, msg.params || {});
  }
  if (msg.verb === "inspect-storage") {
    return runInspectStorage(requestId, msg.params || {});
  }
  if (msg.verb === "fetch-authed-calendar") {
    return runFetchAuthedCalendar(requestId, msg.params || {});
  }
  if (msg.verb === "try-all-tokens") {
    return runTryAllTokens(requestId, msg.params || {});
  }
  if (msg.verb === "bg-fetch-calendar") {
    return runBgFetchCalendar(requestId, msg.params || {});
  }
  if (msg.verb === "list-tabs") {
    return runListTabs(requestId);
  }
  if (msg.verb === "bg-fetch-people") {
    return runBgFetchPeople(requestId, msg.params || {});
  }

  const payload = {
    type: "OWA_FETCH",
    request_id: requestId,
    verb: msg.verb,
    params: msg.params || {},
  };
  const { response, tab, tried, open_error } = await sendToFirstReadyTab(payload);
  if (!response) {
    sendToApp({
      type: "owa_error",
      request_id: requestId,
      code: "no_ready_tab",
      detail: JSON.stringify({
        tabs_tried: tried || [],
        open_error: open_error || "",
        hint: "all candidate OWA tabs failed to respond; the most likely "
          + "cause is stale content scripts after an extension reload -- "
          + "close all outlook.office.com tabs and open a fresh one, "
          + "or visit outlook.office.com if no tab is open.",
      }),
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
    tab_url: (tab && tab.url) || "",
  });
}

async function runDiagnoseMain(requestId, _params) {
  const tabs = await listOwaTabs();
  if (tabs.length === 0) {
    sendToApp({
      type: "owa_error",
      request_id: requestId,
      code: "no_owa_tab",
      detail: "no outlook.office[365].com tab open; open one and retry",
    });
    return;
  }
  const tab = tabs[0];
  const origin = (() => {
    try { return new URL(tab.url || "").origin; } catch (_) { return "https://outlook.office.com"; }
  })();
  const start = new Date(Date.now() - 12 * 3600 * 1000).toISOString();
  const end = new Date(Date.now() + 36 * 3600 * 1000).toISOString();
  const probes = [
    {
      name: "owa_action_GetCalendarView_POST",
      url: origin + "/owa/service.svc?action=GetCalendarView&app=Calendar",
      init: {
        method: "POST",
        headers: {
          "Content-Type": "application/json; charset=utf-8",
          "Accept": "application/json",
        },
        body: JSON.stringify({
          __type: "GetCalendarViewJsonRequest:#Exchange",
          Header: {
            __type: "JsonRequestHeaders:#Exchange",
            RequestServerVersion: "V2018_01_08",
          },
          Body: {
            __type: "GetCalendarViewRequest:#Exchange",
            StartDate: start,
            EndDate: end,
            FolderId: {
              __type: "FolderId:#Exchange",
              BaseFolderId: {
                __type: "DistinguishedFolderId:#Exchange",
                Id: "calendar",
              },
            },
          },
        }),
      },
    },
    {
      name: "owa_action_FindItem_POST",
      url: origin + "/owa/service.svc?action=FindItem&app=Calendar",
      init: {
        method: "POST",
        headers: {
          "Content-Type": "application/json; charset=utf-8",
          "Accept": "application/json",
        },
        body: JSON.stringify({
          __type: "FindItemJsonRequest:#Exchange",
          Header: {
            __type: "JsonRequestHeaders:#Exchange",
            RequestServerVersion: "V2018_01_08",
          },
          Body: {
            __type: "FindItemRequest:#Exchange",
            Paging: {
              __type: "IndexedPageView:#Exchange",
              BasePoint: "Beginning",
              Offset: 0,
              MaxEntriesReturned: 100,
            },
            ParentFolderIds: [{
              __type: "DistinguishedFolderId:#Exchange",
              Id: "calendar",
            }],
            Traversal: "Shallow",
            ItemShape: {
              __type: "ItemResponseShape:#Exchange",
              BaseShape: "IdOnly",
              AdditionalProperties: [
                { __type: "PropertyUri:#Exchange", FieldURI: "ItemSubject" },
                { __type: "PropertyUri:#Exchange", FieldURI: "CalendarStart" },
                { __type: "PropertyUri:#Exchange", FieldURI: "CalendarEnd" },
              ],
            },
            CalendarView: {
              __type: "CalendarView:#Exchange",
              StartDate: start,
              EndDate: end,
              MaxEntriesReturned: 100,
            },
          },
        }),
      },
    },
    {
      name: "graph_v10_calendarview_local",
      url: origin + "/api/Calendar/EventsViewV2" +
        "?startDateTime=" + encodeURIComponent(start) +
        "&endDateTime=" + encodeURIComponent(end),
      init: { method: "GET", headers: { "Accept": "application/json" } },
    },
    {
      name: "owa_internal_calendarview",
      url: origin + "/owa/0/api/v2.0/me/calendarview" +
        "?startDateTime=" + encodeURIComponent(start) +
        "&endDateTime=" + encodeURIComponent(end),
      init: { method: "GET", headers: { "Accept": "application/json" } },
    },
  ];

  const results = [];
  for (const p of probes) {
    try {
      const r = await mainWorldFetch(tab.id, p.url, p.init);
      // Reduce body to a small preview to keep the message under 1MB.
      let preview = null;
      if (r.body && typeof r.body === "object" && r.body._text) {
        preview = r.body._text.slice(0, 800);
      } else if (r.body && typeof r.body === "object") {
        preview = { keys: Object.keys(r.body).slice(0, 12) };
      }
      // Surface a handful of high-value response headers + the URL
      // (in case there was a redirect) so we can see auth challenges
      // and correlation ids without dumping every header.
      const headerHints = {};
      const hh = r.headers || {};
      [
        "www-authenticate", "request-id", "x-feserver", "location",
        "x-calculatedfetarget", "x-owa-error", "x-requestid",
        "x-msedge-ref", "x-owa-version",
      ].forEach((k) => {
        if (hh[k] !== undefined) headerHints[k] = hh[k];
      });
      results.push({
        name: p.name,
        url: p.url,
        final_url: r.url,
        method: p.init.method,
        ok: r.ok,
        status: r.status,
        content_type: r.content_type,
        elapsed_ms: r.elapsed_ms,
        error: r.error,
        body_preview: preview,
        header_hints: headerHints,
      });
    } catch (e) {
      results.push({
        name: p.name,
        url: p.url,
        method: p.init.method,
        error: "exec_failed: " + String(e && e.message ? e.message : e),
      });
    }
  }

  sendToApp({
    type: "owa_response",
    request_id: requestId,
    verb: "diagnose-main",
    ok: true,
    status: 200,
    url: tab.url || "",
    body: { tab_url: tab.url, origin, candidate_probes: results },
    headers: {},
    error: "",
    owa_build: "",
    tab_url: tab.url || "",
  });
}

// Walk the OWA tab's localStorage looking for MSAL-shaped access tokens.
// MSAL.js v5 stores tokens under keys built from
// {home_account_id}-{environment}-accesstoken-{client_id}-{tenant}-{scopes}.
// The Exchange Online client_id is 00000002-0000-0ff1-ce00-000000000000;
// that's the audience the server's WWW-Authenticate header named.
async function runInspectStorage(requestId, _params) {
  const tabs = await listOwaTabs();
  if (tabs.length === 0) {
    sendToApp({
      type: "owa_error",
      request_id: requestId,
      code: "no_owa_tab",
      detail: "no outlook.office[365].com tab open",
    });
    return;
  }
  const tab = tabs[0];
  const [result] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    world: "MAIN",
    func: () => {
      // Return a redacted summary of localStorage plus a token list
      // shaped for inspection. We do NOT log secrets to console; the
      // values flow over native messaging back to the relay process
      // running under the user's own account.
      const allKeys = [];
      const tokenCandidates = [];
      const EXO_CLIENT_ID = "00000002-0000-0ff1-ce00-000000000000";
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (!k) continue;
        allKeys.push({
          key: k.slice(0, 120),
          value_len: (localStorage.getItem(k) || "").length,
        });
        if (k.indexOf("accesstoken") < 0) continue;
        const raw = localStorage.getItem(k) || "";
        let parsed;
        try { parsed = JSON.parse(raw); } catch (_) { parsed = null; }
        if (!parsed) continue;
        const secret = parsed.secret || "";
        const isJwt = typeof secret === "string" && secret.startsWith("eyJ");
        tokenCandidates.push({
          key: k.slice(0, 180),
          client_id: parsed.clientId || "",
          target: parsed.target || "",
          realm: parsed.realm || "",
          home_account_id: (parsed.homeAccountId || "").slice(0, 40),
          environment: parsed.environment || "",
          expires_on: parsed.expiresOn || parsed.extendedExpiresOn || "",
          token_type: parsed.tokenType || "",
          is_jwt: isJwt,
          secret_preview: isJwt ? secret.slice(0, 28) + "..." : "(not JWT)",
          is_exchange: parsed.clientId === EXO_CLIENT_ID,
        });
      }
      return {
        ls_size: localStorage.length,
        all_keys: allKeys.slice(0, 80),
        token_candidates: tokenCandidates,
      };
    },
  });
  sendToApp({
    type: "owa_response",
    request_id: requestId,
    verb: "inspect-storage",
    ok: true,
    status: 200,
    url: tab.url || "",
    body: (result && result.result) || { error: "exec_returned_nothing" },
    headers: {},
    error: "",
    owa_build: "",
    tab_url: tab.url || "",
  });
}

// Find an Exchange Online access token in localStorage and use it as
// Authorization: Bearer for a calendar fetch. The fetch happens inside
// the MAIN-world function so the token never leaves the page context
// until the response is on its way back.
async function runFetchAuthedCalendar(requestId, params) {
  const tabs = await listOwaTabs();
  if (tabs.length === 0) {
    sendToApp({
      type: "owa_error",
      request_id: requestId,
      code: "no_owa_tab",
      detail: "no outlook.office[365].com tab open",
    });
    return;
  }
  const tab = tabs[0];
  const start = params.start_iso ||
    new Date(Date.now() - 12 * 3600 * 1000).toISOString();
  const end = params.end_iso ||
    new Date(Date.now() + 36 * 3600 * 1000).toISOString();

  const [result] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    world: "MAIN",
    args: [start, end],
    func: async (startIso, endIso) => {
      const EXO_CLIENT_ID = "00000002-0000-0ff1-ce00-000000000000";
      const now = Math.floor(Date.now() / 1000);

      // 1. Find an Exchange Online access token that hasn't expired.
      let token = "";
      let tokenInfo = null;
      let candidatesScanned = 0;
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (!k || k.indexOf("accesstoken") < 0) continue;
        let parsed;
        try { parsed = JSON.parse(localStorage.getItem(k) || ""); }
        catch (_) { continue; }
        if (!parsed || parsed.clientId !== EXO_CLIENT_ID) continue;
        candidatesScanned += 1;
        const exp = Number(parsed.expiresOn || 0);
        if (exp && exp < now) continue;
        if (typeof parsed.secret !== "string" || !parsed.secret.startsWith("eyJ")) continue;
        token = parsed.secret;
        tokenInfo = {
          target: parsed.target || "",
          realm: parsed.realm || "",
          expires_in: exp ? exp - now : -1,
          environment: parsed.environment || "",
        };
        break;
      }
      if (!token) {
        return {
          ok: false, status: 0,
          error: "no_exchange_token_found",
          detail: { candidates_scanned: candidatesScanned, exo_client_id: EXO_CLIENT_ID },
        };
      }

      // 2. Try a small ordered list of endpoints, returning on first
      // success. We start with the modern internal endpoint because
      // it returns Graph-shaped JSON that's easiest to parse.
      const endpoints = [
        {
          name: "owa_internal_calendarview",
          url: location.origin + "/owa/0/api/v2.0/me/calendarview" +
            "?startDateTime=" + encodeURIComponent(startIso) +
            "&endDateTime=" + encodeURIComponent(endIso) +
            "&$top=100",
          init: { method: "GET" },
        },
        {
          name: "office365_calendarview",
          url: "https://outlook.office365.com/api/v2.0/me/calendarview" +
            "?startDateTime=" + encodeURIComponent(startIso) +
            "&endDateTime=" + encodeURIComponent(endIso) +
            "&$top=100",
          init: { method: "GET" },
        },
      ];
      const attempts = [];
      for (const ep of endpoints) {
        const t0 = performance.now();
        let res;
        try {
          res = await fetch(ep.url, {
            method: ep.init.method,
            credentials: "include",
            headers: {
              "Authorization": "Bearer " + token,
              "Accept": "application/json",
              "Prefer": 'outlook.timezone="UTC"',
            },
          });
        } catch (e) {
          attempts.push({
            name: ep.name, url: ep.url, error: String(e && e.message ? e.message : e),
            elapsed_ms: Math.round(performance.now() - t0),
          });
          continue;
        }
        const ct = res.headers.get("content-type") || "";
        const headers = {};
        res.headers.forEach((v, k) => {
          if (["www-authenticate", "request-id", "x-feserver",
               "x-calculatedfetarget", "x-owa-error"].indexOf(k) >= 0) {
            headers[k] = v;
          }
        });
        let body;
        if (ct.indexOf("application/json") >= 0) {
          body = await res.json();
        } else {
          const t = await res.text();
          body = { _text: t.slice(0, 2000) };
        }
        attempts.push({
          name: ep.name,
          url: ep.url,
          status: res.status,
          content_type: ct,
          headers: headers,
          ok: res.ok,
          elapsed_ms: Math.round(performance.now() - t0),
          body_size: ct.indexOf("application/json") >= 0
            ? ((body && body.value && body.value.length) || 0)
            : -1,
        });
        if (res.ok) {
          return {
            ok: true,
            status: res.status,
            url: ep.url,
            body,
            content_type: ct,
            attempts,
            endpoint_used: ep.name,
            token_info: tokenInfo,
          };
        }
      }
      return {
        ok: false,
        status: attempts.length ? attempts[attempts.length - 1].status : 0,
        error: "all_endpoints_failed",
        attempts,
        token_info: tokenInfo,
      };
    },
  });
  const inner = (result && result.result) || { error: "exec_returned_nothing" };
  sendToApp({
    type: "owa_response",
    request_id: requestId,
    verb: "fetch-authed-calendar",
    ok: inner.ok === true,
    status: inner.status || 0,
    url: inner.url || tab.url || "",
    body: inner,
    headers: {},
    error: inner.error || "",
    owa_build: "",
    tab_url: tab.url || "",
  });
}

// Try every JWT token we can find in localStorage against the calendar
// API. Empirical brute force; the server tells us which audience it
// accepts. Returns a matrix of (token, endpoint) results.
async function runTryAllTokens(requestId, params) {
  const tabs = await listOwaTabs();
  if (tabs.length === 0) {
    sendToApp({
      type: "owa_error", request_id: requestId,
      code: "no_owa_tab",
      detail: "no outlook.office[365].com tab open",
    });
    return;
  }
  const tab = tabs[0];
  const start = params.start_iso ||
    new Date(Date.now() - 12 * 3600 * 1000).toISOString();
  const end = params.end_iso ||
    new Date(Date.now() + 36 * 3600 * 1000).toISOString();

  const [result] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    world: "MAIN",
    args: [start, end],
    func: async (startIso, endIso) => {
      const now = Math.floor(Date.now() / 1000);

      // Collect every JWT we can find in localStorage. We don't
      // filter by client_id -- the SPA holds tokens with its own
      // client_id but audiences against various backends; we let
      // the server adjudicate.
      const tokens = [];
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (!k || k.indexOf("accesstoken") < 0) continue;
        let parsed;
        try { parsed = JSON.parse(localStorage.getItem(k) || ""); }
        catch (_) { continue; }
        if (!parsed) continue;
        const secret = parsed.secret || "";
        if (typeof secret !== "string" || !secret.startsWith("eyJ")) continue;
        const exp = Number(parsed.expiresOn || 0);
        if (exp && exp < now) continue;
        // Try to decode the JWT's aud claim (middle base64-url segment).
        let aud = "";
        try {
          const parts = secret.split(".");
          if (parts.length === 3) {
            const pad = parts[1] + "===".slice(0, (4 - parts[1].length % 4) % 4);
            const claims = JSON.parse(atob(pad.replace(/-/g, "+").replace(/_/g, "/")));
            aud = claims.aud || "";
          }
        } catch (_) { /* opaque token, no decoded claims */ }
        tokens.push({
          key_tail: k.slice(-100),
          client_id: parsed.clientId || "",
          target: parsed.target || "",
          aud,
          expires_in: exp ? exp - now : -1,
          secret,
        });
      }

      const endpoints = [
        {
          name: "outlook.office.com/owa/0/api/v2.0/me/calendarview",
          url: location.origin + "/owa/0/api/v2.0/me/calendarview" +
            "?startDateTime=" + encodeURIComponent(startIso) +
            "&endDateTime=" + encodeURIComponent(endIso) + "&$top=100",
        },
        {
          name: "outlook.office365.com/api/v2.0/me/calendarview",
          url: "https://outlook.office365.com/api/v2.0/me/calendarview" +
            "?startDateTime=" + encodeURIComponent(startIso) +
            "&endDateTime=" + encodeURIComponent(endIso) + "&$top=100",
        },
        {
          name: "graph.microsoft.com/v1.0/me/calendarview",
          url: "https://graph.microsoft.com/v1.0/me/calendarview" +
            "?startDateTime=" + encodeURIComponent(startIso) +
            "&endDateTime=" + encodeURIComponent(endIso) + "&$top=100",
        },
      ];

      const attempts = [];
      for (const tok of tokens) {
        for (const ep of endpoints) {
          const t0 = performance.now();
          let res;
          try {
            res = await fetch(ep.url, {
              method: "GET",
              credentials: "include",
              headers: {
                "Authorization": "Bearer " + tok.secret,
                "Accept": "application/json",
                "Prefer": 'outlook.timezone="UTC"',
              },
            });
          } catch (e) {
            attempts.push({
              endpoint: ep.name, token_target: tok.target, token_aud: tok.aud,
              error: String(e && e.message ? e.message : e),
              elapsed_ms: Math.round(performance.now() - t0),
            });
            continue;
          }
          const ct = res.headers.get("content-type") || "";
          let body = null;
          let event_count = -1;
          let preview = "";
          if (ct.indexOf("application/json") >= 0) {
            body = await res.json();
            if (body && Array.isArray(body.value)) {
              event_count = body.value.length;
            }
          } else {
            const t = await res.text();
            preview = t.slice(0, 200);
          }
          attempts.push({
            endpoint: ep.name,
            token_target: tok.target.slice(0, 80),
            token_aud: tok.aud,
            status: res.status,
            content_type: ct,
            elapsed_ms: Math.round(performance.now() - t0),
            event_count,
            preview,
            ok: res.ok,
            body: res.ok ? body : null,
          });
          if (res.ok && event_count >= 0) {
            // Return the FIRST successful attempt's full body so the
            // relay sees real events without re-running everything.
            return { winner: attempts[attempts.length - 1], attempts };
          }
        }
      }
      return { winner: null, attempts };
    },
  });

  const inner = (result && result.result) || { error: "exec_returned_nothing" };
  sendToApp({
    type: "owa_response", request_id: requestId,
    verb: "try-all-tokens",
    ok: !!inner.winner,
    status: inner.winner ? inner.winner.status : 0,
    url: inner.winner ? inner.winner.endpoint : tab.url || "",
    body: inner,
    headers: {},
    error: inner.winner ? "" : "no_token_endpoint_combination_succeeded",
    owa_build: "",
    tab_url: tab.url || "",
  });
}

// Two-step: harvest tokens from the page in MAIN world, then fetch
// from the BG service worker (extension origin, CORS-bypass via
// host_permissions, NOT subject to OWA's page CSP or service worker).
// This is the architecturally correct path for an MV3 extension
// reading a backend API the page's CSP would otherwise block.
async function runBgFetchCalendar(requestId, params) {
  const tabs = await listOwaTabs("calendar");
  if (tabs.length === 0) {
    sendToApp({
      type: "owa_error", request_id: requestId, code: "no_owa_tab",
      detail: "no outlook.office[365].com tab open",
    });
    return;
  }
  const tab = tabs[0];
  const start = params.start_iso ||
    new Date(Date.now() - 12 * 3600 * 1000).toISOString();
  const end = params.end_iso ||
    new Date(Date.now() + 36 * 3600 * 1000).toISOString();

  // Step 1: harvest every JWT in localStorage, with decoded aud claim.
  const [harvest] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    world: "MAIN",
    func: () => {
      const now = Math.floor(Date.now() / 1000);
      const out = [];
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (!k || k.indexOf("accesstoken") < 0) continue;
        let parsed;
        try { parsed = JSON.parse(localStorage.getItem(k) || ""); }
        catch (_) { continue; }
        if (!parsed) continue;
        const secret = parsed.secret || "";
        if (typeof secret !== "string" || !secret.startsWith("eyJ")) continue;
        const exp = Number(parsed.expiresOn || 0);
        if (exp && exp < now) continue;
        let aud = "";
        try {
          const parts = secret.split(".");
          if (parts.length === 3) {
            let p = parts[1].replace(/-/g, "+").replace(/_/g, "/");
            while (p.length % 4 !== 0) p += "=";
            const claims = JSON.parse(atob(p));
            aud = claims.aud || "";
          }
        } catch (_) { /* opaque */ }
        out.push({
          target: parsed.target || "",
          aud,
          secret,
          expires_in: exp ? exp - now : -1,
          client_id: parsed.clientId || "",
        });
      }
      return out;
    },
  });
  const tokens = (harvest && harvest.result) || [];
  if (tokens.length === 0) {
    sendToApp({
      type: "owa_error", request_id: requestId, code: "no_tokens",
      detail: "no JWTs found in OWA localStorage; try navigating to /calendar/",
    });
    return;
  }

  // Step 2: fetch each candidate endpoint with each token. From BG SW.
  // The endpoint set is keyed off the token's audience -- send Exchange-
  // backend tokens to the Outlook endpoints, Graph-backend tokens to
  // graph.microsoft.com. Skip combinations where the audience clearly
  // doesn't match the host.
  // Endpoint candidates against three audiences: Exchange (legacy
  // outlook.office.com/office365.com hostnames), Microsoft Graph
  // (the documented, supported path), and the new unified
  // outlook.cloud.microsoft hostname. The cloud.microsoft host is
  // a SPA-only domain in the rollout phase -- the underlying
  // service APIs may still be hosted at the legacy domains, OR
  // they may have moved. We probe both.
  const tabOrigin = (() => {
    try { return new URL(tab.url || "").origin; }
    catch (_) { return "https://outlook.cloud.microsoft"; }
  })();
  const endpoints = [
    {
      name: "outlook.cloud.microsoft_owa_v2",
      url: tabOrigin + "/owa/0/api/v2.0/me/calendarview" +
        "?startDateTime=" + encodeURIComponent(start) +
        "&endDateTime=" + encodeURIComponent(end) + "&$top=100",
      audience_match: () => true,
    },
    {
      name: "outlook.office.com_owa_v2",
      url: "https://outlook.office.com/owa/0/api/v2.0/me/calendarview" +
        "?startDateTime=" + encodeURIComponent(start) +
        "&endDateTime=" + encodeURIComponent(end) + "&$top=100",
      audience_match: (aud) => (aud || "").indexOf("outlook.office") >= 0,
    },
    {
      name: "outlook.office365.com_v2",
      url: "https://outlook.office365.com/api/v2.0/me/calendarview" +
        "?startDateTime=" + encodeURIComponent(start) +
        "&endDateTime=" + encodeURIComponent(end) + "&$top=100",
      audience_match: (aud) => (aud || "").indexOf("outlook.office") >= 0,
    },
    {
      name: "graph.microsoft.com_v1.0",
      url: "https://graph.microsoft.com/v1.0/me/calendarview" +
        "?startDateTime=" + encodeURIComponent(start) +
        "&endDateTime=" + encodeURIComponent(end) + "&$top=100",
      audience_match: (aud) => (aud || "").indexOf("graph.microsoft") >= 0,
    },
  ];

  const attempts = [];
  let winner = null;
  for (const tok of tokens) {
    for (const ep of endpoints) {
      if (!ep.audience_match(tok.aud)) continue;
      const t0 = Date.now();
      let res, err = "", body = null, ct = "", status = 0;
      try {
        res = await fetch(ep.url, {
          method: "GET",
          headers: {
            "Authorization": "Bearer " + tok.secret,
            "Accept": "application/json",
            "Prefer": 'outlook.timezone="UTC"',
          },
        });
        ct = res.headers.get("content-type") || "";
        status = res.status;
        if (ct.indexOf("application/json") >= 0) {
          body = await res.json();
        } else {
          body = { _text: (await res.text()).slice(0, 800) };
        }
      } catch (e) {
        err = String(e && e.message ? e.message : e);
      }
      const event_count = (body && Array.isArray(body.value)) ? body.value.length : -1;
      const attempt = {
        endpoint: ep.name,
        url: ep.url,
        token_aud: tok.aud,
        token_target: tok.target.slice(0, 80),
        status, content_type: ct, error: err,
        event_count,
        elapsed_ms: Date.now() - t0,
      };
      attempts.push(attempt);
      if (res && res.ok && event_count >= 0) {
        winner = { ...attempt, body };
        break;
      }
    }
    if (winner) break;
  }

  sendToApp({
    type: "owa_response", request_id: requestId,
    verb: "bg-fetch-calendar",
    ok: !!winner,
    status: winner ? winner.status : 0,
    url: winner ? winner.url : "",
    body: { winner, attempts, tab_url: tab.url, tokens_seen: tokens.length },
    headers: {},
    error: winner ? "" : "no_token_endpoint_combination_succeeded",
    owa_build: "",
    tab_url: tab.url || "",
  });
}

// Helper: extract every JWT in the OWA tab's localStorage that's
// audience-matched for outlook.office[365].com. Used by both the
// calendar and people fetchers; centralized so a future token cache
// or refresh path lives in one place.
async function harvestExchangeTokens(tabId) {
  const [result] = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    func: () => {
      const now = Math.floor(Date.now() / 1000);
      const out = [];
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (!k || k.indexOf("accesstoken") < 0) continue;
        let parsed;
        try { parsed = JSON.parse(localStorage.getItem(k) || ""); }
        catch (_) { continue; }
        if (!parsed) continue;
        const secret = parsed.secret || "";
        if (typeof secret !== "string" || !secret.startsWith("eyJ")) continue;
        const exp = Number(parsed.expiresOn || 0);
        if (exp && exp < now) continue;
        let aud = "";
        try {
          const parts = secret.split(".");
          if (parts.length === 3) {
            let p = parts[1].replace(/-/g, "+").replace(/_/g, "/");
            while (p.length % 4 !== 0) p += "=";
            aud = (JSON.parse(atob(p)).aud) || "";
          }
        } catch (_) { /* opaque token */ }
        out.push({
          aud, target: parsed.target || "", secret,
          expires_in: exp ? exp - now : -1,
        });
      }
      // Prefer Outlook tokens; fall back to others so the caller can
      // still try them.
      out.sort((a, b) => {
        const ao = (a.aud || "").indexOf("outlook.office") >= 0 ? 0 : 1;
        const bo = (b.aud || "").indexOf("outlook.office") >= 0 ? 0 : 1;
        return ao - bo;
      });
      return out;
    },
  });
  return (result && result.result) || [];
}


// /people endpoint: looks up a person by email and returns enrichment
// fields (JobTitle, CompanyName, Department, OfficeLocation, etc.).
// External invitees -- people on a different tenant from the OWA user
// -- return a sparser response (DisplayName + Address only), which is
// exactly the prod app's parity with the COM path.
async function runBgFetchPeople(requestId, params) {
  const tabs = await listOwaTabs("calendar");
  if (tabs.length === 0) {
    sendToApp({
      type: "owa_error", request_id: requestId, code: "no_owa_tab",
      detail: "no Outlook tab open",
    });
    return;
  }
  const tab = tabs[0];
  const email = (params.email || "").trim();
  if (!email) {
    sendToApp({
      type: "owa_error", request_id: requestId, code: "missing_email",
      detail: "params.email is required",
    });
    return;
  }
  const tokens = await harvestExchangeTokens(tab.id);
  const exoTokens = tokens.filter((t) => (t.aud || "").indexOf("outlook.office") >= 0);
  if (exoTokens.length === 0) {
    sendToApp({
      type: "owa_error", request_id: requestId, code: "no_exchange_token",
      detail: "no Outlook-audienced token in localStorage; "
        + "navigate to /calendar/ in OWA so the SPA mints one",
    });
    return;
  }

  const endpoints = [
    {
      name: "outlook.office365.com_people",
      url: "https://outlook.office365.com/api/v2.0/me/people"
        + "?$search=" + encodeURIComponent('"' + email + '"') + "&$top=5",
    },
    {
      name: "outlook.office.com_people",
      url: "https://outlook.office.com/api/v2.0/me/people"
        + "?$search=" + encodeURIComponent('"' + email + '"') + "&$top=5",
    },
  ];

  const attempts = [];
  let winner = null;
  for (const tok of exoTokens) {
    for (const ep of endpoints) {
      const t0 = Date.now();
      let res, err = "", body = null, ct = "", status = 0;
      try {
        res = await fetch(ep.url, {
          method: "GET",
          headers: {
            "Authorization": "Bearer " + tok.secret,
            "Accept": "application/json",
          },
        });
        ct = res.headers.get("content-type") || "";
        status = res.status;
        if (ct.indexOf("application/json") >= 0) body = await res.json();
        else body = { _text: (await res.text()).slice(0, 500) };
      } catch (e) {
        err = String(e && e.message ? e.message : e);
      }
      const result_count = (body && Array.isArray(body.value)) ? body.value.length :
        (body && Array.isArray(body.Value)) ? body.Value.length : -1;
      const attempt = {
        endpoint: ep.name, url: ep.url,
        token_aud: tok.aud, token_target: (tok.target || "").slice(0, 80),
        status, content_type: ct, error: err,
        result_count, elapsed_ms: Date.now() - t0,
      };
      attempts.push(attempt);
      if (res && res.ok) {
        winner = { ...attempt, body };
        break;
      }
    }
    if (winner) break;
  }

  sendToApp({
    type: "owa_response", request_id: requestId, verb: "bg-fetch-people",
    ok: !!winner, status: winner ? winner.status : 0,
    url: winner ? winner.url : "",
    body: { winner, attempts, queried_email: email, tab_url: tab.url },
    headers: {}, error: winner ? "" : "no_people_endpoint_succeeded",
    owa_build: "", tab_url: tab.url || "",
  });
}


async function runListTabs(requestId) {
  const allTabs = await chrome.tabs.query({});
  const matchTabs = await chrome.tabs.query({
    url: ["*://outlook.office.com/*", "*://outlook.office365.com/*"],
  });
  const perms = await chrome.permissions.getAll();
  sendToApp({
    type: "owa_response", request_id: requestId, verb: "list-tabs",
    ok: true, status: 200, url: "", headers: {},
    body: {
      total_tabs: allTabs.length,
      tabs: allTabs.map((t) => ({
        id: t.id, url: t.url || "(no url, may need permission)",
        title: (t.title || "").slice(0, 60),
        active: t.active, windowId: t.windowId,
      })),
      matched_tabs: matchTabs.map((t) => ({
        id: t.id, url: t.url, title: t.title,
      })),
      manifest_host_permissions: perms.origins || [],
      manifest_permissions: perms.permissions || [],
    },
    error: "", owa_build: "",
  });
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
