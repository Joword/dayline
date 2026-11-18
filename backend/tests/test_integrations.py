"""Integration layer: crypto, configs, HTTP routing, git, calendar backends and preprocessing."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from pydantic import ValidationError

from app.core.crypto import Crypto, CryptoError, generate_key
from app.integrations.calendar import (
    CalDAVBackend,
    CalendarSource,
    Event,
    GraphBackend,
    parse_ics,
    resolve_invites,
    summarize_calendar,
)
from app.integrations.git import Commit, GitHubSource, GitLabSource, PullRequest, summarize_git
from app.integrations.http import TargetClient
from app.integrations.models import (
    CalendarConfig,
    GitConfig,
    IntegrationAuthError,
    IntegrationUnreachable,
    RateLimited,
    reveal,
)

TZ = ZoneInfo("Asia/Shanghai")
START = datetime(2026, 11, 17, tzinfo=TZ)
END = START + timedelta(days=1)


def client(handler, **kw) -> TargetClient:
    t = httpx.MockTransport(handler)
    return TargetClient("none", transport_factory=lambda route: t, **kw)


# --- crypto / configs -----------------------------------------------------------------


def test_crypto_roundtrip_and_rotation():
    old, new = generate_key(), generate_key()
    token = Crypto(old).encrypt({"token": "s3cret"})
    assert "s3cret" not in token
    rotated = Crypto(f"{new},{old}")
    assert rotated.decrypt(token) == {"token": "s3cret"}  # old key still decrypts
    re_enc = rotated.rotate(token)
    assert Crypto(new).decrypt(re_enc) == {"token": "s3cret"}
    with pytest.raises(CryptoError):
        Crypto(new).decrypt(token)
    with pytest.raises(CryptoError, match="not set"):
        Crypto(None).encrypt({})
    with pytest.raises(CryptoError, match="not a valid"):
        Crypto("nope")


def test_config_validation_and_secret_handling():
    with pytest.raises(ValidationError, match="username"):
        GitConfig(provider="github", token="t")
    with pytest.raises(ValidationError, match="client-specific password"):
        CalendarConfig(provider="coremail", mode="caldav", caldav_url="https://dav.corp")
    with pytest.raises(ValidationError, match="supports modes"):
        CalendarConfig(provider="outlook", mode="caldav", caldav_url="x")
    cfg = GitConfig(provider="gitlab", base_url="https://gitlab.corp", token="glpat-x")
    assert "glpat-x" not in cfg.model_dump_json() and reveal(cfg)["token"] == "glpat-x"
    assert cfg.label() == "gitlab:gitlab.corp"


# --- HTTP routing -----------------------------------------------------------------------


async def test_auto_proxy_falls_back_and_remembers():
    calls = []

    def direct(request):
        calls.append("direct")
        raise httpx.ConnectError("blocked", request=request)

    def proxied(request):
        calls.append("proxy")
        return httpx.Response(200, json={"ok": True})

    tc = TargetClient(
        "auto",
        fallback_proxy="http://proxy.corp:8080",
        transport_factory=lambda route: httpx.MockTransport(proxied if route else direct),
    )
    assert (await tc.json("GET", "https://gitlab.corp/api")) == {"ok": True}
    await tc.json("GET", "https://gitlab.corp/api")
    assert calls == ["direct", "proxy", "proxy"] and tc.route_used == "proxy"
    await tc.aclose()

    only_direct = TargetClient("none", transport_factory=lambda route: httpx.MockTransport(direct))
    with pytest.raises(IntegrationUnreachable):
        await only_direct.request("GET", "https://gitlab.corp/api")


@pytest.mark.parametrize(
    ("status", "headers", "exc"),
    [
        (401, {}, IntegrationAuthError),
        (403, {"x-ratelimit-remaining": "0"}, RateLimited),
        (429, {}, RateLimited),
    ],
)
async def test_status_mapping(status, headers, exc):
    tc = client(lambda r: httpx.Response(status, headers=headers))
    with pytest.raises(exc):
        await tc.json("GET", "https://api.github.com/x")


# --- git --------------------------------------------------------------------------------


async def test_github_source_queries_and_parsing():
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        if request.url.path == "/search/commits":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "sha": "a1",
                            "html_url": "https://github.com/o/r/commit/a1",
                            "parents": [{}],
                            "repository": {"full_name": "o/r"},
                            "commit": {
                                "message": "fix: retry on timeout (#12)\n\nbody",
                                "author": {"date": "2026-11-17T03:00:00Z", "email": "me@x"},
                            },
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "number": 12,
                        "title": "Retry on timeout",
                        "html_url": "https://github.com/o/r/pull/12",
                        "state": "closed",
                        "repository_url": "https://api.github.com/repos/o/r",
                        "pull_request": {"merged_at": "2026-11-17T05:00:00Z"},
                    }
                ]
            },
        )

    src = GitHubSource(GitConfig(provider="github", username="me", token="ghp_x"), client(handler))
    commits, prs = await src.fetch(START, END)
    q = seen[0].url.params["q"]
    assert "author:me" in q and "author-date:2026-11-17T00:00:00+08:00..2026-11-18T00:00:00+08:00" in q
    assert seen[0].headers["authorization"] == "Bearer ghp_x"
    assert commits[0].title == "fix: retry on timeout (#12)" and commits[0].repo == "o/r"
    assert prs[0].state == "merged" and prs[0].repo == "o/r"


async def test_gitlab_source_filters_commits_by_author_email():
    def handler(request: httpx.Request):
        p = request.url.path
        assert request.headers["private-token"] == "glpat-x"
        if p == "/api/v4/user":
            return httpx.Response(200, json={"id": 7, "name": "张三", "commit_email": "zs@corp.com"})
        if p == "/api/v4/users/7/events":
            return httpx.Response(200, json=[{"project_id": 3}, {"project_id": 3}])
        if p == "/api/v4/projects/3":
            return httpx.Response(200, json={"path_with_namespace": "pay/svc"})
        if p == "/api/v4/projects/3/repository/commits":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "c1",
                        "title": "feat: x",
                        "message": "feat: x\n\nSee merge request pay/svc!5",
                        "web_url": "u1",
                        "authored_date": "2026-11-17T10:00:00+08:00",
                        "parent_ids": ["p"],
                        "author_email": "ZS@corp.com",
                    },
                    {
                        "id": "c2",
                        "title": "someone else",
                        "message": "",
                        "web_url": "u2",
                        "authored_date": "2026-11-17T11:00:00+08:00",
                        "parent_ids": ["p"],
                        "author_email": "other@corp.com",
                    },
                ],
            )
        if p == "/api/v4/merge_requests":
            return httpx.Response(
                200,
                json=[
                    {
                        "iid": 5,
                        "title": "Feature X",
                        "web_url": "mr5",
                        "state": "merged",
                        "references": {"full": "pay/svc!5"},
                    }
                ],
            )
        return httpx.Response(404)

    src = GitLabSource(
        GitConfig(provider="gitlab", base_url="https://gitlab.corp", token="glpat-x"), client(handler)
    )
    commits, prs = await src.fetch(START, END)
    assert [c.sha for c in commits] == ["c1"]
    assert prs[0].repo == "pay/svc" and prs[0].number == 5
    summary = summarize_git(commits, prs, provider="gitlab", tz=TZ)
    assert summary["repos"][0]["prs"][0]["commits"] == ["feat: x"]


def test_git_preprocessing_drops_merges_and_formatting_and_groups_by_pr():
    t = datetime(2026, 11, 17, 3, tzinfo=UTC)
    commits = [
        Commit("1", "o/r", "fix: retry on timeout (#12)", "u1", t),
        Commit("2", "o/r", "test: cover timeout (#12)", "u2", t + timedelta(minutes=5)),
        Commit("3", "o/r", "Merge branch 'main' into feat", "u3", t, parents=2),
        Commit("4", "o/r", "style: run prettier", "u4", t),
        Commit("5", "o/r", "chore: 代码格式化", "u5", t),
        Commit("6", "o/docs", "docs: add runbook", "u6", t),
        Commit("6", "o/docs", "docs: add runbook", "u6", t),  # duplicate sha (two accounts)
    ]
    prs = [
        PullRequest("o/r", 12, "Retry on timeout", "pr12", "merged"),
        PullRequest("o/r", 13, "Review only", "pr13", "open"),
    ]
    s = summarize_git(commits, prs, provider="github", tz=TZ)
    assert s["dropped"] == {"merge": 1, "format": 2}
    assert s["totals"] == {"commits": 3, "repos": 2, "prs": 2}
    r = s["repos"][0]
    assert r["repo"] == "o/r" and r["commit_count"] == 2
    pr12 = next(p for p in r["prs"] if p["number"] == 12)
    assert (
        pr12["commits"] == ["fix: retry on timeout (#12)", "test: cover timeout (#12)"]
        and pr12["state"] == "merged"
    )
    assert s["repos"][1]["other_commits"][0] == {"title": "docs: add runbook", "url": "u6", "time": "11:00"}


# --- calendar ---------------------------------------------------------------------------

ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:standup@x
SUMMARY:支付组站会
DTSTART;TZID=Asia/Shanghai:20261110T100000
DTEND;TZID=Asia/Shanghai:20261110T101500
RRULE:FREQ=DAILY
ATTENDEE;PARTSTAT=ACCEPTED:mailto:me@x.com
ATTENDEE:mailto:a@x.com
END:VEVENT
BEGIN:VEVENT
UID:review@x
SUMMARY:对账方案评审
DTSTART:20261117T060000Z
DTEND:20261117T080000Z
ORGANIZER:mailto:me@x.com
END:VEVENT
BEGIN:VEVENT
UID:lunch@x
SUMMARY:午休
DTSTART;TZID=Asia/Shanghai:20261117T120000
DTEND;TZID=Asia/Shanghai:20261117T130000
END:VEVENT
BEGIN:VEVENT
UID:declined@x
SUMMARY:季度规划
DTSTART:20261117T020000Z
DTEND:20261117T030000Z
ATTENDEE;PARTSTAT=DECLINED:mailto:me@x.com
END:VEVENT
BEGIN:VEVENT
UID:free@x
SUMMARY:可选分享会
TRANSP:TRANSPARENT
DTSTART:20261117T090000Z
DTEND:20261117T100000Z
END:VEVENT
BEGIN:VEVENT
UID:allday@x
SUMMARY:公司团建
DTSTART;VALUE=DATE:20261117
DTEND;VALUE=DATE:20261118
END:VEVENT
BEGIN:VEVENT
UID:cancel@x
SUMMARY:取消的会
STATUS:CANCELLED
DTSTART:20261117T070000Z
DTEND:20261117T073000Z
END:VEVENT
END:VCALENDAR
"""


def test_ics_parsing_expands_recurrence_and_preprocessing_filters():
    events = parse_ics(ICS, START, END, tz=TZ, me="me@x.com")
    by_uid = {e.uid: e for e in events}
    assert by_uid["standup@x"].start == datetime(2026, 11, 17, 10, tzinfo=TZ)  # RRULE instance
    assert by_uid["review@x"].organizer_is_me and by_uid["declined@x"].declined
    s = summarize_calendar(events, START, END, tz=TZ)
    assert [(e["title"], e["start"], e["end"], e["minutes"]) for e in s["events"]] == [
        ("支付组站会", "10:00", "10:15", 15),
        ("对账方案评审", "14:00", "16:00", 120),
    ]
    assert s["total_minutes"] == 135
    assert s["dropped"] == {"placeholder": 1, "declined": 1, "free": 1, "all_day": 1, "cancelled": 1}


def test_cross_calendar_dedup_by_uid_and_by_time_and_title():
    t = datetime(2026, 11, 17, 6, tzinfo=UTC)
    events = [
        Event("u1", "对账方案评审", t, t + timedelta(hours=2), source="graph", attendees=6),
        Event("u1", "对账方案评审", t, t + timedelta(hours=2), source="ics"),
        Event("", "对账方案评审会", t + timedelta(minutes=2), t + timedelta(hours=2), source="caldav"),
        Event("u2", "1:1", t + timedelta(hours=3), t + timedelta(hours=3, minutes=30), source="caldav"),
    ]
    s = summarize_calendar(events, START, END, tz=TZ)
    assert s["count"] == 2 and s["dropped"] == {"duplicate": 2}
    assert s["events"][0]["attendees"] == 6


def test_invite_updates_and_cancellations():
    t = datetime(2026, 11, 17, 6, tzinfo=UTC)
    events = [
        Event("m1", "评审", t, t + timedelta(hours=1), sequence=0),
        Event("m1", "评审(改时间)", t, t + timedelta(hours=2), sequence=1),
        Event("m2", "周会", t, t + timedelta(hours=1), sequence=0),
        Event("m2", "周会", t, t + timedelta(hours=1), sequence=1, cancelled=True),
    ]
    out = resolve_invites(events)
    assert [(e.uid, e.title) for e in out] == [("m1", "评审(改时间)")]


async def test_caldav_report_and_discovery():
    vevent = ICS.replace("\n", "\r\n")
    multistatus = f"""<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
<d:response><d:href>/cal/me/default/1.ics</d:href><d:propstat><d:prop><c:calendar-data>{vevent}</c:calendar-data></d:prop></d:propstat></d:response>
</d:multistatus>"""
    seen = []

    def handler(request: httpx.Request):
        seen.append((request.method, request.url.path))
        assert request.headers["authorization"].startswith("Basic ")
        if request.method == "REPORT" and request.url.path == "/dav/":
            return httpx.Response(403)  # the root is not a calendar
        if request.method == "PROPFIND" and request.url.path == "/dav/":
            return httpx.Response(
                207,
                text='<d:multistatus xmlns:d="DAV:"><d:response><d:propstat><d:prop><d:current-user-principal><d:href>/principals/me/</d:href></d:current-user-principal></d:prop></d:propstat></d:response></d:multistatus>',
            )
        if request.method == "PROPFIND" and request.url.path == "/principals/me/":
            return httpx.Response(
                207,
                text='<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:response><d:propstat><d:prop><c:calendar-home-set><d:href>/cal/me/</d:href></c:calendar-home-set></d:prop></d:propstat></d:response></d:multistatus>',
            )
        if request.method == "PROPFIND" and request.url.path == "/cal/me/":
            return httpx.Response(
                207,
                text='<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:response><d:href>/cal/me/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat></d:response><d:response><d:href>/cal/me/default/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/><c:calendar/></d:resourcetype></d:prop></d:propstat></d:response></d:multistatus>',
            )
        if request.method == "REPORT" and request.url.path == "/cal/me/default/":
            assert b'time-range start="20261116T160000Z"' in request.content
            return httpx.Response(207, text=multistatus)
        return httpx.Response(404)

    cfg = CalendarConfig(
        provider="coremail",
        mode="caldav",
        caldav_url="https://mail.corp/dav/",
        username="me@x.com",
        password="client-pw",
    )
    events = await CalDAVBackend(cfg, client(handler), TZ).fetch(START, END)
    assert {"对账方案评审", "支付组站会"} <= {e.title for e in events}
    assert seen[-1] == ("REPORT", "/cal/me/default/")


async def test_graph_refreshes_token_pages_and_maps_fields():
    def handler(request: httpx.Request):
        if request.url.host == "login.microsoftonline.com":
            form = dict(x.split("=") for x in request.content.decode().split("&"))
            assert form["grant_type"] == "refresh_token" and form["refresh_token"] == "rt-old"
            return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt-new"})
        assert request.headers["authorization"] == "Bearer at"
        if "skiptoken" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "subject": "1:1",
                            "start": {"dateTime": "2026-11-17T08:00:00.0000000"},
                            "end": {"dateTime": "2026-11-17T08:30:00.0000000"},
                            "showAs": "busy",
                            "responseStatus": {"response": "accepted"},
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "iCalUId": "g1",
                        "subject": "评审",
                        "start": {"dateTime": "2026-11-17T06:00:00.0000000"},
                        "end": {"dateTime": "2026-11-17T08:00:00.0000000"},
                        "isAllDay": False,
                        "showAs": "busy",
                        "isCancelled": False,
                        "isOrganizer": True,
                        "attendees": [{}, {}],
                        "responseStatus": {"response": "organizer"},
                    }
                ],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/calendarView?$skiptoken=abc",
            },
        )

    cfg = CalendarConfig(
        provider="outlook", mode="graph", graph={"tenant": "t", "client_id": "c", "refresh_token": "rt-old"}
    )
    b = GraphBackend(cfg, client(handler), TZ)
    events = await b.fetch(START, END)
    assert [e.title for e in events] == ["评审", "1:1"]
    assert events[0].start == datetime(2026, 11, 17, 6, tzinfo=UTC) and events[0].attendees == 2
    assert b.rotated_refresh_token == "rt-new"


async def test_calendar_auto_mode_order_and_memory():
    def handler(request: httpx.Request):
        return httpx.Response(200, text=ICS)

    cfg = CalendarConfig(
        provider="outlook", ics_url="https://outlook.office365.com/owa/calendar/x/calendar.ics"
    )
    src = CalendarSource(cfg, client(handler), TZ)  # graph not signed in -> falls through to ics
    assert len(await src.fetch(START, END)) >= 2 and src.mode_used == "ics"
    cm = CalendarConfig(
        provider="coremail",
        ics_url="https://mail.corp/x.ics",
        imap_host="imap.corp",
        username="u",
        password="p",
    )
    assert CalendarSource(cm, client(handler), TZ, remembered="imap")._order() == [
        "imap",
        "caldav",
        "api",
        "ics",
    ]


async def test_imap_reads_only_calendar_messages(monkeypatch):
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    from app.integrations import calendar as cal

    invite = MIMEMultipart()
    invite.attach(MIMEText("见附件", "plain", "utf-8"))
    part = MIMEText(ICS.replace("BEGIN:VCALENDAR", "BEGIN:VCALENDAR\nMETHOD:REQUEST", 1), "calendar", "utf-8")
    invite.attach(part)
    fetched = []

    class FakeIMAP:
        def __init__(self, host, port, ssl_context=None, timeout=None):
            assert host == "imap.corp"

        def login(self, user, pw):
            assert pw == "client-pw"

        def select(self, box, readonly=False):
            assert readonly  # never marks anything as read

        def search(self, charset, *criteria):
            return "OK", [b"1 2"]

        def fetch(self, i, what):
            fetched.append((i, what))
            if what == "(BODYSTRUCTURE)":
                structure = (
                    b'1 (BODYSTRUCTURE (("TEXT" "PLAIN") ("TEXT" "CALENDAR" ("METHOD" "REQUEST"))))'
                    if i == b"1"
                    else b'2 (BODYSTRUCTURE ("TEXT" "PLAIN"))'
                )
                return "OK", [structure]
            return "OK", [(b"1 (BODY[] {1}", invite.as_bytes()), b")"]

        def logout(self):
            pass

    monkeypatch.setattr(cal.imaplib, "IMAP4_SSL", FakeIMAP)
    cfg = CalendarConfig(
        provider="coremail", mode="imap", imap_host="imap.corp", username="me@x.com", password="client-pw"
    )
    events = await cal.IMAPInviteBackend(cfg, None, TZ).fetch(START, END)  # type: ignore[arg-type]
    assert "对账方案评审" in {e.title for e in events}
    assert (b"2", "(BODY.PEEK[])") not in fetched  # the plain message was never downloaded
    assert (b"1", "(BODY.PEEK[])") in fetched


def test_secrets_never_in_labels():
    cfg = CalendarConfig(
        provider="coremail", mode="caldav", caldav_url="https://mail.corp/dav/", username="u", password="pw"
    )
    assert "pw" not in cfg.label() and json.dumps(cfg.model_dump(mode="json")).count("pw") == 0
