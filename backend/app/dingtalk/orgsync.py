"""DingTalk directory -> org tree (plan §10), and reading a user's DingTalk log (optional import).

Sync walks the departments from ``dingtalk_root_dept_id``, reads every user's detail and
builds the same rows a manual import file has, so ``app.org.importer`` validates and
applies them (cycles, duplicates, consent clean-up on manager change).

* Dayline id: kept for users already linked by ``dingtalk_id``; otherwise the local part
  of their email; otherwise ``dt_<userid>``.
* Manager: DingTalk's 直属主管 (``manager_userid``); if empty, the leader of their first
  department (or of a parent department when they lead it themselves).
"""

from __future__ import annotations

import asyncio
import re
from datetime import date as date_cls
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.db.models import UserDoc
from app.dingtalk.client import DingTalkClient

_BAD = re.compile(r"[^A-Za-z0-9._-]+")


async def fetch_directory(
    client: DingTalkClient, root: int = 1, *, concurrency: int = 8
) -> tuple[dict[int, dict], list[dict]]:
    depts: dict[int, dict] = {}
    queue = [root]
    while queue:
        d = queue.pop(0)
        if d in depts:
            continue
        depts[d] = await client.department(d) or {"dept_id": d}
        queue += [x for x in await client.sub_department_ids(d) if x not in depts]
    userids: list[str] = []
    for d in depts:
        userids += [u for u in await client.department_user_ids(d) if u not in userids]
    sem = asyncio.Semaphore(concurrency)

    async def detail(uid: str) -> dict:
        async with sem:
            return await client.user(uid)

    users = [u for u in await asyncio.gather(*(detail(u) for u in userids)) if u]
    return depts, users


def _candidate_id(u: dict) -> str:
    email = (u.get("org_email") or u.get("email") or "").strip()
    base = email.split("@")[0] if email else f"dt_{u['userid']}"
    base = _BAD.sub("_", base).strip("._-") or f"dt_{_BAD.sub('_', u['userid'])}"
    return base[:64] if base[0].isalnum() else f"u{base}"[:64]


def directory_rows(
    depts: dict[int, dict], users: list[dict], existing: list[UserDoc], root: int = 1
) -> list[dict[str, Any]]:
    linked = {u.dingtalk_id: u.id for u in existing if u.dingtalk_id}
    # people imported by hand before DingTalk sync: link them by email instead of duplicating
    by_email = {u.email.lower(): u.id for u in existing if u.email and not u.dingtalk_id}
    reserved = {u.id for u in existing if not u.dingtalk_id}
    ids: dict[str, str] = {}
    for u in users:
        email = (u.get("org_email") or u.get("email") or "").lower()
        uid = linked.get(u["userid"]) or by_email.get(email)
        if uid is None:
            base = uid = _candidate_id(u)
            n = 2
            while uid in ids.values() or uid in reserved:
                uid, n = f"{base}{n}", n + 1
        ids[u["userid"]] = uid

    def leader_of(dept_id: int, exclude: str) -> str | None:
        while dept_id in depts:
            info = depts[dept_id]
            for lead in info.get("dept_manager_userid_list") or []:
                if lead != exclude and lead in ids:
                    return lead
            parent = info.get("parent_id")
            if not parent or parent == dept_id:
                return None
            dept_id = parent
        return None

    rows = []
    for u in users:
        dept_ids = [d for d in u.get("dept_id_list") or [] if d in depts]
        unit_dept = next((d for d in dept_ids if d != root), dept_ids[0] if dept_ids else None)
        manager = u.get("manager_userid") if u.get("manager_userid") in ids else None
        if manager is None and unit_dept is not None:
            manager = leader_of(unit_dept, exclude=u["userid"])
        rows.append(
            {
                "id": ids[u["userid"]],
                "name": u.get("name") or u["userid"],
                "email": (u.get("org_email") or u.get("email") or None),
                "manager": ids.get(manager) if manager else None,
                "org_unit": depts[unit_dept].get("name") if unit_dept is not None else None,
                "dingtalk_id": u["userid"],
            }
        )
    return rows


async def fetch_log_text(
    client: DingTalkClient, staff_id: str, day: str, *, template_name: str = "", tz: str = "Asia/Shanghai"
) -> str | None:
    """The user's own DingTalk log for `day` as plain text (input for /gen; no two-way sync)."""
    start = datetime.combine(date_cls.fromisoformat(day), time.min, ZoneInfo(tz))
    end = start + timedelta(days=1)
    res = await client.list_reports(
        userid=staff_id,
        start_ms=int(start.timestamp() * 1000),
        end_ms=int(end.timestamp() * 1000),
        template_name=template_name,
    )
    logs = res.get("data_list") or []
    if not logs:
        return None
    contents = sorted(logs[0].get("contents") or [], key=lambda c: int(c.get("sort", 0)))
    return "\n\n".join(
        f"{c.get('key', '')}:\n{c.get('value', '')}".strip() for c in contents if c.get("value")
    )
