// OWA probe content script. Listens for OWA_FETCH requests from the
// service worker, dispatches to the right OWA internal endpoint with
// the user's cookies, returns the result.
//
// Verbs supported:
//   calendar.fetch       params: { start_iso, end_iso }
//   people.lookup        params: { email }
//   attachments.list     params: { event_id }
//   attachments.fetch    params: { event_id, attachment_id }
//
// All endpoints live under /owa/0/api/v2.0/me/. The OWA SPA itself
// calls the same URLs, so the auth surface is identical -- our calls
// look like additional OWA UI activity to the server.

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || msg.type !== "OWA_FETCH") return;
  MN_PROBE.log("recv OWA_FETCH", msg.verb, "rid=" + msg.request_id);
  handleVerb(msg)
    .then((result) => sendResponse(result))
    .catch((e) => {
      MN_PROBE.log("verb threw", e);
      sendResponse({
        ok: false,
        status: 0,
        url: "",
        body: null,
        headers: {},
        error: "verb_threw: " + (e && e.message ? e.message : String(e)),
        owa_build: MN_PROBE.owaBuild(),
      });
    });
  return true; // tell Chrome we'll respond asynchronously
});

async function handleVerb(msg) {
  const { verb, params } = msg;
  switch (verb) {
    case "calendar.fetch":
      return calendarFetch(params || {});
    case "people.lookup":
      return peopleLookup(params || {});
    case "attachments.list":
      return attachmentsList(params || {});
    case "attachments.fetch":
      return attachmentsFetch(params || {});
    case "diagnose":
      return diagnose(params || {});
    default:
      return {
        ok: false,
        status: 0,
        url: "",
        body: null,
        headers: {},
        error: "unknown_verb: " + String(verb),
        owa_build: MN_PROBE.owaBuild(),
      };
  }
}

// Diagnostic verb: introspect the OWA tab's runtime + probe a set of
// candidate API roots. Returns a structured report so we can pick the
// right endpoint without iterating the manifest's host_permissions.
async function diagnose(params) {
  const report = {
    location_href: location.href,
    location_origin: location.origin,
    document_title: document.title,
    has_window_owa: typeof window.Owa !== "undefined",
    owa_local_settings: null,
    meta_tags: {},
    cookies_sample: document.cookie ? "(present, len=" + document.cookie.length + ")" : "(none)",
    candidate_probes: [],
  };

  // Meta tag dump -- the OWA SPA emits a ton of these and they often
  // include the API path + build hash.
  document.querySelectorAll("meta[name]").forEach((m) => {
    const name = m.getAttribute("name");
    const value = m.getAttribute("content");
    if (name && value !== null) {
      report.meta_tags[name] = value;
    }
  });

  // window.Owa exposes some build + endpoint config when the SPA has
  // booted. Snapshot the parts that are likely to point at the real
  // API.
  try {
    if (window.Owa) {
      const o = window.Owa;
      report.owa_local_settings = {
        keys: Object.keys(o).slice(0, 50),
        localSettings: o.LocalSettings ? Object.keys(o.LocalSettings).slice(0, 30) : null,
      };
    }
  } catch (e) {
    report.owa_local_settings = { error: String(e) };
  }

  // Probe each candidate API root. We're looking for one that returns
  // JSON content-type with a real status code. The candidates are
  // ordered from most-modern to most-legacy.
  const start = new Date(Date.now() - 12 * 3600 * 1000).toISOString();
  const end = new Date(Date.now() + 36 * 3600 * 1000).toISOString();
  const candidates = [
    {
      name: "owa_action_GetCalendarView_POST",
      method: "POST",
      url: location.origin +
        "/owa/service.svc?action=GetCalendarView&app=Calendar",
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
          FolderId: { __type: "FolderId:#Exchange", BaseFolderId: { __type: "DistinguishedFolderId:#Exchange", Id: "calendar" }, },
        },
      }),
      headers: {
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
        "X-OWA-CANARY": _readCookie("X-OWA-CANARY"),
      },
    },
    {
      name: "graph_v10_calendarview",
      method: "GET",
      url: location.origin + "/api/Calendar/EventsViewV2" +
        "?startDateTime=" + encodeURIComponent(start) +
        "&endDateTime=" + encodeURIComponent(end),
      headers: { "Accept": "application/json" },
    },
    {
      name: "owa_internal_api_calendarview",
      method: "GET",
      url: location.origin + "/owa/0/api/v2.0/me/calendarview" +
        "?startDateTime=" + encodeURIComponent(start) +
        "&endDateTime=" + encodeURIComponent(end),
      headers: { "Accept": "application/json" },
    },
    {
      name: "outlook_office365_internal",
      method: "GET",
      url: "https://outlook.office365.com/owa/0/api/v2.0/me/calendarview" +
        "?startDateTime=" + encodeURIComponent(start) +
        "&endDateTime=" + encodeURIComponent(end),
      headers: { "Accept": "application/json" },
    },
  ];

  for (const c of candidates) {
    try {
      const r = await fetch(c.url, {
        method: c.method,
        credentials: "include",
        headers: c.headers,
        body: c.body || undefined,
      });
      const ct = r.headers.get("content-type") || "";
      const bodyPreview = ct.indexOf("json") >= 0
        ? await r.json().then((j) => ({ keys: Object.keys(j).slice(0, 8) }))
            .catch((e) => "json_parse_error: " + e.message)
        : (await r.text().then((t) => t.slice(0, 240)).catch(() => "(unreadable)"));
      report.candidate_probes.push({
        name: c.name,
        url: c.url,
        status: r.status,
        content_type: ct,
        body_preview: bodyPreview,
        ok: r.ok,
      });
    } catch (e) {
      report.candidate_probes.push({
        name: c.name,
        url: c.url,
        error: String(e && e.message ? e.message : e),
      });
    }
  }

  return {
    ok: true,
    status: 200,
    url: location.href,
    body: report,
    headers: {},
    error: "",
    owa_build: MN_PROBE.owaBuild(),
  };
}

function _readCookie(name) {
  const all = document.cookie || "";
  const parts = all.split(";");
  for (const p of parts) {
    const eq = p.indexOf("=");
    if (eq < 0) continue;
    const k = p.slice(0, eq).trim();
    if (k === name) return decodeURIComponent(p.slice(eq + 1));
  }
  return "";
}

function calendarFetch(params) {
  const start = params.start_iso || "";
  const end = params.end_iso || "";
  if (!start || !end) {
    return Promise.resolve({
      ok: false,
      status: 0,
      url: "",
      body: null,
      headers: {},
      error: "missing_params: start_iso and end_iso required",
      owa_build: MN_PROBE.owaBuild(),
    });
  }
  // /calendarview is the documented Microsoft-Graph-shaped equivalent
  // OWA exposes internally; the response shape is close to Graph's
  // /me/calendarview output. Top-level "value" is the event list.
  const qs = new URLSearchParams({
    startDateTime: start,
    endDateTime: end,
    $top: "100",
    $select: [
      "id",
      "subject",
      "start",
      "end",
      "location",
      "organizer",
      "attendees",
      "body",
      "hasAttachments",
      "isOnlineMeeting",
      "onlineMeetingUrl",
      "onlineMeeting",
      "iCalUId",
      "showAs",
      "type",
      "webLink",
    ].join(","),
    $orderby: "start/dateTime",
  });
  return MN_PROBE.owaFetch("/calendarview?" + qs.toString());
}

function peopleLookup(params) {
  const email = (params.email || "").trim();
  if (!email) {
    return Promise.resolve({
      ok: false,
      status: 0,
      url: "",
      body: null,
      headers: {},
      error: "missing_params: email required",
      owa_build: MN_PROBE.owaBuild(),
    });
  }
  // /people returns People-shaped entries with JobTitle / CompanyName /
  // Department for tenant-resolved contacts. External invitees return
  // a sparser shape (DisplayName + email only).
  const qs = new URLSearchParams({
    $search: '"' + email + '"',
    $top: "5",
    $select: [
      "displayName",
      "scoredEmailAddresses",
      "givenName",
      "surname",
      "jobTitle",
      "companyName",
      "department",
      "officeLocation",
      "personType",
    ].join(","),
  });
  return MN_PROBE.owaFetch("/people?" + qs.toString());
}

function attachmentsList(params) {
  const id = params.event_id || "";
  if (!id) {
    return Promise.resolve({
      ok: false,
      status: 0,
      url: "",
      body: null,
      headers: {},
      error: "missing_params: event_id required",
      owa_build: MN_PROBE.owaBuild(),
    });
  }
  // OWA's event IDs are opaque URL-safe strings; encode defensively
  // even though they don't usually contain reserved chars.
  return MN_PROBE.owaFetch(
    "/events/" + encodeURIComponent(id) + "/attachments?$select=id,name,contentType,size,isInline",
  );
}

async function attachmentsFetch(params) {
  const eventId = params.event_id || "";
  const attId = params.attachment_id || "";
  if (!eventId || !attId) {
    return {
      ok: false,
      status: 0,
      url: "",
      body: null,
      headers: {},
      error: "missing_params: event_id + attachment_id required",
      owa_build: MN_PROBE.owaBuild(),
    };
  }
  // $value returns the raw bytes. Our owaFetch detects non-JSON
  // response and base64-encodes the body. Caller is responsible for
  // chunking if the result exceeds Chrome's 1 MB native-messaging cap.
  return MN_PROBE.owaFetch(
    "/events/" + encodeURIComponent(eventId) +
      "/attachments/" + encodeURIComponent(attId) + "/$value",
  );
}

MN_PROBE.log("content script loaded v" + MN_PROBE.VERSION + " on " + location.host);
