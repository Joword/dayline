"""Calendar activity: Outlook / Coremail via reusable backends (plan §9.2).

  OutlookSource   Graph (Microsoft 365) -> ICS                      mode: auto | graph | ics
  CoremailSource  CalDAV -> Coremail open API -> ICS -> IMAP invites  mode: auto | caldav | api | ics | imap

``auto`` tries the backends that have credentials in that order and remembers the one
that worked (``integration.state.mode_used``). IMAP is the last resort: it only sees
invitations received by mail, reads only messages with a calendar part, never stores
mail bodies, and opens the mailbox read-only.

Preprocessing drops lunch / placeholder / free / declined / cancelled / all-day events
and merges duplicates across calendars: same UID first, otherwise start time within 5
minutes and similar titles.
"""

from __future__ import annotations

import asyncio
import email
import imaplib
import re
import ssl
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any
from urllib.parse import urljoin

import icalendar
import recurring_ical_events

from app.integrations.http import TargetClient, check_status
from app.integrations.models import CalendarConfig, IntegrationError, IntegrationNotConfigured
from app.rules.trends import similarity


@dataclass
class Event:
    uid: str
    title: str
    start: datetime
    end: datetime
    all_day: bool = False
    cancelled: bool = False
    free: bool = False  # transparent / "show as free"
    declined: bool = False
    attendees: int = 0
    organizer_is_me: bool = False
    sequence: int = 0
    source: str = ""


# --- ICS parsing (shared by ICS, CalDAV and IMAP) -----------------------------------------


def _as_dt(v: Any, tz: tzinfo) -> tuple[datetime, bool]:
    if isinstance(v, datetime):
        return (v if v.tzinfo else v.replace(tzinfo=tz)), False
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day, tzinfo=tz), True
    raise ValueError(f"unsupported date value {v!r}")


def _addr(v: Any) -> str:
    return str(v).lower().removeprefix("mailto:")


def parse_ics(
    text: str, start: datetime, end: datetime, *, tz: tzinfo, me: str | None = None, source: str = "ics"
) -> list[Event]:
    try:
        cal = icalendar.Calendar.from_ical(text)
    except ValueError as e:
        raise IntegrationError("calendar data could not be parsed") from e
    method = str(cal.get("METHOD", "")).upper()
    me = (me or "").lower()
    out = []
    for ev in recurring_ical_events.of(cal).between(start, end):
        if "DTSTART" not in ev:
            continue
        s, all_day = _as_dt(ev["DTSTART"].dt, tz)
        if "DTEND" in ev:
            e, _ = _as_dt(ev["DTEND"].dt, tz)
        elif "DURATION" in ev:
            e = s + ev["DURATION"].dt
        else:
            e = s + (timedelta(days=1) if all_day else timedelta(0))
        attendees = ev.get("ATTENDEE") or []
        if not isinstance(attendees, list):
            attendees = [attendees]
        my_status = next(
            (str(a.params.get("PARTSTAT", "")).upper() for a in attendees if me and _addr(a) == me), ""
        )
        busy = str(ev.get("X-MICROSOFT-CDO-BUSYSTATUS", "")).upper()
        out.append(
            Event(
                uid=str(ev.get("UID", "")),
                title=str(ev.get("SUMMARY", "")),
                start=s,
                end=e,
                all_day=all_day,
                cancelled=str(ev.get("STATUS", "")).upper() == "CANCELLED" or method == "CANCEL",
                free=str(ev.get("TRANSP", "")).upper() == "TRANSPARENT" or busy == "FREE",
                declined=my_status == "DECLINED",
                attendees=len(attendees),
                organizer_is_me=bool(me) and "ORGANIZER" in ev and _addr(ev["ORGANIZER"]) == me,
                sequence=int(ev.get("SEQUENCE", 0) or 0),
                source=source,
            )
        )
    return out


# --- backends ---------------------------------------------------------------------------


class ICSBackend:
    name = "ics"

    def __init__(self, cfg: CalendarConfig, http: TargetClient, tz: tzinfo):
        if not cfg.ics_url:
            raise IntegrationNotConfigured("no ics_url")
        self.url, self.http, self.tz, self.me = cfg.ics_url.get_secret_value(), http, tz, cfg.email

    async def fetch(self, start: datetime, end: datetime) -> list[Event]:
        resp = await self.http.request("GET", self.url)
        check_status(resp)
        return parse_ics(resp.text, start, end, tz=self.tz, me=self.me, source=self.name)


DAV, CALDAV = "DAV:", "urn:ietf:params:xml:ns:caldav"


def _propfind(inner: str) -> str:
    return f'<?xml version="1.0"?><d:propfind xmlns:d="{DAV}" xmlns:c="{CALDAV}"><d:prop>{inner}</d:prop></d:propfind>'


class CalDAVBackend:
    name = "caldav"

    def __init__(self, cfg: CalendarConfig, http: TargetClient, tz: tzinfo):
        if not (cfg.caldav_url and cfg.username and cfg.password):
            raise IntegrationNotConfigured("caldav needs caldav_url, username and a client password")
        self.url, self.http, self.tz, self.me = cfg.caldav_url, http, tz, cfg.email or cfg.username
        self.auth = (cfg.username, cfg.password.get_secret_value())

    async def _dav(self, method: str, url: str, body: str, depth: str) -> ET.Element:
        resp = await self.http.request(
            method,
            url,
            content=body.encode(),
            auth=self.auth,
            headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
        )
        check_status(resp)
        try:
            return ET.fromstring(resp.content)
        except ET.ParseError as e:
            raise IntegrationError("CalDAV server returned invalid XML") from e

    async def _report(self, url: str, start: datetime, end: datetime) -> list[Event]:
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<c:calendar-query xmlns:d="{DAV}" xmlns:c="{CALDAV}"><d:prop><c:calendar-data/></d:prop>'
            '<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">'
            f'<c:time-range start="{start.astimezone(UTC):%Y%m%dT%H%M%SZ}" end="{end.astimezone(UTC):%Y%m%dT%H%M%SZ}"/>'
            "</c:comp-filter></c:comp-filter></c:filter></c:calendar-query>"
        )
        root = await self._dav("REPORT", url, body, "1")
        events: list[Event] = []
        for node in root.iter(f"{{{CALDAV}}}calendar-data"):
            if node.text and node.text.strip():
                events += parse_ics(node.text, start, end, tz=self.tz, me=self.me, source=self.name)
        return events

    async def _discover(self) -> list[str]:
        """RFC 6764/4791: principal -> calendar-home-set -> calendar collections."""
        root = await self._dav("PROPFIND", self.url, _propfind("<d:current-user-principal/>"), "0")
        href = root.find(f".//{{{DAV}}}current-user-principal/{{{DAV}}}href")
        principal = urljoin(self.url, href.text) if href is not None and href.text else self.url
        root = await self._dav("PROPFIND", principal, _propfind("<c:calendar-home-set/>"), "0")
        href = root.find(f".//{{{CALDAV}}}calendar-home-set/{{{DAV}}}href")
        home = urljoin(self.url, href.text) if href is not None and href.text else principal
        root = await self._dav("PROPFIND", home, _propfind("<d:resourcetype/>"), "1")
        cals = []
        for resp in root.iter(f"{{{DAV}}}response"):
            h = resp.find(f"{{{DAV}}}href")
            if (
                h is not None
                and h.text
                and resp.find(f".//{{{DAV}}}resourcetype/{{{CALDAV}}}calendar") is not None
            ):
                cals.append(urljoin(self.url, h.text))
        if not cals:
            raise IntegrationError("no CalDAV calendars found")
        return cals

    async def fetch(self, start: datetime, end: datetime) -> list[Event]:
        try:
            return await self._report(self.url, start, end)
        except IntegrationError:
            pass  # not a calendar collection: discover the calendars below this URL
        events: list[Event] = []
        for cal in await self._discover():
            events += await self._report(cal, start, end)
        return events


class IMAPInviteBackend:
    name = "imap"
    LOOKBACK_DAYS = 30  # invitations arrive before the meeting
    MAX_MESSAGES = 300

    def __init__(self, cfg: CalendarConfig, http: TargetClient, tz: tzinfo):
        if not (cfg.imap_host and cfg.username and cfg.password):
            raise IntegrationNotConfigured("imap needs imap_host, username and a client password")
        self.cfg, self.tz = cfg, tz

    async def fetch(self, start: datetime, end: datetime) -> list[Event]:
        try:
            return await asyncio.to_thread(self._fetch_sync, start, end)
        except (imaplib.IMAP4.error, OSError) as e:
            raise IntegrationError(f"IMAP: {type(e).__name__}") from e

    def _fetch_sync(self, start: datetime, end: datetime) -> list[Event]:
        cfg = self.cfg
        ctx = (
            ssl.create_default_context(cafile=cfg.ca_bundle)
            if cfg.ca_bundle
            else ssl.create_default_context()
        )
        conn = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port, ssl_context=ctx, timeout=20)  # type: ignore[arg-type]
        try:
            conn.login(cfg.username, cfg.password.get_secret_value())  # type: ignore[arg-type,union-attr]
            conn.select("INBOX", readonly=True)
            since = (start - timedelta(days=self.LOOKBACK_DAYS)).strftime("%d-%b-%Y")
            _, data = conn.search(None, "SINCE", since)
            ids = (data[0] or b"").split()[-self.MAX_MESSAGES :]
            events: list[Event] = []
            for i in ids:
                _, meta = conn.fetch(i, "(BODYSTRUCTURE)")
                structure = str(meta).lower()  # e.g. ("text" "calendar" ...) / ("application" "ics" ...)
                if '"calendar"' not in structure and '"ics"' not in structure:
                    continue  # only messages that carry a calendar part are downloaded
                _, msg = conn.fetch(i, "(BODY.PEEK[])")
                raw = next((x[1] for x in msg if isinstance(x, tuple)), b"")
                for part in email.message_from_bytes(raw).walk():
                    if part.get_content_type() in ("text/calendar", "application/ics"):
                        payload = part.get_payload(decode=True) or b""
                        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                        events += parse_ics(
                            text, start, end, tz=self.tz, me=cfg.email or cfg.username, source=self.name
                        )
            return resolve_invites(events)
        finally:
            try:
                conn.logout()
            except Exception:
                pass


def resolve_invites(events: list[Event]) -> list[Event]:
    """Apply updates and cancellations: per (uid, start) the highest SEQUENCE wins (a later
    message wins a tie); cancelled instances are dropped. A series cancellation expands to
    cancelled instances, so it overrides each one."""
    latest: dict[tuple[str, datetime], Event] = {}
    for e in events:  # message order
        key = (e.uid, e.start)
        if key not in latest or e.sequence >= latest[key].sequence:
            latest[key] = e
    return [e for e in latest.values() if not e.cancelled]


GRAPH = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "offline_access Calendars.Read"


def _login(tenant: str) -> str:
    return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"


class GraphBackend:
    name = "graph"

    def __init__(self, cfg: CalendarConfig, http: TargetClient, tz: tzinfo):
        if not cfg.graph:
            raise IntegrationNotConfigured("not signed in to Microsoft (dl integrations connect-outlook)")
        self.cfg, self.http, self.tz = cfg, http, tz
        self.rotated_refresh_token: str | None = None

    async def _token(self) -> str:
        g = self.cfg.graph
        assert g is not None
        resp = await self.http.request(
            "POST",
            f"{_login(g.tenant)}/token",
            data={
                "grant_type": "refresh_token",
                "client_id": g.client_id,
                "refresh_token": g.refresh_token.get_secret_value(),
                "scope": GRAPH_SCOPE,
            },
        )
        if resp.status_code in (400, 401):
            raise IntegrationError("Microsoft sign-in expired; run `dl integrations connect-outlook` again")
        check_status(resp)
        body = resp.json()
        if body.get("refresh_token") and body["refresh_token"] != g.refresh_token.get_secret_value():
            self.rotated_refresh_token = body["refresh_token"]
        return body["access_token"]

    async def fetch(self, start: datetime, end: datetime) -> list[Event]:
        headers = {"Authorization": f"Bearer {await self._token()}", "Prefer": 'outlook.timezone="UTC"'}
        url: str | None = f"{GRAPH}/me/calendarView"
        params: dict[str, Any] | None = {
            "startDateTime": start.astimezone(UTC).isoformat(),
            "endDateTime": end.astimezone(UTC).isoformat(),
            "$top": 100,
            "$select": "subject,start,end,isAllDay,showAs,isCancelled,responseStatus,isOrganizer,attendees,iCalUId",
        }
        events: list[Event] = []
        for _ in range(5):
            if url is None:
                break
            data = await self.http.json("GET", url, params=params, headers=headers)
            for it in data.get("value", []):
                events.append(
                    Event(
                        uid=it.get("iCalUId") or "",
                        title=it.get("subject") or "",
                        start=_graph_dt(it["start"]["dateTime"]),
                        end=_graph_dt(it["end"]["dateTime"]),
                        all_day=bool(it.get("isAllDay")),
                        cancelled=bool(it.get("isCancelled")),
                        free=it.get("showAs") in ("free", "workingElsewhere"),
                        declined=(it.get("responseStatus") or {}).get("response") == "declined",
                        attendees=len(it.get("attendees") or []),
                        organizer_is_me=bool(it.get("isOrganizer")),
                        source=self.name,
                    )
                )
            url, params = data.get("@odata.nextLink"), None
        return events


def _graph_dt(s: str) -> datetime:
    # "2026-11-17T02:00:00.0000000" (UTC because of the Prefer header): trim to microseconds
    base, _, frac = s.partition(".")
    return datetime.fromisoformat(base + (f".{frac[:6]}" if frac else "")).replace(tzinfo=UTC)


async def graph_device_code_start(http: TargetClient, tenant: str, client_id: str) -> dict[str, Any]:
    return await http.json(
        "POST", f"{_login(tenant)}/devicecode", data={"client_id": client_id, "scope": GRAPH_SCOPE}
    )


async def graph_device_code_poll(
    http: TargetClient, tenant: str, client_id: str, flow: dict[str, Any], *, sleep=asyncio.sleep
) -> str:
    """Poll until the user finished signing in; returns the refresh token."""
    interval = int(flow.get("interval", 5))
    for _ in range(int(flow.get("expires_in", 900)) // max(interval, 1)):
        resp = await http.request(
            "POST",
            f"{_login(tenant)}/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": client_id,
                "device_code": flow["device_code"],
            },
        )
        body = resp.json()
        if resp.status_code == 200:
            return body["refresh_token"]
        if body.get("error") == "slow_down":
            interval += 5
        elif body.get("error") != "authorization_pending":
            raise IntegrationError(f"Microsoft sign-in failed: {body.get('error')}")
        await sleep(interval)
    raise IntegrationError("Microsoft sign-in timed out")


class CoremailAPIBackend:
    """Coremail's open API differs per deployment; enabled once the admins confirm it (PLAN §18)."""

    name = "api"

    def __init__(self, cfg: CalendarConfig, http: TargetClient, tz: tzinfo):
        raise IntegrationNotConfigured("Coremail open API is not available in this deployment yet")

    async def fetch(self, start: datetime, end: datetime) -> list[Event]:  # pragma: no cover
        return []


BACKENDS = {
    "graph": GraphBackend,
    "ics": ICSBackend,
    "caldav": CalDAVBackend,
    "api": CoremailAPIBackend,
    "imap": IMAPInviteBackend,
}
ORDER = {"outlook": ["graph", "ics"], "coremail": ["caldav", "api", "ics", "imap"]}


class CalendarSource:
    def __init__(self, cfg: CalendarConfig, http: TargetClient, tz: tzinfo, *, remembered: str | None = None):
        self.cfg, self.http, self.tz = cfg, http, tz
        self.remembered = remembered
        self.mode_used: str | None = None
        self.rotated_refresh_token: str | None = None

    def _order(self) -> list[str]:
        if self.cfg.mode != "auto":
            return [self.cfg.mode]
        order = list(ORDER[self.cfg.provider])
        if self.remembered in order:
            order.remove(self.remembered)
            order.insert(0, self.remembered)
        return order

    async def fetch(self, start: datetime, end: datetime) -> list[Event]:
        errors = []
        for mode in self._order():
            try:
                backend = BACKENDS[mode](self.cfg, self.http, self.tz)
            except IntegrationNotConfigured as e:
                errors.append(f"{mode}: {e}")
                continue
            try:
                events = await backend.fetch(start, end)
            except IntegrationError as e:
                if self.cfg.mode != "auto":
                    raise
                errors.append(f"{mode}: {e}")
                continue
            self.mode_used = mode
            self.rotated_refresh_token = getattr(backend, "rotated_refresh_token", None)
            return events
        raise IntegrationError("; ".join(errors) or "no calendar backend configured")


# --- preprocessing --------------------------------------------------------------------

PLACEHOLDER_RE = re.compile(
    r"午休|午饭|午餐|吃饭|通勤|休息|专注|勿扰|占位|\b(lunch|focus( time)?|hold|block(ed)?|commute|break|ooo|out of office)\b",
    re.IGNORECASE,
)


def summarize_calendar(events: list[Event], start: datetime, end: datetime, *, tz: tzinfo) -> dict[str, Any]:
    dropped = {"all_day": 0, "cancelled": 0, "declined": 0, "free": 0, "placeholder": 0, "duplicate": 0}
    kept: list[Event] = []
    for e in sorted(events, key=lambda x: x.start):
        if e.end <= start or e.start >= end:
            continue
        reason = (
            "all_day"
            if e.all_day
            else "cancelled"
            if e.cancelled
            else "declined"
            if e.declined
            else "free"
            if e.free
            else "placeholder"
            if PLACEHOLDER_RE.search(e.title)
            else None
        )
        if reason:
            dropped[reason] += 1
            continue
        dup = next(
            (
                k
                for k in kept
                if (e.uid and k.uid == e.uid and k.start == e.start)
                or (abs((k.start - e.start).total_seconds()) <= 300 and similarity(k.title, e.title) >= 0.6)
            ),
            None,
        )
        if dup is not None:
            dropped["duplicate"] += 1
            if e.source not in dup.source.split("+"):
                kept[kept.index(dup)] = replace(
                    dup, source=f"{dup.source}+{e.source}", attendees=max(dup.attendees, e.attendees)
                )
            continue
        kept.append(e)
    rows = []
    for e in kept:
        s, t = max(e.start, start), min(e.end, end)
        rows.append(
            {
                "title": e.title,
                "start": s.astimezone(tz).strftime("%H:%M"),
                "end": t.astimezone(tz).strftime("%H:%M"),
                "minutes": int((t - s).total_seconds() // 60),
                "attendees": e.attendees,
                "organizer": e.organizer_is_me,
            }
        )
    return {
        "events": rows,
        "count": len(rows),
        "total_minutes": sum(r["minutes"] for r in rows),
        "dropped": {k: v for k, v in dropped.items() if v},
    }
