"""M6 DingTalk: client, templates, outbox, bot, org sync."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import pytest

from app.core.config import Settings
from app.db.models import UserDoc
from app.dingtalk.bot import BotService, Incoming, command_link, deliver
from app.dingtalk.client import DingTalkClient, DingTalkError
from app.dingtalk.orgsync import directory_rows
from app.dingtalk.outbox import OutboxRepo, OutboxService
from app.dingtalk.templates import (
    TemplateMapping,
    render_contents,
    save_mapping,
    template_field_names,
    template_hash,
    template_state,
)
from app.integrations.http import TargetClient
from app.org.importer import build_plan
from tests.conftest import FakeAdapter, item, resp, submit
from tests.test_manager_flows import ROLLUP
from tests.test_worker import services as m5_services

DAY = "2026-11-17"


# --- a fake DingTalk OpenAPI -------------------------------------------------------------------


class FakeDingTalk:
    def __init__(self, *, fail_create: int = 0, template_fields: list[str] | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.tokens = 0
        self.fail_create = fail_create
        self.template_fields = template_fields or ["今日完成工作", "未完成工作", "需协调工作", "明日计划"]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content or b"{}")
        self.calls.append((path, body))
        if path == "/v1.0/oauth2/accessToken":
            self.tokens += 1
            return httpx.Response(200, json={"accessToken": f"tok{self.tokens}", "expireIn": 7200})
        if path == "/v1.0/robot/oToMessages/batchSend":
            assert request.headers["x-acs-dingtalk-access-token"].startswith("tok")
            return httpx.Response(200, json={"processQueryKey": "q"})
        if path.startswith("/topapi/"):
            assert parse_qs(request.url.query.decode())["access_token"][0].startswith("tok")
        if path == "/topapi/report/template/getbyname":
            fields = [{"field_name": f, "sort": i, "type": 1} for i, f in enumerate(self.template_fields)]
            return httpx.Response(
                200,
                json={
                    "errcode": 0,
                    "result": {"id": "tpl1", "name": body["template_name"], "fields": fields},
                },
            )
        if path == "/topapi/report/create":
            if self.fail_create > 0:
                self.fail_create -= 1
                return httpx.Response(200, json={"errcode": 500, "errmsg": "system busy"})
            return httpx.Response(200, json={"errcode": 0, "result": f"log-{len(self.calls)}"})
        if path == "/hook":
            return httpx.Response(200, json={"errcode": 0})
        return httpx.Response(404, json={"errcode": 404, "errmsg": "not found"})

    def client(self) -> DingTalkClient:
        t = httpx.MockTransport(self.handler)
        return DingTalkClient("appkey", "secret", http=TargetClient("none", transport_factory=lambda r: t))

    def paths(self) -> list[str]:
        return [p for p, _ in self.calls]


async def test_client_token_cache_messages_and_errors():
    fake = FakeDingTalk()
    c = fake.client()
    await c.send_markdown(["dt_zs"], "标题", "**内容**")
    await c.send_action_card(
        ["dt_zs"], "t", "x", [("确认", command_link("/confirm 1")), ("网页", "https://d/x")]
    )
    assert fake.tokens == 1  # cached
    body = fake.calls[1][1]
    assert body["robotCode"] == "appkey" and body["msgKey"] == "sampleMarkdown"
    assert json.loads(body["msgParam"]) == {"title": "标题", "text": "**内容**"}
    card = json.loads(fake.calls[2][1]["msgParam"])
    assert fake.calls[2][1]["msgKey"] == "sampleActionCard2" and card["actionURL1"].startswith(
        "dtmd://dingtalkclient/sendMessage?content="
    )
    log_id = await c.create_report(
        userid="dt_zs", template_id="tpl1", contents=[{"key": "k", "content": "c"}], to_userids=["dt_ls"]
    )
    assert log_id.startswith("log-")
    param = fake.calls[-1][1]["create_report_param"]
    assert (
        param["userid"] == "dt_zs"
        and param["template_id"] == "tpl1"
        and param["to_userids"] == ["dt_ls"]
        and param["dd_from"] == "dayline"
    )
    with pytest.raises(DingTalkError):
        await c.department(1)  # 404 from the fake


async def test_client_refreshes_an_invalid_token_once():
    fake = FakeDingTalk()
    first = {"done": False}
    orig = fake.handler

    def handler(request):
        if request.url.path == "/topapi/v2/department/get" and not first["done"]:
            first["done"] = True
            return httpx.Response(200, json={"errcode": 40014, "errmsg": "invalid token"})
        if request.url.path == "/topapi/v2/department/get":
            return httpx.Response(200, json={"errcode": 0, "result": {"dept_id": 1, "name": "公司"}})
        return orig(request)

    t = httpx.MockTransport(handler)
    c = DingTalkClient("k", "s", http=TargetClient("none", transport_factory=lambda r: t))
    assert (await c.department(1))["name"] == "公司" and fake.tokens == 2


def test_command_link_round_trips():
    url = command_link("/confirm 6afd19bf 1,2")
    assert unquote(urlparse(url).query.removeprefix("content=")) == "/confirm 6afd19bf 1,2"


# --- templates ----------------------------------------------------------------------------------

OUTPUT = {
    "date": DAY,
    "own_items": [
        item("done", "上线重试"),
        item("progress", "对账报表", progress="60%"),
        item("blocker", "等风控文档", need_from="风控组"),
        item("plan", "完成导出", priority="P1"),
    ],
    "plan_check": [{"plan_text": "完成导出", "status": "in_progress"}],
    "team_summary": {
        "summary": "整体正常",
        "escalations": [{"text": "环境不稳定", "need_from": "运维"}],
        "missing": [{"id": "z", "name": "赵六"}],
    },
}


def test_render_contents_follows_the_mapping_and_keeps_unmapped_content():
    m = TemplateMapping(
        template_hash="h",
        fields=[
            {"dingtalk_field": "今日完成工作", "source": "own_items[kind=done]"},
            {
                "dingtalk_field": "需协调工作",
                "source": ["own_items[kind=blocker]", "team_summary.escalations"],
                "format": "bullet_list",
            },
        ],
    )
    c = render_contents(OUTPUT, m, lang="zh")
    assert [x["key"] for x in c] == ["今日完成工作", "需协调工作"]
    assert c[0]["content"] == "1. 上线重试" and c[0]["type"] == "1" and c[0]["content_type"] == "markdown"
    assert "- 等风控文档(需要: 风控组)" in c[1]["content"] and "- 环境不稳定 — 运维" in c[1]["content"]
    # nothing is lost: progress, plan, plan check, summary, missing go to the last field
    for s in ("对账报表", "完成导出", "整体正常", "赵六"):
        assert s in c[1]["content"]
    dropped = render_contents(OUTPUT, m.model_copy(update={"unmapped_policy": "drop"}))
    assert "对账报表" not in dropped[1]["content"]


def test_default_rendering_and_template_state(tmp_path: Path):
    c = render_contents(OUTPUT, None, default_field="今日工作")
    assert len(c) == 1 and c[0]["key"] == "今日工作" and "# 日报 2026-11-17" in c[0]["content"]
    tpl, mp = tmp_path / "t.md", tmp_path / "m.yaml"
    tpl.write_text("", encoding="utf-8")
    assert template_state(tpl, mp).empty and template_state(tpl, mp).usable
    tpl.write_text("## 今日完成工作\n## 明日计划\n", encoding="utf-8")
    st = template_state(tpl, mp)
    assert st.stale and not st.usable  # filled but not mapped
    save_mapping(
        mp,
        TemplateMapping(
            template_hash=st.hash,
            fields=[{"dingtalk_field": "今日完成工作", "source": "own_items[kind=done]"}],
        ),
    )
    assert template_state(tpl, mp).usable
    tpl.write_text("## 今日完成工作\n## 明日计划\n## 新字段\n", encoding="utf-8")
    assert template_state(tpl, mp).stale  # the company changed the template
    assert template_field_names("## 今日完成工作\n明日计划:\n- 备注:\n正文") == [
        "今日完成工作",
        "明日计划",
        "备注",
    ]
    assert template_hash(" a \n") == template_hash("a")
    with pytest.raises(ValueError, match="unknown sources"):
        TemplateMapping(template_hash="h", fields=[{"dingtalk_field": "x", "source": "own_items[kind=nope]"}])


# --- outbox + bot (MongoDB) ---------------------------------------------------------------------


async def dingtalk_services(org, adapter: FakeAdapter, fake: FakeDingTalk, tmp_path: Path, **settings):
    s = m5_services(org, adapter)
    s.settings = Settings(
        dingtalk_app_key="appkey",
        dingtalk_app_secret="secret",
        public_url="https://dayline.test",
        dingtalk_template_path=tmp_path / "dingtalk_report.md",
        dingtalk_mapping_path=tmp_path / "dingtalk_mapping.yaml",
        **settings,
    )
    s.dingtalk = fake.client()
    s.outbox = OutboxService(
        repo=OutboxRepo(org.db), client=s.dingtalk, reports=org.reports, users=org.users, settings=s.settings
    )
    s.notifier.outbox = s.outbox
    s.report_service.outbox = s.outbox
    s.router = __import__("app.skills.router", fromlist=["SkillRouter"]).SkillRouter(s.registry)
    await org.users.col.update_many({}, [{"$set": {"dingtalk_id": {"$concat": ["dt_", "$_id"]}}}])
    return s


async def test_confirm_queues_log_sync_and_outbox_delivers(org, tmp_path):
    fake = FakeDingTalk()
    s = await dingtalk_services(org, FakeAdapter(), fake, tmp_path)
    draft = await submit(org.reports, "zhangsan", DAY, [item("done", "上线重试")], confirm=False)
    await s.report_service.confirm("zhangsan", draft["id"])
    assert (await org.reports.get(draft["id"]))["sync"]["status"] == "pending"
    assert (
        await s.outbox.repo.enqueue(
            "dingtalk.log", {"report_id": draft["id"]}, dedupe_key=f"log:{draft['id']}"
        )
        is False
    )  # deduped
    handled = await s.outbox.process_due()
    assert handled == [("dingtalk.log", "done")]
    synced = (await org.reports.get(draft["id"]))["sync"]
    assert synced["status"] == "synced" and synced["dingtalk_log_id"].startswith("log-")
    param = next(b for p, b in fake.calls if p == "/topapi/report/create")["create_report_param"]
    assert param["userid"] == "dt_zhangsan" and param["to_userids"] == ["dt_lisi"]  # sent to the manager
    assert (
        param["contents"][0]["key"] == "今日完成工作" and "上线重试" in param["contents"][0]["content"]
    )  # empty template: first field


async def test_outbox_retries_then_parks_and_mapping_controls_sync(org, tmp_path):
    from app.dingtalk import outbox as ob

    fake = FakeDingTalk(fail_create=1)
    s = await dingtalk_services(org, FakeAdapter(), fake, tmp_path)
    doc = await submit(org.reports, "zhangsan", DAY, [item("done", "x")])
    await s.outbox.enqueue_report_sync(doc)
    assert await s.outbox.process_due() == [("dingtalk.log", "pending")]  # DingTalk said "busy": retry later
    row = (await s.outbox.repo.list("pending"))[0]
    assert row["attempts"] == 1 and "system busy" in row["last_error"]
    assert (await org.reports.get(doc["id"]))["sync"]["status"] == "pending"
    assert await s.outbox.process_due() == []  # not due yet (backoff)
    await s.outbox.repo.col.update_many({}, {"$set": {"next_retry_at": ob.utcnow()}})
    assert await s.outbox.process_due() == [("dingtalk.log", "done")]

    # a filled template without a confirmed mapping stops the sync (no API call)
    (tmp_path / "dingtalk_report.md").write_text("## 今日完成工作\n## 明日计划\n", encoding="utf-8")
    doc2 = await submit(org.reports, "wangwu", DAY, [item("done", "y")])
    await s.outbox.enqueue_report_sync(doc2)
    before = fake.paths().count("/topapi/report/create")
    assert await s.outbox.process_due() == [("dingtalk.log", "pending")]
    assert fake.paths().count("/topapi/report/create") == before
    assert "dl template map" in (await s.outbox.repo.list("pending"))[0]["last_error"]

    # a user without DingTalk is a permanent failure
    await org.users.col.update_one({"_id": "zhaoliu"}, {"$unset": {"dingtalk_id": ""}})
    await s.outbox.repo.enqueue("dingtalk.message", {"user_id": "zhaoliu", "title": "t", "text": ""})
    await s.outbox.repo.col.update_many({"type": "dingtalk.log"}, {"$set": {"status": "done"}})
    assert await s.outbox.process_due() == [("dingtalk.message", "dead")]
    assert await s.outbox.repo.retry_dead() == 1


async def test_notifications_also_go_to_dingtalk(org, tmp_path):
    fake = FakeDingTalk()
    s = await dingtalk_services(org, FakeAdapter(), fake, tmp_path)
    await s.notifier.notify(
        ["wangwu"], "report.missing", "日报还没交", link="/generate", dedupe_key="report.missing:x"
    )
    await s.notifier.notify(
        ["wangwu"], "report.missing", "日报还没交", link="/generate", dedupe_key="report.missing:x"
    )
    assert await s.outbox.process_due() == [("dingtalk.message", "done")]  # once, despite two notify calls
    sent = json.loads(next(b for p, b in fake.calls if p.endswith("batchSend"))["msgParam"])
    assert "日报还没交" in sent["text"] and "https://dayline.test/generate" in sent["text"]


def msg(text: str, sender: str = "dt_zhangsan", *, group: bool = False, mid: str | None = None) -> Incoming:
    return Incoming(
        msg_id=mid or f"m-{hash((text, sender, group)) & 0xFFFFFF}",
        text=text,
        sender_staff_id=sender,
        sender_nick="nick",
        conversation_type="2" if group else "1",
        session_webhook="https://oapi.dingtalk.com/hook",
    )


GEN = {
    "kind": "individual",
    "date": DAY,
    "lang": "zh",
    "plan_check": [],
    "questions": ["导出什么时候能做?"],
    "own_items": [item("done", "上线支付回调重试"), item("blocker", "等风控文档", severity="high")],
}


async def test_bot_generate_confirm_and_report_in_dingtalk(org, tmp_path):
    fake = FakeDingTalk()
    a = FakeAdapter({"e1": [resp(json.dumps(GEN, ensure_ascii=False))]})
    s = await dingtalk_services(org, a, fake, tmp_path)
    bot = BotService(s)
    progress: list[str] = []

    async def note(t: str) -> None:
        progress.append(t)

    [card] = await bot.handle(msg("/gen 上线了支付回调重试,导出卡在风控文档"), progress=note)
    assert progress == ["⏳ 正在生成日报 …"] and card.private
    assert "上线支付回调重试" in card.text and "导出什么时候能做?" in card.text
    confirm_url = card.buttons[0][1]
    assert card.buttons[0][0] == "确认提交" and card.buttons[1][1] == "https://dayline.test/generate"
    command = unquote(urlparse(confirm_url).query.removeprefix("content="))
    assert command.startswith("/confirm ")

    # the user taps the button: DingTalk sends the command back as a message
    [done] = await bot.handle(msg(command))
    assert "已提交" in done.text and "李四" in done.text and "钉钉日志" in done.text
    assert await bot.handle(msg(command)) == []  # same message delivered again: ignored
    report = await org.reports.latest("zhangsan", DAY)
    assert report["status"] == "confirmed" and report["sync"]["status"] == "pending"
    assert await s.outbox.process_due() == [("dingtalk.log", "done")]

    # the manager sees the team, drafts and confirms the roll-up, raising one candidate
    a.script["e1"] = [resp(json.dumps(ROLLUP, ensure_ascii=False))]
    [team] = await bot.handle(msg("/team " + DAY, "dt_lisi"))
    assert "张三:✅ 已提交" in team.text and team.buttons[0][1] == command_link(f"/rollup {DAY}")
    [draft] = await bot.handle(msg(f"/rollup {DAY}", "dt_lisi"))
    assert "建议上报" in draft.text and "/rollup-confirm" in draft.text
    rid = unquote(urlparse(draft.buttons[0][1]).query.removeprefix("content=")).split()[1]
    [sent] = await bot.handle(msg(f"/rollup-confirm {rid} 1", "dt_lisi"))
    assert "已发送(上报 1 项)" in sent.text
    [esc] = await bot.handle(msg("/escalations", "dt_boss"))
    esc_id = esc.text.split("`")[1]
    [acc] = await bot.handle(msg(f"/esc accept {esc_id} 我来", "dt_boss"))
    assert "处理中" in acc.text


async def test_bot_permissions_identity_and_group_privacy(org, tmp_path):
    fake = FakeDingTalk()
    s = await dingtalk_services(
        org, FakeAdapter({"e1": [resp(json.dumps(GEN, ensure_ascii=False))]}), fake, tmp_path
    )
    bot = BotService(s)
    [who] = await bot.handle(msg("/gen x", "dt_stranger"))
    assert "没有关联" in who.text
    [no] = await bot.handle(msg("/rollup", "dt_zhangsan"))
    assert "⚠️" in no.text and "manager" in no.text
    [helped] = await bot.handle(msg("/help"))
    assert "/gen" in helped.text and "/review" in helped.text
    # from a group chat, the private draft goes one-to-one; the group only sees a short note
    m = msg("/gen 做了 x", group=True)
    replies = await bot.handle(m)
    await deliver(s.dingtalk, m, replies)
    paths = fake.paths()
    assert "/v1.0/robot/oToMessages/batchSend" in paths
    hook = [b for p, b in fake.calls if p == "/hook"]
    assert hook == [{"msgtype": "text", "text": {"content": "已私聊发送给 nick。"}}]
    # in a one-to-one chat, a card goes back through the session webhook as an actionCard
    await deliver(s.dingtalk, msg("x"), replies)
    assert [b for p, b in fake.calls if p == "/hook"][-1]["msgtype"] == "actionCard"


async def test_bot_routes_free_text_with_intents(org, tmp_path):
    fake = FakeDingTalk()
    a = FakeAdapter(
        {
            "e1": [
                resp(json.dumps({"skill": "generate-daily", "confidence": 0.9})),
                resp(json.dumps(GEN, ensure_ascii=False)),
            ]
        }
    )
    s = await dingtalk_services(org, a, fake, tmp_path)
    [card] = await BotService(s).handle(msg("帮我写今天的日报:上线了支付回调重试"))
    assert card.title.startswith("日报草稿") and a.calls[0][2].tier == "small"


# --- org sync -----------------------------------------------------------------------------------


def test_directory_rows_map_managers_ids_and_links():
    depts = {
        1: {"dept_id": 1, "name": "公司", "dept_manager_userid_list": ["u_boss"]},
        2: {"dept_id": 2, "name": "支付组", "parent_id": 1, "dept_manager_userid_list": ["u_li"]},
    }
    users = [
        {"userid": "u_boss", "name": "赵总", "org_email": "boss@corp.com", "dept_id_list": [1]},
        {
            "userid": "u_li",
            "name": "李四",
            "email": "lisi@corp.com",
            "dept_id_list": [2],
        },  # leads 支付组: manager = parent's leader
        {
            "userid": "u_zs",
            "name": "张三",
            "email": "zhangsan@corp.com",
            "dept_id_list": [2],
            "manager_userid": "u_li",
        },
        {"userid": "u_ww", "name": "王五", "dept_id_list": [2]},  # no email, no manager_userid -> dept leader
    ]
    existing = [
        UserDoc(id="zs", name="张三", email="ZhangSan@corp.com", org_path="/zs/"),  # imported by hand earlier
        UserDoc(id="lisi", name="other", org_path="/lisi/"),  # an unrelated person owns the id "lisi"
    ]
    rows = {r["dingtalk_id"]: r for r in directory_rows(depts, users, existing)}
    assert rows["u_zs"]["id"] == "zs"  # linked by email, not duplicated
    assert rows["u_li"]["id"] == "lisi2"  # "lisi" is taken by someone else
    assert rows["u_ww"]["id"] == "dt_u_ww"
    assert rows["u_zs"]["manager"] == "lisi2" and rows["u_ww"]["manager"] == "lisi2"
    assert rows["u_li"]["manager"] == "boss" and rows["u_boss"]["manager"] is None
    assert rows["u_zs"]["org_unit"] == "支付组"
    plan = build_plan(list(rows.values()))
    assert {u.id: u.org_path for u in plan.users}["zs"] == "/boss/lisi2/zs/"
