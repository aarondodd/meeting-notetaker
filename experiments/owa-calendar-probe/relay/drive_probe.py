"""Headless CLI driver for the OWA Calendar Probe.

Starts the bridge, waits for the extension's service worker to call
connectNative(), fires the requested verbs, prints + captures the
responses, exits.

Built for ad-hoc validation runs ("does the OWA flow actually work?")
where the relay GUI is overkill. The GUI uses identical plumbing --
anything that works here will work there.

Usage::

    python relay/drive_probe.py fetch-today
    python relay/drive_probe.py fetch-days --days 7
    python relay/drive_probe.py people --email someone@example.com
    python relay/drive_probe.py wait-only       # just confirm connection

Set MN_PROBE_KEEP_EMAILS=1 to disable redaction in captures + stdout.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_THIS = Path(__file__).resolve()
if str(_THIS.parent.parent) not in sys.path:
    sys.path.insert(0, str(_THIS.parent.parent))

from relay import paths  # noqa: E402
from relay.bridge import Bridge  # noqa: E402
from relay.capture import CaptureLog  # noqa: E402
from relay.parser import (  # noqa: E402
    parse_attachments_list,
    parse_calendarview,
    parse_people_lookup,
)


log = logging.getLogger("drive_probe")


class _Pending:
    """Single-request waiter. The CLI fires one verb at a time."""

    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        self.message: dict | None = None
        self.event = threading.Event()


def _on_state(state: str, detail: str) -> None:
    log.info("bridge state -> %s (%s)", state, detail or "-")


def _wait_for_connection(state_log: list, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(s[0] == "connected" for s in state_log):
            return True
        time.sleep(0.1)
    return False


def _print_calendar_summary(meetings) -> None:
    if not meetings:
        print("  (no events returned)")
        return
    for m in meetings:
        start = m.start_utc.astimezone().strftime("%Y-%m-%d %H:%M") if m.start_utc else "?"
        end = m.end_utc.astimezone().strftime("%H:%M") if m.end_utc else "?"
        att_count = len(m.attendees)
        marker = " [att]" if m.has_attachments else ""
        loc = f" @ {m.location}" if m.location else ""
        print(f"  {start}-{end}{loc}  {m.subject}  ({att_count} attendees){marker}")


def _print_people_summary(people: list[dict]) -> None:
    if not people:
        print("  (no person resolved -- external email or no /people match)")
        return
    for p in people:
        title = p.get("job_title") or "(no title)"
        company = p.get("company_name") or ""
        dept = p.get("department") or ""
        extras = " / ".join(filter(None, [company, dept]))
        print(f"  {p.get('display_name')}: {title}{f' -- {extras}' if extras else ''}")


def _print_attachments_summary(atts) -> None:
    if not atts:
        print("  (no attachments)")
        return
    for a in atts:
        print(f"  {a.name}  ({a.size} bytes, {a.content_type}, id={a.attachment_id[:24]}...)")


def _print_list_tabs(body: dict) -> None:
    if not isinstance(body, dict):
        print(f"  unexpected body type: {type(body)}")
        return
    print(f"  granted host_permissions: {body.get('manifest_host_permissions')}")
    print(f"  granted permissions     : {body.get('manifest_permissions')}")
    print(f"  total tabs visible: {body.get('total_tabs')}")
    print(f"  outlook tabs matched: {len(body.get('matched_tabs', []))}")
    for t in body.get("matched_tabs", []):
        print(f"    [match] id={t.get('id')} url={t.get('url')[:100]}")
    print("  all tabs:")
    for t in body.get("tabs", []):
        url = t.get("url") or "(none)"
        marker = " *" if "outlook" in url else "  "
        print(f"   {marker} id={t.get('id'):>4} {url[:100]}")


def _print_bg_fetch_calendar(body: dict) -> None:
    if not isinstance(body, dict):
        print(f"  unexpected body type: {type(body)}")
        return
    print(f"  tab_url    : {body.get('tab_url')}")
    print(f"  tokens seen: {body.get('tokens_seen')}")
    attempts = body.get("attempts") or []
    print(f"  attempts   : {len(attempts)}")
    for a in attempts:
        status = a.get("status", 0)
        ct = (a.get("content_type") or "")[:32]
        ev = a.get("event_count", -1)
        if status:
            mark = "OK  " if 200 <= status < 300 else "FAIL"
            print(f"    [{mark}] {a.get('endpoint')}  status={status} "
                  f"ct={ct} elapsed={a.get('elapsed_ms')}ms events={ev}")
        else:
            print(f"    [ERR ] {a.get('endpoint')}  err={a.get('error', '?')!r} "
                  f"elapsed={a.get('elapsed_ms')}ms")
        print(f"           token_aud={a.get('token_aud')!r}")
    winner = body.get("winner")
    if winner:
        raw = winner.get("body") or {}
        meetings = parse_calendarview(raw)
        print(f"\n  WINNER: {winner.get('endpoint')} -- parsed {len(meetings)} meetings")
        for m in meetings[:20]:
            start = m.start_utc.astimezone().strftime("%Y-%m-%d %H:%M") if m.start_utc else "?"
            end = m.end_utc.astimezone().strftime("%H:%M") if m.end_utc else "?"
            online = " [Teams]" if m.is_online_meeting else ""
            att_count = len(m.attendees)
            print(f"    {start}-{end}  {m.subject}  ({att_count} attendees){online}")
            for a in m.attendees:
                print(f"      attendee: {a.name} <{a.email}> ({a.attendee_type})")
    else:
        print("\n  no winner -- check token audiences vs endpoint hosts above.")


def _print_bg_fetch_people(body: dict) -> None:
    if not isinstance(body, dict):
        print(f"  unexpected body type: {type(body)}")
        return
    print(f"  queried_email: {body.get('queried_email')}")
    print(f"  tab_url      : {body.get('tab_url')}")
    attempts = body.get("attempts") or []
    print(f"  attempts     : {len(attempts)}")
    for a in attempts:
        status = a.get("status", 0)
        rc = a.get("result_count", -1)
        if status:
            mark = "OK  " if 200 <= status < 300 else "FAIL"
            print(f"    [{mark}] {a.get('endpoint')}  status={status} "
                  f"results={rc} elapsed={a.get('elapsed_ms')}ms")
        else:
            print(f"    [ERR ] {a.get('endpoint')}  err={a.get('error')!r}")
    winner = body.get("winner")
    if winner:
        raw = winner.get("body") or {}
        people = parse_people_lookup(raw)
        print(f"\n  WINNER: {winner.get('endpoint')} -- parsed {len(people)} entries")
        for p in people:
            title = p.get("job_title") or "(no title)"
            company = p.get("company_name") or ""
            dept = p.get("department") or ""
            extras = " / ".join(filter(None, [company, dept]))
            print(f"    {p.get('display_name')} <{p.get('email')}> -- {title}"
                  f"{f' ({extras})' if extras else ''}")
            print(f"      person_type: {p.get('person_type')!r}")
        if not people:
            print("    (no person matched; external invitee or tenant doesn't index them)")
    else:
        print("\n  no winner -- see attempt errors above.")


def _print_try_all_tokens(body: dict) -> None:
    if not isinstance(body, dict):
        print(f"  unexpected body type: {type(body)}")
        return
    attempts = body.get("attempts") or []
    winner = body.get("winner")
    # Group by endpoint, show the best status per endpoint per token.
    print(f"  total attempts: {len(attempts)}")
    by_status = {}
    for a in attempts:
        s = a.get("status", "?")
        by_status[s] = by_status.get(s, 0) + 1
    print(f"  status distribution: {dict(sorted(by_status.items(), key=str))}")
    # Show every attempt that wasn't a 401 -- those are the signal.
    interesting = [a for a in attempts if a.get("status") not in (401, 403)]
    if not interesting:
        print("  every attempt was 401/403; no token had the right audience.")
        # Show the first 401 so we can see WWW-Authenticate / token details.
        first = attempts[0] if attempts else None
        if first:
            print(f"    sample: endpoint={first.get('endpoint')} "
                  f"token_aud={first.get('token_aud')!r}")
    else:
        print(f"  non-401/403 attempts ({len(interesting)}):")
        for a in interesting:
            print(f"    status={a.get('status')} endpoint={a.get('endpoint')}")
            print(f"      token_aud={a.get('token_aud')!r} target={a.get('token_target')!r}")
            if a.get("preview"):
                print(f"      preview: {a.get('preview')[:200]}")
            if a.get("event_count", -1) >= 0:
                print(f"      events: {a.get('event_count')}")
    if winner:
        print(f"\n  WINNER: {winner.get('endpoint')} (events={winner.get('event_count')})")
        body_inner = winner.get("body") or {}
        events = body_inner.get("value") or []
        for ev in events[:15]:
            subj = (ev.get("subject") or "(no subject)")[:80]
            start = (ev.get("start") or {}).get("dateTime", "?")[:19]
            print(f"    {start}  {subj}")


def _print_inspect_storage(body: dict) -> None:
    if not isinstance(body, dict):
        print(f"  unexpected body type: {type(body)}")
        return
    print(f"  localStorage entries: {body.get('ls_size')}")
    candidates = body.get("token_candidates") or []
    exo = [c for c in candidates if c.get("is_exchange")]
    print(f"  total accesstoken keys: {len(candidates)} (exchange-online: {len(exo)})")
    for c in exo[:5]:
        print(f"    EXO target={c.get('target')[:70]!r}")
        print(f"        realm={c.get('realm')} env={c.get('environment')} "
              f"expires_on={c.get('expires_on')}")
        print(f"        is_jwt={c.get('is_jwt')} preview={c.get('secret_preview')}")
    for c in candidates:
        if c.get("is_exchange"):
            continue
        print(f"    other client_id={c.get('client_id')} target={c.get('target')[:50]!r}")


def _print_fetch_authed(body: dict) -> None:
    if not isinstance(body, dict):
        print(f"  unexpected body type: {type(body)}")
        return
    attempts = body.get("attempts") or []
    print(f"  attempts: {len(attempts)}")
    for a in attempts:
        status = a.get("status", "?")
        ct = (a.get("content_type") or "")[:30]
        size = a.get("body_size")
        ok = "OK  " if a.get("ok") else "FAIL"
        print(f"    [{ok}] {a.get('name')} status={status} ct={ct} "
              f"elapsed={a.get('elapsed_ms')}ms events={size}")
        for k, v in (a.get("headers") or {}).items():
            print(f"           hdr {k}: {v[:160]}")
    tok = body.get("token_info")
    if tok:
        print(f"  token: target={tok.get('target')[:80]!r} "
              f"expires_in={tok.get('expires_in')}s "
              f"realm={tok.get('realm')}")
    endpoint = body.get("endpoint_used")
    if endpoint:
        print(f"  endpoint used: {endpoint}")
        inner = body.get("body")
        if isinstance(inner, dict) and isinstance(inner.get("value"), list):
            events = inner["value"]
            print(f"  events returned: {len(events)}")
            for ev in events[:10]:
                subj = (ev.get("subject") or "(no subject)")[:80]
                start = (ev.get("start") or {}).get("dateTime", "?")[:19]
                org = ((ev.get("organizer") or {}).get("emailAddress") or {}).get("name", "?")
                print(f"    {start}  {subj}  [organizer: {org}]")
    elif body.get("error"):
        print(f"  ERROR: {body.get('error')}")
        det = body.get("detail")
        if det:
            print(f"    detail: {det}")


def _print_diagnose_main(body: dict) -> None:
    if not isinstance(body, dict):
        print(f"  unexpected body type: {type(body)}")
        return
    print(f"  tab_url: {body.get('tab_url')}")
    print(f"  origin : {body.get('origin')}")
    probes = body.get("candidate_probes") or []
    print(f"  main-world probes ({len(probes)}):")
    for p in probes:
        if p.get("error") and not p.get("status"):
            print(f"    [ERR ] {p['name']}: {p.get('error')}")
            continue
        marker = "OK  " if p.get("ok") else "FAIL"
        ct = (p.get("content_type") or "")[:40]
        elapsed = p.get("elapsed_ms", "?")
        print(
            f"    [{marker}] {p['name']} ({p.get('method')})"
            f"  status={p.get('status')} ct={ct} elapsed={elapsed}ms"
        )
        if p.get("final_url") and p.get("final_url") != p.get("url"):
            print(f"           final_url: {p['final_url']}")
        hints = p.get("header_hints") or {}
        for k, v in hints.items():
            print(f"           hdr {k}: {v[:200]}")
        preview = p.get("body_preview")
        if isinstance(preview, dict):
            print(f"           json keys: {preview.get('keys')}")
        elif isinstance(preview, str):
            preview_clean = " ".join(preview.split())
            print(f"           preview: {preview_clean[:400]}")


def _print_diagnose(body: dict) -> None:
    if not isinstance(body, dict):
        print(f"  unexpected body type: {type(body)}")
        return
    print(f"  location_href : {body.get('location_href')}")
    print(f"  document_title: {body.get('document_title')}")
    print(f"  cookies       : {body.get('cookies_sample')}")
    print(f"  window.Owa    : {body.get('has_window_owa')}")
    metas = body.get("meta_tags") or {}
    interesting = ("scriptVer", "hashedPath", "physicalRing", "environment",
                   "publicUrl", "businessCanonicalHostName", "OutlookBuildVersion",
                   "webServerForest", "owaIsAuthenticated")
    print("  interesting meta tags:")
    for k in interesting:
        if k in metas:
            v = metas[k]
            print(f"    {k} = {v[:120]}")
    probes = body.get("candidate_probes") or []
    print(f"  candidate probes ({len(probes)}):")
    for p in probes:
        if "error" in p:
            print(f"    [ERR ] {p['name']}: {p['error']}")
        else:
            ok = "OK " if p.get("ok") else "FAIL"
            preview = p.get("body_preview")
            if isinstance(preview, dict):
                preview = "json keys=" + str(preview.get("keys"))
            elif isinstance(preview, str):
                preview = preview.replace("\n", " ")[:120]
            print(f"    [{ok}] {p['name']}: status={p.get('status')} "
                  f"ct={p.get('content_type', '')[:40]}")
            print(f"           preview: {preview}")


def run(args) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    state_log: list[tuple[str, str]] = []
    inbox: dict[str, _Pending] = {}
    capture = CaptureLog()

    def on_state(state: str, detail: str) -> None:
        state_log.append((state, detail))
        _on_state(state, detail)

    def on_message(msg: dict) -> None:
        msg_type = msg.get("type", "")
        rid = msg.get("request_id", "")
        if msg_type in ("owa_response", "owa_error"):
            pending = inbox.get(rid)
            if pending is not None:
                pending.message = msg
                pending.event.set()
            verb = msg.get("verb", "?")
            ok = msg.get("ok", False)
            status = msg.get("status", 0)
            log.info(
                "%s rid=%s verb=%s ok=%s status=%s",
                msg_type, rid, verb, ok, status,
            )
        else:
            log.info("bridge rx: %s rid=%s", msg_type, rid)

    bridge = Bridge(on_message=on_message, on_state_change=on_state)
    port = bridge.start()
    print(f"relay listening on 127.0.0.1:{port}; bridge.json -> {paths.handshake_path()}")
    print(
        f"waiting up to {args.wait}s for Edge service worker to call "
        f"connectNative('com.meeting_notetaker.probe')..."
    )

    try:
        if not _wait_for_connection(state_log, timeout=args.wait):
            print(
                "ERROR: extension never connected. Check that:\n"
                "  1. The probe extension is enabled in edge://extensions\n"
                "  2. ID matches hllocpegdlgjbneinopdboclkekjljml\n"
                "  3. Re-run install_host.py if NMH manifest is missing\n"
                "  4. Service worker should ping every 30s on the alarm; "
                "try reloading the extension to force an immediate connect."
            )
            return 2

        print("connected. firing verbs...")

        verbs_to_run = _build_verb_list(args)
        if not verbs_to_run:
            print("no verbs requested -- 'wait-only' mode; exiting clean.")
            return 0

        ok_count = 0
        for verb, params, label in verbs_to_run:
            rid = f"cli-{verb.replace('.', '-')}-{int(time.time() * 1000)}"
            pending = _Pending(rid)
            inbox[rid] = pending
            payload = {
                "type": "owa_request",
                "request_id": rid,
                "verb": verb,
                "params": params,
            }
            capture.record_owa_request(
                verb=verb, request_id=rid, params=params,
            )
            if not bridge.send(payload):
                print(f"[FAIL] {label}: bridge not connected anymore")
                continue
            print(f"-> {label}  ({verb}, rid={rid[-8:]})")
            if not pending.event.wait(timeout=args.timeout):
                print(f"[FAIL] {label}: no response within {args.timeout}s")
                continue
            resp = pending.message or {}
            capture_path = capture.record_owa_response(
                verb=verb, request_id=rid, payload=resp,
            )
            print(f"   capture -> {capture_path.name}")

            if resp.get("type") == "owa_error":
                print(f"   ERROR code={resp.get('code')} detail={resp.get('detail')}")
                continue
            # The diagnostic verbs return useful bodies even on ok=False
            # (they're SUPPOSED to report failures). Don't short-circuit
            # those.
            diagnostic_verbs = {
                "try-all-tokens", "bg-fetch-calendar", "diagnose-main",
                "diagnose", "inspect-storage",
            }
            if not resp.get("ok") and verb not in diagnostic_verbs:
                print(
                    f"   HTTP {resp.get('status')}: {resp.get('error') or '(no error string)'}"
                )
                continue
            body = resp.get("body") or {}
            if verb == "calendar.fetch":
                meetings = parse_calendarview(body)
                print(f"   parsed {len(meetings)} meetings:")
                _print_calendar_summary(meetings)
            elif verb == "people.lookup":
                people = parse_people_lookup(body)
                print(f"   resolved {len(people)} entries:")
                _print_people_summary(people)
            elif verb == "attachments.list":
                atts = parse_attachments_list(body)
                print(f"   {len(atts)} attachments:")
                _print_attachments_summary(atts)
            elif verb == "diagnose":
                _print_diagnose(body)
            elif verb == "diagnose-main":
                _print_diagnose_main(body)
            elif verb == "inspect-storage":
                _print_inspect_storage(body)
            elif verb == "fetch-authed-calendar":
                _print_fetch_authed(body)
            elif verb == "try-all-tokens":
                _print_try_all_tokens(body)
            elif verb == "bg-fetch-calendar":
                _print_bg_fetch_calendar(body)
            elif verb == "list-tabs":
                _print_list_tabs(body)
            elif verb == "bg-fetch-people":
                _print_bg_fetch_people(body)
            else:
                print(f"   (raw body len={len(json.dumps(body))})")
            ok_count += 1

        print(f"\n{ok_count}/{len(verbs_to_run)} verbs succeeded.")
        print(f"owa_build seen: {resp.get('owa_build') or 'unknown'}")
        return 0 if ok_count == len(verbs_to_run) else 1
    finally:
        bridge.stop()
        time.sleep(0.1)


def _build_verb_list(args):
    cmd = args.command
    out = []
    if cmd == "wait-only":
        return out
    if cmd == "fetch-today":
        now = datetime.now()
        start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc) - timedelta(hours=12)
        end = start + timedelta(days=2)
        out.append((
            "calendar.fetch",
            {
                "start_iso": start.isoformat().replace("+00:00", "Z"),
                "end_iso": end.isoformat().replace("+00:00", "Z"),
            },
            "fetch today's calendar",
        ))
    elif cmd == "fetch-days":
        now = datetime.now(tz=timezone.utc)
        start = now - timedelta(hours=12)
        end = now + timedelta(days=max(1, args.days))
        out.append((
            "calendar.fetch",
            {
                "start_iso": start.isoformat().replace("+00:00", "Z"),
                "end_iso": end.isoformat().replace("+00:00", "Z"),
            },
            f"fetch next {args.days} days",
        ))
    elif cmd == "people":
        out.append((
            "people.lookup",
            {"email": args.email},
            f"resolve {args.email}",
        ))
    elif cmd == "diagnose":
        out.append((
            "diagnose",
            {},
            "inspect OWA runtime + probe API candidates",
        ))
    elif cmd == "diagnose-main":
        out.append((
            "diagnose-main",
            {},
            "probe OWA endpoints from page MAIN world (bearer auth)",
        ))
    elif cmd == "inspect-storage":
        out.append((
            "inspect-storage",
            {},
            "list MSAL access-token candidates in OWA localStorage",
        ))
    elif cmd == "fetch-authed":
        from datetime import datetime, timedelta, timezone as _tz
        now = datetime.now(tz=_tz.utc)
        start = now - timedelta(hours=12)
        end = now + timedelta(days=max(1, args.days))
        out.append((
            "fetch-authed-calendar",
            {
                "start_iso": start.isoformat().replace("+00:00", "Z"),
                "end_iso": end.isoformat().replace("+00:00", "Z"),
            },
            f"fetch calendar with stored bearer token (window {args.days}d)",
        ))
    elif cmd == "try-all-tokens":
        from datetime import datetime, timedelta, timezone as _tz
        now = datetime.now(tz=_tz.utc)
        start = now - timedelta(hours=12)
        end = now + timedelta(days=max(1, args.days))
        out.append((
            "try-all-tokens",
            {
                "start_iso": start.isoformat().replace("+00:00", "Z"),
                "end_iso": end.isoformat().replace("+00:00", "Z"),
            },
            f"brute-force every JWT in localStorage against 3 calendar endpoints",
        ))
    elif cmd == "bg-fetch":
        from datetime import datetime, timedelta, timezone as _tz
        now = datetime.now(tz=_tz.utc)
        start = now - timedelta(hours=12)
        end = now + timedelta(days=max(1, args.days))
        out.append((
            "bg-fetch-calendar",
            {
                "start_iso": start.isoformat().replace("+00:00", "Z"),
                "end_iso": end.isoformat().replace("+00:00", "Z"),
            },
            f"BG-SW fetch (bypasses page CSP/SW), audience-matched tokens",
        ))
    elif cmd == "list-tabs":
        out.append((
            "list-tabs", {}, "list every tab the extension can see",
        ))
    elif cmd == "bg-people":
        out.append((
            "bg-fetch-people",
            {"email": args.email},
            f"resolve {args.email} via /people endpoint",
        ))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "wait-only", "fetch-today", "fetch-days", "people",
            "diagnose", "diagnose-main", "inspect-storage", "fetch-authed",
            "try-all-tokens", "bg-fetch", "list-tabs", "bg-people",
        ),
        help="Which verb to fire after the extension connects.",
    )
    parser.add_argument(
        "--days", type=int, default=1,
        help="Day window for fetch-days (default 1).",
    )
    parser.add_argument(
        "--email", type=str, default="",
        help="Email to resolve for the 'people' command.",
    )
    parser.add_argument(
        "--wait", type=float, default=45.0,
        help="Seconds to wait for the extension to connect (default 45).",
    )
    parser.add_argument(
        "--timeout", type=float, default=30.0,
        help="Seconds to wait for each verb's response (default 30).",
    )
    args = parser.parse_args()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
