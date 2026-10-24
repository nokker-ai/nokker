import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from nokker.connectors import net
from nokker.connectors.calendar import (
    CalendarConnector, build_ics, edit_ics, escape_text, events_between, fold, parse_ics, parse_input_time, split_line,
    unfold,
)
from nokker.connectors.errors import ConnectorError
from nokker.core.ports import ToolContext
from nokker.core.turn import Effect
from nokker.tools.effects import DEFAULT_EFFECTS

TZ = ZoneInfo("Europe/Zurich")
NOW = datetime(2026, 10, 21, 10, 0, tzinfo=TZ)
CTX = ToolContext(thread_id="t", turn_index=0, channel="web")
PASSWORD = "app-pass-s3cr3t"
SECRET_URL = "https://calendar.example.org/ical/tok-9f8e7d6c5b/basic.ics"


def vcal(*events: str) -> str:
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + "".join(e.strip().replace("\n", "\r\n") + "\r\n" for e in events) + "END:VCALENDAR\r\n"


def vevent(*lines: str) -> str:
    return "BEGIN:VEVENT\n" + "\n".join(lines) + "\nEND:VEVENT"


def between(ics: str, t0: datetime, t1: datetime):
    return events_between(parse_ics(ics, TZ).events, t0, t1, TZ)


# ───────────────────────────── parseur ─────────────────────────────


def test_unfold_and_split_line_with_quoted_params():
    assert unfold("SUMMARY:Une longue\r\n  ligne\r\nX:1\r\n") == ["SUMMARY:Une longue ligne", "X:1"]
    name, params, value = split_line('ATTENDEE;CN="Dupont; Jean:x";ROLE=REQ:mailto:j@ex.org')
    assert (name, params["CN"], params["ROLE"], value) == ("ATTENDEE", "Dupont; Jean:x", "REQ", "mailto:j@ex.org")


def test_utc_tzid_floating_and_all_day():
    ics = vcal(
        vevent("UID:a", "SUMMARY:UTC", "DTSTART:20261022T080000Z", "DTEND:20261022T090000Z"),
        vevent("UID:b", "SUMMARY:Paris", "DTSTART;TZID=Europe/Paris:20261022T140000", "DTEND;TZID=Europe/Paris:20261022T150000"),
        vevent("UID:c", "SUMMARY:Flottant", "DTSTART:20261023T090000", "DTEND:20261023T100000"),
        vevent("UID:d", "SUMMARY:Journée", "DTSTART;VALUE=DATE:20261024", "DTEND;VALUE=DATE:20261026"),
    )
    r = {e["id"]: e for e in between(ics, NOW, NOW + timedelta(days=10))}
    assert r["a"]["start"] == "2026-10-22T10:00+02:00" and r["a"]["all_day"] is False
    assert r["b"]["start"] == "2026-10-22T14:00+02:00"
    assert r["c"]["start"] == "2026-10-23T09:00+02:00"
    assert (r["d"]["start"], r["d"]["end"], r["d"]["all_day"]) == ("2026-10-24", "2026-10-25", True)  # fin incluse


def test_dst_change_is_rendered_in_account_timezone():
    ics = vcal(vevent("UID:z", "SUMMARY:x", "DTSTART:20261026T120000Z", "DTEND:20261026T130000Z"))
    (e,) = between(ics, datetime(2026, 10, 26, tzinfo=TZ), datetime(2026, 10, 27, tzinfo=TZ))
    assert e["start"] == "2026-10-26T13:00+01:00"  # heure d'hiver depuis le 25.10


def test_duration_instead_of_dtend():
    ics = vcal(vevent("UID:a", "SUMMARY:x", "DTSTART:20261022T080000Z", "DURATION:PT90M"))
    (e,) = between(ics, NOW, NOW + timedelta(days=3))
    assert (e["start"], e["end"]) == ("2026-10-22T10:00+02:00", "2026-10-22T11:30+02:00")


def test_text_escapes_and_folding_roundtrip():
    ics = vcal(vevent("UID:a", "SUMMARY:A\\, B\\; C\\\\ D", "DESCRIPTION:ligne1\\nligne2 très\r\n  longue", "LOCATION:Salle\\, 2",
                      "DTSTART:20261022T080000Z", "DTEND:20261022T090000Z"))
    (e,) = between(ics, NOW, NOW + timedelta(days=3))
    assert e["summary"] == "A, B; C\\ D"
    assert e["description"] == "ligne1\nligne2 très longue"
    assert e["location"] == "Salle, 2"


def test_attendees_strip_mailto_and_valarm_ignored():
    ics = vcal(vevent("UID:a", "SUMMARY:x", "DTSTART:20261022T080000Z", "DTEND:20261022T090000Z",
                      "ATTENDEE;CN=Jean:mailto:jean@example.org", "BEGIN:VALARM", "TRIGGER:-PT15M", "SUMMARY:alarme", "END:VALARM"))
    (e,) = between(ics, NOW, NOW + timedelta(days=3))
    assert e["attendees"] == ["jean@example.org"] and e["summary"] == "x"


def test_cancelled_and_out_of_window_are_dropped():
    ics = vcal(
        vevent("UID:a", "SUMMARY:annulé", "STATUS:CANCELLED", "DTSTART:20261022T080000Z", "DTEND:20261022T090000Z"),
        vevent("UID:b", "SUMMARY:loin", "DTSTART:20270101T080000Z", "DTEND:20270101T090000Z"),
        vevent("UID:c", "SUMMARY:passé", "DTSTART:20260101T080000Z", "DTEND:20260101T090000Z"),
        vevent("UID:d", "SUMMARY:en cours", "DTSTART:20261021T070000Z", "DTEND:20261021T090000Z"),
    )
    assert [e["id"] for e in between(ics, NOW, NOW + timedelta(days=7))] == ["d"]


def test_weekly_rrule_count_byday():
    ics = vcal(vevent("UID:w", "SUMMARY:Point", "DTSTART;TZID=Europe/Zurich:20261019T100000", "DTEND;TZID=Europe/Zurich:20261019T110000",
                      "RRULE:FREQ=WEEKLY;BYDAY=MO,WE;COUNT=5"))
    r = between(ics, datetime(2026, 10, 1, tzinfo=TZ), datetime(2026, 12, 1, tzinfo=TZ))
    assert [e["start"][:10] for e in r] == ["2026-10-19", "2026-10-21", "2026-10-26", "2026-10-28", "2026-11-02"]


def test_weekly_rrule_until_and_exdate_and_interval():
    ics = vcal(vevent("UID:w", "SUMMARY:Point", "DTSTART:20261019T080000Z", "DTEND:20261019T090000Z",
                      "RRULE:FREQ=WEEKLY;INTERVAL=2;UNTIL=20261130T000000Z", "EXDATE:20261102T080000Z"))
    r = between(ics, datetime(2026, 10, 1, tzinfo=TZ), datetime(2027, 1, 1, tzinfo=TZ))
    days = [e["start"][:10] for e in r]
    assert days == ["2026-10-19", "2026-11-16"]  # 02.11 exclu, 30.11 08:00Z > UNTIL 00:00Z


def test_recurrence_keeps_wall_time_across_dst():
    ics = vcal(vevent("UID:w", "SUMMARY:x", "DTSTART;TZID=Europe/Zurich:20261023T090000", "DTEND;TZID=Europe/Zurich:20261023T100000",
                      "RRULE:FREQ=DAILY;COUNT=4"))
    r = between(ics, datetime(2026, 10, 1, tzinfo=TZ), datetime(2026, 12, 1, tzinfo=TZ))
    assert [e["start"] for e in r] == ["2026-10-23T09:00+02:00", "2026-10-24T09:00+02:00", "2026-10-25T09:00+01:00",
                                       "2026-10-26T09:00+01:00"]


def test_monthly_skips_missing_day_and_yearly():
    m = vcal(vevent("UID:m", "SUMMARY:x", "DTSTART;VALUE=DATE:20261031", "RRULE:FREQ=MONTHLY;COUNT=3"))
    assert [e["start"] for e in between(m, datetime(2026, 10, 1, tzinfo=TZ), datetime(2027, 6, 1, tzinfo=TZ))] == [
        "2026-10-31", "2026-12-31", "2027-01-31"]
    y = vcal(vevent("UID:y", "SUMMARY:x", "DTSTART;VALUE=DATE:20261105", "RRULE:FREQ=YEARLY"))
    assert [e["start"] for e in between(y, datetime(2026, 10, 1, tzinfo=TZ), datetime(2028, 12, 1, tzinfo=TZ))] == [
        "2026-11-05", "2027-11-05", "2028-11-05"]


def test_recurrence_id_override_replaces_the_occurrence():
    ics = vcal(
        vevent("UID:w", "SUMMARY:Série", "DTSTART:20261021T080000Z", "DTEND:20261021T090000Z", "RRULE:FREQ=DAILY;COUNT=3"),
        vevent("UID:w", "SUMMARY:Déplacé", "RECURRENCE-ID:20261022T080000Z", "DTSTART:20261022T150000Z", "DTEND:20261022T160000Z"),
    )
    r = between(ics, datetime(2026, 10, 21, tzinfo=TZ), datetime(2026, 10, 30, tzinfo=TZ))
    assert [(e["summary"], e["start"]) for e in r] == [
        ("Série", "2026-10-21T10:00+02:00"), ("Déplacé", "2026-10-22T17:00+02:00"), ("Série", "2026-10-23T10:00+02:00")]


@pytest.mark.parametrize("rule", ["FREQ=MONTHLY;BYDAY=2TU", "FREQ=YEARLY;BYMONTH=3", "FREQ=HOURLY", "FREQ=WEEKLY;COUNT=2;UNTIL=20270101T000000Z"])
def test_unsupported_rrule_gives_first_occurrence_with_note(rule):
    ics = vcal(vevent("UID:u", "SUMMARY:x", "DTSTART:20261022T080000Z", "DTEND:20261022T090000Z", f"RRULE:{rule}"))
    r = between(ics, NOW, NOW + timedelta(days=90))
    assert len(r) == 1 and "récurrence non gérée" in r[0]["note"]


def test_broken_content_never_raises_and_non_ics_is_protocol_error():
    ics = vcal(vevent("UID:a", "SUMMARY:ok", "DTSTART:20261022T080000Z", "DTEND:20261022T090000Z"),
               vevent("UID:b", "SUMMARY:cassé", "DTSTART:pas-une-date"), vevent("SUMMARY:sans début"))
    p = parse_ics(ics, TZ)
    assert [e.uid for e in p.events] == ["a"] and p.warnings
    with pytest.raises(ConnectorError) as e:
        parse_ics("<html>login</html>", TZ)
    assert e.value.code == "protocol_error"


def test_unknown_tzid_falls_back_with_note():
    ics = vcal(vevent("UID:a", "SUMMARY:x", "DTSTART;TZID=W. Europe Standard Time:20261022T100000",
                      "DTEND;TZID=W. Europe Standard Time:20261022T110000"))
    (e,) = between(ics, NOW, NOW + timedelta(days=3))
    assert e["start"] == "2026-10-22T10:00+02:00" and "inconnu" in e["note"]


# ───────────────────────────── écriture ─────────────────────────────


def test_escape_text_blocks_line_injection():
    out = escape_text("x\r\nEND:VEVENT\r\nBEGIN:VEVENT\rATTENDEE:mailto:evil@x.org\x00")
    assert "\r" not in out and "\n" not in out and "\x00" not in out
    ics = build_ics([f"SUMMARY:{out}", "UID:u"])
    lines = ics.split("\r\n")
    assert lines.count("BEGIN:VEVENT") == 1 and lines.count("END:VEVENT") == 1
    assert not any(ln.startswith("ATTENDEE") for ln in lines)


def test_fold_is_utf8_safe_and_reversible():
    line = "SUMMARY:" + "é" * 100
    folded = fold(line)
    assert all(len(part.encode()) <= 75 for part in folded.split("\r\n"))
    assert unfold(folded) == [line]


def test_parse_input_time():
    d, all_day = parse_input_time("2026-10-27T14:00", TZ)
    assert (d.isoformat(), all_day) == ("2026-10-27T14:00:00+01:00", False)
    assert parse_input_time("2026-10-27", TZ)[1] is True
    assert parse_input_time("2026-10-27T14:00Z", TZ)[0].utcoffset() == timedelta(0)
    with pytest.raises(ValueError):
        parse_input_time("demain", TZ)


def test_edit_ics_keeps_unknown_properties_and_valarm():
    src = vcal(vevent("UID:a", "SUMMARY:vieux", "X-CUSTOM:garde", "SEQUENCE:2", "DTSTART:20261022T080000Z", "DTEND:20261022T090000Z",
                      "BEGIN:VALARM", "TRIGGER:-PT15M", "SUMMARY:alarme", "END:VALARM"))
    out = edit_ics(src, {"SUMMARY"}, ["SUMMARY:nouveau"])
    assert "X-CUSTOM:garde" in out and "SUMMARY:nouveau" in out and "SUMMARY:vieux" not in out
    assert "SUMMARY:alarme" in out and "SEQUENCE:3" in out and "BEGIN:VALARM" in out


# ───────────────────────────── mode iCal ─────────────────────────────


class Net:
    """Faux réseau : `routes(request) -> httpx.Response` ; garde la liste des requêtes vues."""

    def __init__(self, routes):
        self.routes, self.seen = routes, []

    def factory(self, host, port):
        def handler(req: httpx.Request) -> httpx.Response:
            self.seen.append((req.method, str(req.url), dict(req.headers), req.content))
            return self.routes(req)

        return None, httpx.Client(transport=httpx.MockTransport(handler))


def ical(routes, secret=SECRET_URL, **kw):
    n = Net(routes)
    return CalendarConnector({"mode": "ical", "label": "calendar.example.org"}, secret, client_factory=n.factory, now=lambda: NOW, **kw), n


ICAL_BODY = vcal(vevent("UID:a", "SUMMARY:Dentiste", "DTSTART:20261022T080000Z", "DTEND:20261022T090000Z"))


def run(c, name, **args):
    r = c.call(name, args, CTX)
    return r, (json.loads(r.content) if not r.is_error else r.content)


def test_ical_reads_events_and_only_exposes_calendar_events():
    c, n = ical(lambda req: httpx.Response(200, text=ICAL_BODY))
    assert [s.name for s in c.specs()] == ["calendar_events"]
    r, data = run(c, "calendar_events", days=3)
    assert not r.is_error and data["count"] == 1 and data["events"][0]["summary"] == "Dentiste"
    assert n.seen[0][0] == "GET" and "authorization" not in n.seen[0][2]


def test_ical_write_tools_are_refused_and_nothing_is_sent():
    c, n = ical(lambda req: httpx.Response(200, text=ICAL_BODY))
    for name, args in (("calendar_create", {"summary": "x", "start": "2026-10-27T14:00", "end": "2026-10-27T15:00"}),
                       ("calendar_update", {"event_id": "a", "summary": "x"}), ("calendar_delete", {"event_id": "a"})):
        r = c.call(name, args, CTX)
        assert r.is_error and "inconnu" in r.content
    assert n.seen == []


def test_ical_accepts_webcal_and_rejects_http_and_userinfo():
    c, n = ical(lambda req: httpx.Response(200, text=ICAL_BODY), secret="webcal://calendar.example.org/x/y.ics")
    run(c, "calendar_events")
    assert n.seen[0][1] == "https://calendar.example.org/x/y.ics"
    for bad in ("http://calendar.example.org/a.ics", "https://u:p@calendar.example.org/a.ics", "ftp://x/y"):
        with pytest.raises(ConnectorError) as e:
            CalendarConnector({"mode": "ical"}, bad)
        assert e.value.code == "bad_settings"


def test_ical_redirect_to_private_host_is_blocked_by_the_factory():
    def factory(host, port):
        hop = net.check_endpoint(host, port, "https", resolver=lambda h, p, proto=0: [(2, 1, 6, "", ("10.0.0.5" if h == "intra.example.org" else "93.184.216.34", p))])
        return hop, httpx.Client(transport=httpx.MockTransport(
            lambda req: httpx.Response(302, headers={"location": "https://intra.example.org/secret.ics"})))

    c = CalendarConnector({"mode": "ical"}, SECRET_URL, client_factory=factory, now=lambda: NOW)
    r, msg = run(c, "calendar_events")
    assert r.is_error and msg.startswith("host_blocked")


def test_ical_follows_at_most_three_redirects():
    c, _ = ical(lambda req: httpx.Response(302, headers={"location": str(req.url) + "x"}))
    r, msg = run(c, "calendar_events")
    assert r.is_error and "redirections" in msg


def test_ical_size_limit():
    c, _ = ical(lambda req: httpx.Response(200, content=b"A" * (5 * 1024 * 1024 + 1)))
    r, msg = run(c, "calendar_events")
    assert r.is_error and msg.startswith("protocol_error") and "volumineuse" in msg


def test_ical_html_page_is_protocol_error_and_http_errors_are_mapped():
    r, msg = run(ical(lambda req: httpx.Response(200, text="<html>"))[0], "calendar_events")
    assert r.is_error and msg.startswith("protocol_error")
    r, msg = run(ical(lambda req: httpx.Response(404))[0], "calendar_events")
    assert r.is_error and msg.startswith("protocol_error")
    r, msg = run(ical(lambda req: httpx.Response(403))[0], "calendar_events")
    assert r.is_error and msg.startswith("auth_failed")


def test_network_errors_never_leak_the_secret_url():
    def boom(req):
        raise httpx.ConnectError(f"all attempts failed for {req.url}")

    c, _ = ical(boom)
    r, msg = run(c, "calendar_events")
    assert r.is_error and msg.startswith("unreachable") and "tok-9f8e7d6c5b" not in msg and "calendar.example.org/ical" not in msg


def test_tls_error_is_mapped():
    import ssl

    def boom(req):
        err = httpx.ConnectError("certificate verify failed")
        err.__cause__ = ssl.SSLError("CERTIFICATE_VERIFY_FAILED")
        raise err

    r, msg = run(ical(boom)[0], "calendar_events")
    assert r.is_error and msg.startswith("tls_failed")


def test_ical_test_message():
    c, _ = ical(lambda req: httpx.Response(200, text=ICAL_BODY))
    assert "1 événement" in c.test()
    c, _ = ical(lambda req: httpx.Response(401))
    with pytest.raises(ConnectorError) as e:
        c.test()
    assert e.value.code == "auth_failed" and "tok-9f8e7d6c5b" not in e.value.message


def test_days_validation():
    c, _ = ical(lambda req: httpx.Response(200, text=ICAL_BODY))
    for bad in (0, 400, "abc"):
        r, msg = run(c, "calendar_events", days=bad)
        assert r.is_error and "days" in msg


# ───────────────────────────── mode CalDAV ─────────────────────────────

NS = 'xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"'


def ms(*responses: str) -> httpx.Response:
    return httpx.Response(207, text=f'<?xml version="1.0"?><d:multistatus {NS}>{"".join(responses)}</d:multistatus>',
                          headers={"content-type": "application/xml"})


def resp(href: str, props: str) -> str:
    return f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>{props}</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"


def cal_props(name: str, comps=("VEVENT",)) -> str:
    cs = "".join(f'<c:comp name="{c}"/>' for c in comps)
    return (f"<d:resourcetype><d:collection/><c:calendar/></d:resourcetype><d:displayname>{name}</d:displayname>"
            f"<c:supported-calendar-component-set>{cs}</c:supported-calendar-component-set>")


class FakeDav:
    HOST = "dav.example.org"

    def __init__(self):
        self.store: dict[str, tuple[str, str]] = {"/cal/u/work/abc.ics": (
            vcal(vevent("UID:ext-1", "SUMMARY:Réunion", "DTSTART:20261022T080000Z", "DTEND:20261022T090000Z", "SEQUENCE:0",
                        "X-CUSTOM:garde")), '"e1"')}
        self.log: list[tuple[str, str, dict[str, str], bytes]] = []
        self.n = 0
        self.require_auth = True

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.log.append((req.method, req.url.path, dict(req.headers), req.content))
        if self.require_auth and not req.headers.get("authorization", "").startswith("Basic "):
            return httpx.Response(401)
        p, m = req.url.path, req.method
        if m == "PROPFIND":
            if p in ("/", "/.well-known/caldav"):
                return ms(resp("/", "<d:current-user-principal><d:href>/principals/u/</d:href></d:current-user-principal>"))
            if p == "/principals/u/":
                return ms(resp(p, "<c:calendar-home-set><d:href>/cal/u/</d:href></c:calendar-home-set>"))
            if p == "/cal/u/":
                return ms(resp(p, "<d:resourcetype><d:collection/></d:resourcetype>"),
                          resp("/cal/u/work/", cal_props("Travail")), resp("/cal/u/home/", cal_props("Maison")),
                          resp("/cal/u/todo/", cal_props("Tâches", ("VTODO",))))
            return httpx.Response(404)
        if m == "REPORT":
            body = req.content.decode()
            rows = []
            for href, (ics, etag) in self.store.items():
                if not href.startswith(p):
                    continue
                if "<c:prop-filter" in body:
                    uid = body.split('i;octet">')[1].split("<")[0]
                    if f"UID:{uid}\r\n" not in ics:
                        continue
                rows.append(f"<d:response><d:href>{href}</d:href><d:propstat><d:prop><d:getetag>{etag}</d:getetag>"
                            f"<c:calendar-data>{ics}</c:calendar-data></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")
            return ms(*rows)
        if m == "GET":
            if p in self.store:
                return httpx.Response(200, text=self.store[p][0], headers={"etag": self.store[p][1]})
            return httpx.Response(404)
        if m == "PUT":
            if req.headers.get("if-none-match") == "*" and p in self.store:
                return httpx.Response(412)
            if "if-match" in req.headers and (p not in self.store or self.store[p][1] != req.headers["if-match"]):
                return httpx.Response(412)
            self.n += 1
            self.store[p] = (req.content.decode(), f'"v{self.n}"')
            return httpx.Response(201)
        if m == "DELETE":
            if p not in self.store:
                return httpx.Response(404)
            if "if-match" in req.headers and self.store[p][1] != req.headers["if-match"]:
                return httpx.Response(412)
            del self.store[p]
            return httpx.Response(204)
        return httpx.Response(405)


def caldav(dav: FakeDav | None = None, url="https://dav.example.org/", password=PASSWORD):
    dav = dav or FakeDav()
    seen_hosts = []

    def factory(host, port):
        seen_hosts.append(host)
        return None, httpx.Client(transport=httpx.MockTransport(dav))

    c = CalendarConnector({"mode": "caldav", "url": url, "username": "jean@example.org"}, password,
                          client_factory=factory, now=lambda: NOW)
    return c, dav, seen_hosts


def test_caldav_exposes_four_tools_with_default_effects():
    c, _, _ = caldav()
    specs = {s.name: s for s in c.specs()}
    assert set(specs) == {"calendar_events", "calendar_create", "calendar_update", "calendar_delete"}
    for name, s in specs.items():
        assert s.effect == DEFAULT_EFFECTS[name] and s.source == "connector:calendar" and not s.core
        assert "user" not in s.parameters["properties"]
    assert specs["calendar_events"].effect is Effect.READ and specs["calendar_delete"].effect is Effect.DESTRUCTIVE
    assert {s.name: s.effect for s in ical(lambda r: httpx.Response(200))[0].specs()} == {"calendar_events": DEFAULT_EFFECTS["calendar_events"]}


def test_caldav_discovery_lists_only_event_calendars_and_sends_basic_auth():
    c, dav, _ = caldav()
    assert [x.name for x in c._discover()] == ["Travail", "Maison"]
    assert [(m, p) for m, p, _, _ in dav.log] == [("PROPFIND", "/"), ("PROPFIND", "/principals/u/"), ("PROPFIND", "/cal/u/")]
    assert dav.log[0][2]["authorization"].startswith("Basic ") and dav.log[0][2]["depth"] == "0" and dav.log[2][2]["depth"] == "1"


def test_caldav_well_known_fallback_and_direct_calendar_url():
    dav = FakeDav()
    orig = dav.__call__

    def routes(req):
        if req.url.path == "/" and req.method == "PROPFIND":
            return httpx.Response(404)
        return orig(req)

    c, _, _ = caldav(dav)
    c._factory = lambda host, port: (None, httpx.Client(transport=httpx.MockTransport(routes)))
    assert [x.name for x in c._discover()][0] == "Travail"
    direct, _, _ = caldav(url="https://dav.example.org/cal/u/work/")
    direct._factory = lambda host, port: (None, httpx.Client(transport=httpx.MockTransport(
        lambda req: ms(resp("/cal/u/work/", cal_props("Travail"))))))
    assert [x.name for x in direct._discover()] == ["Travail"]


def test_caldav_events_via_report_and_calendar_selection():
    c, dav, _ = caldav()
    r, data = run(c, "calendar_events", days=3)
    assert not r.is_error and data["events"][0]["summary"] == "Réunion" and data["events"][0]["id"] == "ext-1"
    report = next(x for x in dav.log if x[0] == "REPORT")
    assert report[1] == "/cal/u/work/" and b"time-range" in report[3] and b"20261021T080000Z" in report[3]
    r, data = run(c, "calendar_events", calendar="Maison")
    assert data["count"] == 0
    r, data = run(c, "calendar_events", calendar="all")
    assert data["count"] == 1
    r, msg = run(c, "calendar_events", calendar="Inconnu")
    assert r.is_error and "Travail" in msg and PASSWORD not in msg


def test_caldav_create_puts_a_clean_ics_without_invitation_semantics():
    c, dav, _ = caldav()
    r, data = run(c, "calendar_create", summary="Dîner; chez Léa, 19h\r\nATTENDEE:mailto:evil@x.org", start="2026-10-27T19:00",
                  end="2026-10-27T22:00", location="Genève", attendees=["lea@example.org"])
    assert not r.is_error and data["created"] and "aucune invitation" in data["note"]
    put = next(x for x in dav.log if x[0] == "PUT")
    assert put[1] == f"/cal/u/work/{data['id']}.ics" and put[2]["if-none-match"] == "*"
    body = put[3].decode()
    assert f"UID:{data['id']}" in body and "DTSTART:20261027T180000Z" in body and "DTEND:20261027T210000Z" in body
    lines = body.split("\r\n")
    assert "ORGANIZER" not in body and "METHOD" not in body
    assert [ln for ln in lines if ln.startswith("ATTENDEE")] == ["ATTENDEE:mailto:lea@example.org"]
    assert lines.count("BEGIN:VEVENT") == 1
    r, data2 = run(c, "calendar_events", days=30)
    assert any(e["id"] == data["id"] and e["summary"].startswith("Dîner; chez Léa, 19h") for e in data2["events"])


def test_caldav_create_validation():
    c, dav, _ = caldav()
    for args in ({"summary": "", "start": "2026-10-27T10:00", "end": "2026-10-27T11:00"},
                 {"summary": "x", "start": "2026-10-27T12:00", "end": "2026-10-27T11:00"},
                 {"summary": "x", "start": "2026-10-27", "end": "2026-10-27T11:00"},
                 {"summary": "x", "start": "bientôt", "end": "2026-10-27T11:00"},
                 {"summary": "x", "start": "2026-10-27T10:00", "end": "2026-10-27T11:00", "attendees": ["pas une adresse"]}):
        assert c.call("calendar_create", args, CTX).is_error
    assert not any(m == "PUT" for m, *_ in dav.log)


def test_caldav_create_all_day_end_is_inclusive():
    c, dav, _ = caldav()
    r, data = run(c, "calendar_create", summary="Congé", start="2026-10-27", end="2026-10-28")
    body = next(x for x in dav.log if x[0] == "PUT")[3].decode()
    assert "DTSTART;VALUE=DATE:20261027" in body and "DTEND;VALUE=DATE:20261029" in body


def test_caldav_update_by_uid_uses_if_match_and_keeps_other_properties():
    c, dav, _ = caldav()
    r, data = run(c, "calendar_update", event_id="ext-1", summary="Réunion déplacée", start="2026-10-23T10:00")
    assert not r.is_error and data["updated"]
    put = next(x for x in dav.log if x[0] == "PUT")
    assert put[1] == "/cal/u/work/abc.ics" and put[2]["if-match"] == '"e1"'  # trouvé par REPORT (nom de fichier ≠ UID)
    body = put[3].decode()
    assert "SUMMARY:Réunion déplacée" in body and "X-CUSTOM:garde" in body and "SEQUENCE:1" in body
    assert "DTSTART:20261023T080000Z" in body and "DTEND:20261023T090000Z" in body  # durée conservée


def test_caldav_update_errors():
    c, dav, _ = caldav()
    r, msg = run(c, "calendar_update", event_id="nope", summary="x")
    assert r.is_error and "introuvable" in msg
    r, msg = run(c, "calendar_update", event_id="ext-1")
    assert r.is_error and "rien à modifier" in msg
    dav.store["/cal/u/work/abc.ics"] = (dav.store["/cal/u/work/abc.ics"][0], '"e2"')
    c2, _, _ = caldav(dav)
    orig = dav.__call__

    def racing(req):  # l'événement change entre la lecture et l'écriture
        if req.method == "PUT":
            dav.store["/cal/u/work/abc.ics"] = (dav.store["/cal/u/work/abc.ics"][0], '"e3"')
        return orig(req)

    c2._factory = lambda host, port: (None, httpx.Client(transport=httpx.MockTransport(racing)))
    r, msg = run(c2, "calendar_update", event_id="ext-1", summary="x")
    assert r.is_error and "modifié entre-temps" in msg


def test_caldav_update_recurring_time_change_refused_but_text_change_allowed():
    dav = FakeDav()
    dav.store["/cal/u/work/r.ics"] = (vcal(vevent("UID:rec", "SUMMARY:Hebdo", "DTSTART:20261022T080000Z", "DTEND:20261022T090000Z",
                                                  "RRULE:FREQ=WEEKLY")), '"r1"')
    c, _, _ = caldav(dav)
    r, msg = run(c, "calendar_update", event_id="rec", start="2026-10-23T10:00")
    assert r.is_error and "récurrente" in msg
    r, data = run(c, "calendar_update", event_id="rec", location="Salle 3")
    assert not r.is_error and "toute la série" in data["note"]


def test_caldav_update_attendees_replaced_without_invitation():
    c, dav, _ = caldav()
    r, data = run(c, "calendar_update", event_id="ext-1", attendees=["a@example.org", "b@example.org"])
    body = next(x for x in dav.log if x[0] == "PUT")[3].decode()
    assert body.count("ATTENDEE:mailto:") == 2 and "aucune invitation" in data["note"]


def test_caldav_delete_by_uid_with_if_match():
    c, dav, _ = caldav()
    r, data = run(c, "calendar_delete", event_id="ext-1")
    assert not r.is_error and data["deleted"] and dav.store == {}
    d = next(x for x in dav.log if x[0] == "DELETE")
    assert d[2]["if-match"] == '"e1"'
    r, msg = run(c, "calendar_delete", event_id="ext-1")
    assert r.is_error and "introuvable" in msg


def test_caldav_finds_events_created_here_by_filename_without_report():
    c, dav, _ = caldav()
    _, created = run(c, "calendar_create", summary="x", start="2026-10-27T10:00", end="2026-10-27T11:00")
    dav.log.clear()
    run(c, "calendar_delete", event_id=created["id"])
    assert [m for m, *_ in dav.log] == ["GET", "DELETE"]


def test_caldav_wrong_password_is_auth_failed_without_leaking_it():
    dav = FakeDav()
    dav.require_auth = False
    dav.__class__ = type("Deny", (FakeDav,), {"__call__": lambda self, req: httpx.Response(401, text=f"bad {PASSWORD}")})
    c, _, _ = caldav(dav)
    r, msg = run(c, "calendar_events")
    assert r.is_error and msg.startswith("auth_failed") and PASSWORD not in msg
    with pytest.raises(ConnectorError) as e:
        c.test()
    assert e.value.code == "auth_failed" and PASSWORD not in e.value.message


def test_caldav_non_dav_server_is_protocol_error():
    c, _, _ = caldav()
    c._factory = lambda host, port: (None, httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text="<html/>"))))
    r, msg = run(c, "calendar_events")
    assert r.is_error and msg.startswith("protocol_error")


def test_xml_entities_are_refused():
    c, _, _ = caldav()
    evil = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><d:multistatus xmlns:d="DAV:">&a;</d:multistatus>'
    c._factory = lambda host, port: (None, httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(207, text=evil))))
    r, msg = run(c, "calendar_events")
    assert r.is_error and msg.startswith("protocol_error")


def test_caldav_test_message():
    c, _, _ = caldav()
    msg = c.test()
    assert "2 agenda(s)" in msg and "1 événement(s)" in msg and PASSWORD not in msg


def test_authorization_only_goes_to_origin_or_same_domain_hosts():
    dav = FakeDav()
    auth_by_host: dict[str, str | None] = {}

    def factory(host, port):
        def handler(req):
            auth_by_host[host] = req.headers.get("authorization")
            if host == "dav.example.org":
                return httpx.Response(302, headers={"location": "https://evil.example.net/x"}) if req.url.path == "/" else dav(req)
            return httpx.Response(401)
        return None, httpx.Client(transport=httpx.MockTransport(handler))

    c = CalendarConnector({"mode": "caldav", "url": "https://dav.example.org/", "username": "u"}, PASSWORD, client_factory=factory,
                          now=lambda: NOW)
    run(c, "calendar_events")
    assert auth_by_host["dav.example.org"].startswith("Basic ") and auth_by_host["evil.example.net"] is None
    # un frère du même domaine enregistrable (shard iCloud) reçoit l'authentification
    assert c._auth_ok("p12-caldav.example.org") and not c._auth_ok("example.net") and not c._auth_ok("example.org.evil.io")
    co = CalendarConnector({"mode": "caldav", "url": "https://dav.firma.co.uk/", "username": "u"}, PASSWORD)
    assert co._auth_ok("x.firma.co.uk") and not co._auth_ok("evil.co.uk")


def test_caldav_requires_https_username_and_valid_tz():
    for settings in ({"mode": "caldav", "url": "http://dav.example.org/", "username": "u"}, {"mode": "caldav", "url": "https://x.org/"},
                     {"mode": "ical", "tz": "Mars/Olympus"}, {"mode": "autre"}):
        with pytest.raises(ConnectorError) as e:
            CalendarConnector(settings, "secret-value")
        assert e.value.code == "bad_settings"
    with pytest.raises(ConnectorError):
        CalendarConnector({"mode": "ical"}, "")


def test_unknown_tool_and_unexpected_exception_do_not_escape():
    c, _, _ = caldav()
    assert c.call("calendar_nope", {}, CTX).is_error
    c._factory = lambda host, port: (_ for _ in ()).throw(RuntimeError(f"explose avec {PASSWORD}"))
    r = c.call("calendar_events", {}, CTX)
    assert r.is_error and PASSWORD not in r.content
    assert datetime.now(UTC)  # garde-fou d'import
