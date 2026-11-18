"""DingTalk bot: commands and skill routing (plan §10). Independent of the Stream SDK, so
it is unit-testable; ``stream.py`` feeds it messages and delivers its replies.

    /gen <今天做了什么>      draft today's report; the card's button sends `/confirm <id>`
    /confirm <id>          submit the draft (then synced to DingTalk 日志 through the outbox)
    /review <日报原文>       review someone's report
    /team [日期]            manager: who has submitted
    /rollup [日期]          manager: draft the team roll-up; the button sends `/rollup-confirm <id>`
    /rollup-confirm <id> [1,2] [f:<升级id>]   confirm, raising candidates 1,2 / forwarding held items
    /escalations           items waiting for you;  /esc accept|resolve|forward <id> [备注]
    /help                  commands, including every skill's `/command` from the registry
    anything else          natural language -> intent routing

Buttons use DingTalk's ``dtmd://dingtalkclient/sendMessage`` links: clicking one sends the
command back to the bot as the user, so generate -> confirm -> report works inside DingTalk
without registering interactive-card callbacks.

Results with personal data are sent one-to-one when the command came from a group chat.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from pymongo.errors import DuplicateKeyError

from app.db.models import UserDoc, utcnow
from app.escalations.service import EscalationError
from app.llm.types import NoEndpointAvailable
from app.relay.service import RelayError
from app.reports.service import ReportError
from app.skills.registry import SkillNotFound
from app.skills.router import primary_text_input
from app.skills.runner import SkillForbidden, SkillInputError, SkillRunError, SkillRunResult

log = logging.getLogger("dayline.dingtalk.bot")

Progress = Callable[[str], Awaitable[None]]


@dataclass
class Incoming:
    msg_id: str
    text: str
    sender_staff_id: str | None
    sender_nick: str = ""
    conversation_type: str = "1"  # "1" one-to-one, "2" group
    session_webhook: str | None = None

    @property
    def in_group(self) -> bool:
        return self.conversation_type == "2"


@dataclass
class Reply:
    title: str
    text: str  # Markdown
    buttons: list[tuple[str, str]] = field(default_factory=list)  # (title, url)
    private: bool = False  # contains personal data: from a group, deliver one-to-one


def command_link(command: str) -> str:
    """A button URL that, when clicked in DingTalk, sends `command` to the bot as the user."""
    return "dtmd://dingtalkclient/sendMessage?content=" + quote(command, safe="")


BUILTIN = {
    "/gen": "写日报:/gen 今天做了什么",
    "/confirm": "确认提交草稿:/confirm <id>",
    "/review": "审阅日报:/review <日报原文>",
    "/team": "团队提交情况(管理者):/team [日期]",
    "/rollup": "团队汇总草稿(管理者):/rollup [日期]",
    "/rollup-confirm": "确认团队汇总:/rollup-confirm <id> [上报的候选编号,如 1,2]",
    "/escalations": "等我处理的升级事项",
    "/esc": "处理升级事项:/esc accept|resolve|forward <id> [备注]",
    "/help": "帮助",
}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class BotService:
    def __init__(self, services: Any):
        self.s = services

    # --- entry point -----------------------------------------------------------------------

    async def handle(self, msg: Incoming, progress: Progress | None = None) -> list[Reply]:
        if not await self._first_time(msg.msg_id):
            return []  # Stream redelivery of a message we already handled
        user = await self.s.users.find_by_dingtalk_id(msg.sender_staff_id) if msg.sender_staff_id else None
        if user is None:
            return [
                Reply(
                    "未绑定",
                    "你的钉钉账号还没有关联 Dayline 用户,请联系管理员同步组织架构(`dl dingtalk sync-org`)。",
                )
            ]
        text = msg.text.strip()
        progress = progress or _noop
        try:
            return await self._dispatch(user, text, progress)
        except (
            SkillForbidden,
            RelayError,
            EscalationError,
            ReportError,
            SkillInputError,
            SkillNotFound,
        ) as e:
            return [Reply("无法完成", f"⚠️ {_reason(e)}")]
        except SkillRunError as e:
            if isinstance(e.__cause__, NoEndpointAvailable):
                return [Reply("暂时不可用", "⚠️ 模型服务暂时不可用,请稍后再试。")]
            return [Reply("处理失败", f"⚠️ 处理失败:{e}")]

    async def _first_time(self, msg_id: str) -> bool:
        if not msg_id or self.s.mongo is None:
            return True
        try:
            await self.s.mongo.db["dingtalk_events"].insert_one({"_id": msg_id, "created_at": utcnow()})
        except DuplicateKeyError:
            return False
        return True

    async def _dispatch(self, user: UserDoc, text: str, progress: Progress) -> list[Reply]:
        cmd, _, rest = text.partition(" ")
        cmd, rest = cmd.lower(), rest.strip()
        handlers = {
            "/gen": self._gen,
            "/confirm": self._confirm,
            "/review": self._review,
            "/team": self._team,
            "/rollup": self._rollup,
            "/rollup-confirm": self._rollup_confirm,
            "/escalations": self._escalations,
            "/esc": self._esc,
        }
        if not text or cmd in ("/help", "帮助", "help"):
            return [self._help()]
        if cmd in handlers:
            return await handlers[cmd](user, rest, progress)
        route = self.s.router.route(text)
        if route is None and not text.startswith("/"):
            route = await self.s.router.resolve(
                text, self.s.gateway, lang=self._lang(user), is_manager=await self.s.relay.is_manager(user.id)
            )
        if route is None:
            return [self._help("没有听懂。")]
        if route.skill.name == "generate-daily":
            return await self._gen(user, route.rest, progress)
        field_name = primary_text_input(route.skill)
        if field_name is None:
            return [Reply(route.skill.name, f"`{route.skill.name}` 需要结构化输入,请在网页上使用。")]
        await progress(f"⏳ 正在运行 {route.skill.name} …")
        result = await self.s.runner.run(
            route.skill.name,
            {field_name: route.rest},
            user_id=user.id,
            lang=self._lang(user),
            client_id="dingtalk",
        )
        return [self._result(route.skill.name, result)]

    # --- commands ----------------------------------------------------------------------------

    async def _gen(self, user: UserDoc, text: str, progress: Progress) -> list[Reply]:
        if not text:
            return [Reply("写日报", "用法:`/gen 今天做了什么`,写得越具体越好;git 和日历会自动带上。")]
        await progress("⏳ 正在生成日报 …")
        result = await self.s.runner.run(
            "generate-daily",
            {"narrative": text},
            user_id=user.id,
            lang=self._lang(user),
            client_id="dingtalk",
        )
        if result.status != "ok":
            return [self._result("日报", result)]
        doc = await self.s.report_service.save_generated(
            user.id, result, {"narrative": text, "source": "dingtalk"}
        )
        md = result.markdown
        questions = (result.output or {}).get("questions") or []
        if questions:
            md += "\n\n---\n补充这些信息后重新发送 `/gen …` 会更完整:\n" + "\n".join(
                f"- {q}" for q in questions
            )
        if result.notice:
            md += f"\n\n> {result.notice}"
        buttons = [("确认提交", command_link(f"/confirm {doc['id']}"))]
        if url := self._url("/generate"):
            buttons.append(("在网页上修改", url))
        return [Reply(f"日报草稿 v{doc['version']}", md, buttons, private=True)]

    async def _confirm(self, user: UserDoc, rest: str, progress: Progress) -> list[Reply]:
        report_id = rest.split()[0] if rest else ""
        if not report_id:
            return [Reply("确认", "用法:`/confirm <id>`(点击日报卡片上的“确认提交”即可)")]
        doc = await self.s.report_service.confirm(user.id, report_id)
        to = ""
        if user.manager_id and (m := await self.s.users.get(user.manager_id)):
            to = f",{m.name} 现在可以看到"
        synced = (
            "\n\n同时会同步到钉钉日志。"
            if self.s.outbox is not None and self.s.outbox.enabled and user.dingtalk_id
            else ""
        )
        return [Reply("已提交", f"✅ {doc['date']} 的日报已提交(v{doc['version']}){to}。{synced}")]

    async def _review(self, user: UserDoc, rest: str, progress: Progress) -> list[Reply]:
        if not rest:
            return [Reply("审阅日报", "用法:`/review <日报原文>`")]
        await progress("⏳ 正在审阅 …")
        result = await self.s.runner.run(
            "review-member", {"report": rest}, user_id=user.id, lang=self._lang(user), client_id="dingtalk"
        )
        return [self._result("审阅结果", result)]

    async def _team(self, user: UserDoc, rest: str, progress: Progress) -> list[Reply]:
        day = rest if DATE_RE.match(rest) else self.s.runner.today().isoformat()
        snap = await self.s.relay.team_snapshot(await self.s.relay.viewer(user.id, client_id="dingtalk"), day)
        lines = [f"#### 团队提交情况 {day}", f"已提交 {len(snap.submitted)} / {len(snap.members)}", ""]
        lines += [f"- {m.user.name}:{'✅ 已提交' if m.report else '⏳ 未提交'}" for m in snap.members]
        buttons = [("生成团队汇总", command_link(f"/rollup {day}"))] if snap.submitted else []
        return [Reply("团队提交情况", "\n".join(lines), buttons, private=True)]

    async def _rollup(self, user: UserDoc, rest: str, progress: Progress) -> list[Reply]:
        day = rest if DATE_RE.match(rest) else None
        await progress("⏳ 正在生成团队汇总 …")
        result, doc, warnings = await self.s.report_service.draft_rollup(user.id, day, lang=self._lang(user))
        if doc is None:
            return [self._result("团队汇总", result)]
        md = doc["rendered_md"]
        candidates = doc["team_summary"].get("escalation_candidates") or []
        if candidates:
            md += f"\n\n---\n要把候选事项上报,发送 `/rollup-confirm {doc['id']} 1,2`(编号见上)。"
        md += "".join(f"\n\n> ⚠️ {w}" for w in warnings)
        buttons = [("确认并发送(不上报)", command_link(f"/rollup-confirm {doc['id']}"))]
        if url := self._url(f"/team?date={doc['date']}"):
            buttons.append(("在网页上处理", url))
        return [Reply(f"团队汇总草稿 {doc['date']}", md, buttons, private=True)]

    async def _rollup_confirm(self, user: UserDoc, rest: str, progress: Progress) -> list[Reply]:
        parts = rest.replace(",", ",").split()
        if not parts:
            return [Reply("确认团队汇总", "用法:`/rollup-confirm <id> [1,2] [f:<升级id>]`")]
        report_id, escalate, forward = parts[0], [], []
        for p in parts[1:]:
            for tok in p.split(","):
                if tok.startswith("f:"):
                    forward.append(tok[2:])
                elif tok.isdigit():
                    escalate.append(int(tok))
        doc, stale = await self.s.report_service.confirm_rollup(
            user.id, report_id, escalate=escalate, forward=forward
        )
        n = len(doc["team_summary"].get("escalations") or [])
        text = f"✅ {doc['date']} 的团队汇总已发送(上报 {n} 项)。"
        if stale:
            text += f"\n\n注意:{'、'.join(stale)} 在草稿生成后更新了日报。"
        return [Reply("团队汇总已发送", text)]

    async def _escalations(self, user: UserDoc, rest: str, progress: Progress) -> list[Reply]:
        items = await self.s.escalations.find_for(user.id, "held")
        if not items:
            return [Reply("升级事项", "没有等你处理的升级事项。")]
        lines = ["#### 等你处理的升级事项", ""]
        lines += [f"- `{e.id}` {e.text}(来自 {e.origin.user_id},{e.origin.date})" for e in items]
        lines += ["", "处理:`/esc accept <id>`、`/esc resolve <id> 解决方式`、`/esc forward <id>`"]
        return [Reply("升级事项", "\n".join(lines), private=True)]

    async def _esc(self, user: UserDoc, rest: str, progress: Progress) -> list[Reply]:
        action, _, tail = rest.partition(" ")
        esc_id, _, note = tail.strip().partition(" ")
        fns = {
            "accept": self.s.escalations.accept,
            "forward": self.s.escalations.escalate,
            "resolve": self.s.escalations.resolve,
        }
        if action not in fns or not esc_id:
            return [Reply("升级事项", "用法:`/esc accept|resolve|forward <id> [备注]`")]
        e = await (
            fns[action](user.id, esc_id, note)
            if action == "resolve"
            else fns[action](user.id, esc_id, note or None)
        )
        labels = {"in_progress": "处理中", "escalated": "已上报", "resolved": "已解决", "open": "跟踪中"}
        return [
            Reply(
                "升级事项", f"✅ `{e.id}` {labels.get(e.status, e.status)} · 当前处理人 {e.current_holder_id}"
            )
        ]

    # --- helpers ------------------------------------------------------------------------------

    def _help(self, prefix: str = "") -> Reply:
        lines = [prefix, "#### Dayline 指令", ""] if prefix else ["#### Dayline 指令", ""]
        lines += [f"- `{k}` {v}" for k, v in BUILTIN.items()]
        extra = [
            (c, s.manifest.description)
            for s in self.s.registry.list()
            for c in s.manifest.triggers.commands
            if c.startswith("/") and c not in BUILTIN and c.split()[0] not in BUILTIN
        ]
        if extra:
            lines += ["", "其他 skill:"] + [f"- `{c}` {d}" for c, d in extra]
        lines += ["", "也可以直接用自然语言说明你想做什么。"]
        return Reply("帮助", "\n".join(lines))

    def _result(self, title: str, result: SkillRunResult) -> Reply:
        if result.status != "ok":
            return Reply(title, "⚠️ 输出没有通过校验:\n" + "\n".join(f"- {e}" for e in result.errors))
        text = result.markdown + (f"\n\n> {result.notice}" if result.notice else "")
        return Reply(title, text, private=True)

    def _lang(self, user: UserDoc) -> str:
        return user.lang or self.s.settings.default_lang

    def _url(self, path: str) -> str | None:
        base = self.s.settings.public_url
        return f"{base.rstrip('/')}{path}" if base else None


async def _noop(_: str) -> None:
    return None


def _reason(e: Exception) -> str:
    return e.args[0] if isinstance(e, KeyError) and e.args else str(e)


# --- delivery ----------------------------------------------------------------------------------


def webhook_payload(reply: Reply) -> dict[str, Any]:
    if reply.buttons:
        return {
            "msgtype": "actionCard",
            "actionCard": {
                "title": reply.title,
                "text": reply.text,
                "btnOrientation": "0",
                "btns": [{"title": t, "actionURL": u} for t, u in reply.buttons],
            },
        }
    return {"msgtype": "markdown", "markdown": {"title": reply.title, "text": reply.text}}


async def deliver(client: Any, msg: Incoming, replies: list[Reply]) -> None:
    """Send replies: in the conversation, or one-to-one when private data came from a group."""
    for r in replies:
        if msg.in_group and r.private and msg.sender_staff_id:
            if r.buttons:
                await client.send_action_card([msg.sender_staff_id], r.title, r.text, r.buttons)
            else:
                await client.send_markdown([msg.sender_staff_id], r.title, r.text)
            if msg.session_webhook:
                await client.reply(
                    msg.session_webhook,
                    {"msgtype": "text", "text": {"content": f"已私聊发送给 {msg.sender_nick or '你'}。"}},
                )
        elif msg.session_webhook:
            await client.reply(msg.session_webhook, webhook_payload(r))
