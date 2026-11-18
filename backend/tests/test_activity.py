"""ActivityService + Relay.activity + generate-daily auto-fill (MongoDB + mock HTTP)."""

from __future__ import annotations

import json

import httpx
import pytest

from app.core.config import Settings
from app.core.crypto import Crypto, generate_key
from app.db.repositories.integrations import ActivityCacheRepo, IntegrationsRepo
from app.integrations.models import CalendarConfig, GitConfig
from app.integrations.service import ActivityService
from app.relay.service import Relay
from tests.conftest import FakeAdapter, item, resp
from tests.test_integrations import ICS

DAY = "2026-11-17"


def github_handler(calls: list):
    def handler(request: httpx.Request):
        calls.append(request.url.host + request.url.path)
        if request.url.host == "calendar.test":
            return httpx.Response(200, text=ICS)
        if request.url.path == "/search/commits":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "sha": "a1",
                            "html_url": "https://github.com/pay/svc/commit/a1",
                            "parents": [{}],
                            "repository": {"full_name": "pay/svc"},
                            "commit": {
                                "message": "fix: retry payment callback (#482)",
                                "author": {"date": "2026-11-17T02:00:00Z"},
                            },
                        }
                    ]
                },
            )
        if request.url.path == "/search/issues":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "number": 482,
                            "title": "Retry payment callback",
                            "html_url": "https://github.com/pay/svc/pull/482",
                            "state": "closed",
                            "repository_url": "https://api.github.com/repos/pay/svc",
                            "pull_request": {"merged_at": "x"},
                        }
                    ]
                },
            )
        return httpx.Response(404)

    return handler


@pytest.fixture
async def activity(org):
    calls: list[str] = []
    transport = httpx.MockTransport(github_handler(calls))
    svc = ActivityService(
        integrations=IntegrationsRepo(org.db),
        cache=ActivityCacheRepo(org.db),
        crypto=Crypto(generate_key()),
        settings=Settings(),
        transport_factory=lambda route: transport,
    )
    org.relay = Relay(org.users, org.reports, org.audit, svc)
    org.activity, org.calls = svc, calls
    return org


async def connect(org, user="zhangsan"):
    await org.activity.add(user, GitConfig(provider="github", username="zs", token="ghp_secret"))
    await org.activity.add(
        user,
        CalendarConfig(
            provider="outlook", mode="ics", ics_url="https://calendar.test/me.ics", email="me@x.com"
        ),
    )


async def test_credentials_are_encrypted_at_rest(activity):
    await connect(activity)
    raw = [d async for d in activity.db["integrations"].find()]
    assert len(raw) == 2 and all("ghp_secret" not in json.dumps(d, default=str) for d in raw)
    assert {d["label"] for d in raw} == {"github:github.com", "outlook:ics"}


async def test_collect_summarises_and_caches(activity):
    await connect(activity)
    me = await activity.users.get("zhangsan")
    a = await activity.activity.collect(me, DAY)
    assert a["git"]["repos"][0]["prs"][0]["number"] == 482
    assert a["calendar"]["total_minutes"] == 135 and a["errors"] == {}
    n = len(activity.calls)
    await activity.activity.collect(me, DAY)  # a past day: served from the cache
    assert len(activity.calls) == n
    await activity.activity.collect(me, DAY, refresh=True)
    assert len(activity.calls) > n
    doc = (await activity.activity.integrations.for_user("zhangsan", "calendar"))[0]
    assert doc.state["mode_used"] == "ics" and doc.last_ok_at is not None


async def test_one_failing_integration_does_not_hide_the_other(activity):
    await connect(activity)
    await activity.activity.add(
        "zhangsan", GitConfig(provider="gitlab", base_url="https://gitlab.test", token="bad")
    )
    a = await activity.activity.collect(await activity.users.get("zhangsan"), DAY)
    assert a["git"]["totals"]["commits"] == 1 and "gitlab" in a["errors"]["git"]
    bad = next(
        d for d in await activity.activity.integrations.for_user("zhangsan", "git") if d.provider == "gitlab"
    )
    assert bad.status == "error" and bad.last_error


async def test_collector_results_are_used_without_local_credentials(activity):
    await activity.activity.ingest(
        "wangwu",
        DAY,
        "git",
        {"provider": "gitlab", "repos": [], "totals": {"commits": 0, "repos": 0, "prs": 0}},
    )
    a = await activity.activity.collect(await activity.users.get("wangwu"), DAY)
    assert a["git"]["provider"] == "gitlab" and activity.calls == []


async def test_relay_activity_respects_per_source_consent(activity):
    await connect(activity)
    lisi = await activity.relay.viewer("lisi")
    a = await activity.relay.activity(lisi, "zhangsan", DAY)
    assert a["git"] is None and a["calendar"] is None and set(a["unavailable"]) == {"git", "calendar"}
    assert activity.calls == []  # nothing fetched without consent

    await activity.relay.grant_consent(await activity.relay.viewer("zhangsan"), "git")
    a = await activity.relay.activity(lisi, "zhangsan", DAY)
    assert a["git"]["totals"]["commits"] == 1 and a["calendar"] is None
    assert not any("calendar.test" in c for c in activity.calls)  # calendar not even fetched

    boss = await activity.relay.viewer("boss")
    assert set((await activity.relay.activity(boss, "zhangsan", DAY))["unavailable"]) == {"git", "calendar"}
    audit = await activity.audit.recent(target_prefix="user:zhangsan")
    assert any(e["action"] == "activity.read" and e["actor_id"] == "boss" and not e["allowed"] for e in audit)


async def test_generate_daily_is_prefilled_with_own_activity(activity):
    from tests.test_manager_flows import service

    await connect(activity)
    out = {
        "kind": "individual",
        "date": DAY,
        "lang": "zh",
        "plan_check": [],
        "questions": [],
        "own_items": [
            item(
                "done",
                "修复支付回调重试,PR #482 已合并",
                source="git",
                ref_url="https://github.com/pay/svc/pull/482",
            )
        ],
    }
    a = FakeAdapter({"e1": [resp(json.dumps(out, ensure_ascii=False))]})
    runner = service(activity, a).runner
    r = await runner.run("generate-daily", {"narrative": "修了回调重试", "date": DAY}, user_id="zhangsan")
    assert r.status == "ok", r.errors  # git source + ref_url accepted because the data was provided
    prompt = a.calls[0][2].messages[0].text
    assert (
        '<untrusted kind="context" name="own_activity">' in prompt
        and "pull/482" in prompt
        and "对账方案评审" in prompt
    )


async def test_generate_daily_without_integrations_keeps_manual_only(activity):
    from tests.test_manager_flows import service

    fabricated = {
        "kind": "individual",
        "date": DAY,
        "lang": "zh",
        "plan_check": [],
        "questions": [],
        "own_items": [item("done", "合并 PR", source="git", ref_url="https://github.com/x/pull/1")],
    }
    fixed = {**fabricated, "own_items": [item("done", "修了回调重试")]}
    a = FakeAdapter(
        {
            "e1": [
                resp(json.dumps(fabricated, ensure_ascii=False)),
                resp(json.dumps(fixed, ensure_ascii=False)),
            ]
        }
    )
    r = await service(activity, a).runner.run(
        "generate-daily", {"narrative": "修了回调重试", "date": DAY}, user_id="wangwu"
    )
    assert r.status == "ok" and r.validation_attempts == 2  # git source rejected: no git data for wangwu
