// Shared helpers for the OWA probe content script.
//
// Kept intentionally tiny -- this is sandbox code. Everything here is
// importable from owa-probe.js by reading the same content-script
// global scope (manifest declares this file first).

// eslint-disable-next-line no-unused-vars
const MN_PROBE = (function () {
  const VERSION = "0.0.1";

  // OWA's internal API is mounted at /owa/0/api/v2.0/me/*. The host is
  // whichever flavor of outlook.office* the user landed on. We trust
  // location.origin rather than hardcoding.
  function apiBase() {
    return location.origin + "/owa/0/api/v2.0/me";
  }

  // OWA's JS exposes a build version in a meta tag; capturing it with
  // every response lets us correlate breakage to a specific OWA build.
  function owaBuild() {
    const m = document.querySelector('meta[name="OutlookBuildVersion"]');
    if (m) return m.getAttribute("content") || "";
    // Fallback: some builds expose it on window.SuiteServiceProxy or
    // window.Owa. Probe defensively; never throw.
    try {
      if (window.Owa && window.Owa.LocalSettings && window.Owa.LocalSettings.build) {
        return String(window.Owa.LocalSettings.build);
      }
    } catch (_) { /* ignore */ }
    return "";
  }

  // Wrap fetch with credentials: 'include' so the user's OWA session
  // cookies ride along. Returns a plain object so it can be serialized
  // back to the service worker (which can't transfer Response objects).
  async function owaFetch(path, init) {
    const url = path.startsWith("http") ? path : apiBase() + path;
    const opts = Object.assign(
      {
        method: "GET",
        credentials: "include",
        headers: { Accept: "application/json" },
      },
      init || {},
    );
    let resp;
    try {
      resp = await fetch(url, opts);
    } catch (e) {
      return {
        ok: false,
        status: 0,
        url,
        body: null,
        headers: {},
        error: "fetch_failed: " + (e && e.message ? e.message : String(e)),
        owa_build: owaBuild(),
      };
    }
    const headers = {};
    resp.headers.forEach((v, k) => {
      // Trim to a known-safe subset; full headers can contain auth-y
      // values we don't want to log accidentally.
      if (k === "content-type" || k === "request-id" || k === "x-owa-versionhash") {
        headers[k] = v;
      }
    });
    let body = null;
    const ct = resp.headers.get("content-type") || "";
    try {
      if (ct.indexOf("application/json") >= 0) {
        body = await resp.json();
      } else if (ct.indexOf("text/") === 0) {
        body = { _text: await resp.text() };
      } else if (resp.body) {
        // Binary path (attachment $value). Read as ArrayBuffer and
        // base64-encode. Native messaging caps each message at 1MB, so
        // the caller is expected to chunk afterwards.
        const buf = await resp.arrayBuffer();
        body = { _b64: arrayBufferToBase64(buf), _bytes: buf.byteLength };
      }
    } catch (e) {
      return {
        ok: false,
        status: resp.status,
        url,
        body: null,
        headers,
        error: "decode_failed: " + (e && e.message ? e.message : String(e)),
        owa_build: owaBuild(),
      };
    }
    return {
      ok: resp.ok,
      status: resp.status,
      url,
      body,
      headers,
      error: "",
      owa_build: owaBuild(),
    };
  }

  function arrayBufferToBase64(buf) {
    const bytes = new Uint8Array(buf);
    let s = "";
    const CHUNK = 0x8000;
    for (let i = 0; i < bytes.length; i += CHUNK) {
      s += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
    }
    return btoa(s);
  }

  function log(...args) {
    console.log("[mn-probe content]", ...args);
  }

  return { VERSION, apiBase, owaBuild, owaFetch, log };
})();
