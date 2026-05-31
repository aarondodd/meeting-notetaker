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
