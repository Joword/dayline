"""Manual org import (M2; DingTalk directory sync replaces it in M6).

The file is a full snapshot of the org: every user with their manager. Accepted
formats: YAML (``users: [...]``) or CSV (``id,name,email,manager,org_unit,roles,
dingtalk_id,lang,timezone``; ``roles`` separated by ``;``).

Validation happens before anything is written: unique ids and emails, managers
that exist in the file, no cycles. Existing users keep their consents, API tokens
and creation time; consents granted to a previous manager are dropped.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from app.db.models import USER_ID_PATTERN, UserDoc, utcnow
from app.db.repositories.users import UsersRepo

MAX_DEPTH_THREE_LAYER = 3


class OrgImportError(ValueError):
    pass


@dataclass
class ImportPlan:
    users: list[UserDoc]
    warnings: list[str] = field(default_factory=list)


@dataclass
class ImportResult:
    created: list[str]
    updated: list[str]
    deactivated: list[str]
    warnings: list[str]


def parse_file(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".csv":
        rows = []
        for r in csv.DictReader(io.StringIO(text)):
            r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
            r["roles"] = [x for x in r.get("roles", "").split(";") if x]
            rows.append({k: v for k, v in r.items() if v not in ("", [])} | {"roles": r["roles"]})
        return rows
    data = yaml.safe_load(text) or {}
    users = data.get("users") if isinstance(data, dict) else data
    if not isinstance(users, list):
        raise OrgImportError("expected a list under 'users:'")
    return users


def build_plan(rows: list[dict[str, Any]]) -> ImportPlan:
    errors: list[str] = []
    warnings: list[str] = []
    by_id: dict[str, dict[str, Any]] = {}
    emails: dict[str, str] = {}
    for i, r in enumerate(rows, 1):
        uid = str(r.get("id", "")).strip()
        if not re.fullmatch(USER_ID_PATTERN, uid):
            errors.append(f"row {i}: invalid id {uid!r} (letters, digits, . _ -)")
            continue
        if uid in by_id:
            errors.append(f"row {i}: duplicate id {uid}")
            continue
        if not r.get("name"):
            errors.append(f"row {i}: {uid} has no name")
        if email := (r.get("email") or "").lower():
            if email in emails:
                errors.append(f"row {i}: email {email} used by both {emails[email]} and {uid}")
            emails[email] = uid
        by_id[uid] = r
    for uid, r in by_id.items():
        m = r.get("manager") or r.get("manager_id")
        if m and m not in by_id:
            errors.append(f"{uid}: manager {m!r} is not in the file")
        if m == uid:
            errors.append(f"{uid}: is their own manager")
    if errors:
        raise OrgImportError("\n".join(errors))

    # materialised paths; detect cycles
    paths: dict[str, list[str]] = {}

    def chain(uid: str) -> list[str]:
        if uid in paths:
            return paths[uid]
        seen: list[str] = []
        cur: str | None = uid
        while cur is not None:
            if cur in seen:
                raise OrgImportError(f"reporting cycle: {' -> '.join([*seen, cur])}")
            seen.append(cur)
            cur = by_id[cur].get("manager") or by_id[cur].get("manager_id") or None
        paths[uid] = list(reversed(seen))
        return paths[uid]

    users: list[UserDoc] = []
    for uid, r in by_id.items():
        ids = chain(uid)
        if len(ids) > MAX_DEPTH_THREE_LAYER:
            warnings.append(
                f"{uid}: depth {len(ids)} exceeds the three-layer model; skip-level access stays closed"
            )
        try:
            users.append(
                UserDoc(
                    id=uid,
                    name=str(r["name"]),
                    email=(r.get("email") or None),
                    manager_id=r.get("manager") or r.get("manager_id") or None,
                    level=len(ids) - 1,
                    org_unit=r.get("org_unit"),
                    org_path="/" + "/".join(ids) + "/",
                    system_roles=r.get("roles") or [],
                    dingtalk_id=r.get("dingtalk_id"),
                    git_identities=r.get("git") or r.get("git_identities") or [],
                    timezone=r.get("timezone"),
                    lang=r.get("lang"),
                )
            )
        except ValidationError as e:
            errors.append(f"{uid}: {e.errors()[0]['loc']} {e.errors()[0]['msg']}")
    roots = [u.id for u in users if u.manager_id is None]
    if len(roots) > 1:
        warnings.append(f"{len(roots)} people have no manager: {', '.join(roots[:5])}")
    if errors:
        raise OrgImportError("\n".join(errors))
    return ImportPlan(users=users, warnings=warnings)


async def apply_plan(
    repo: UsersRepo, plan: ImportPlan, *, deactivate_missing: bool = False, dry_run: bool = False
) -> ImportResult:
    existing = {u.id: u for u in await repo.all(include_inactive=True)}
    created, updated, merged = [], [], []
    roles_in_file = {u.id for u in plan.users if u.system_roles}
    for u in plan.users:
        old = existing.get(u.id)
        if old is None:
            created.append(u.id)
            merged.append(u)
            continue
        updated.append(u.id)
        merged.append(
            u.model_copy(
                update={
                    "created_at": old.created_at,
                    "updated_at": utcnow(),
                    "api_tokens": old.api_tokens,
                    # consents are bound to the manager they were given to
                    "consents": [c for c in old.consents if c.grantee_id == u.manager_id],
                    "system_roles": u.system_roles if u.id in roles_in_file else old.system_roles,
                }
            )
        )
    missing = [uid for uid, u in existing.items() if u.active and uid not in {x.id for x in plan.users}]
    deactivated = missing if deactivate_missing else []
    warnings = list(plan.warnings)
    if missing and not deactivate_missing:
        warnings.append(
            f"{len(missing)} active users are not in the file (use --deactivate-missing): {', '.join(missing[:5])}"
        )
    if not dry_run:
        await repo.replace_many(merged)
        await repo.deactivate(deactivated)
    return ImportResult(created=created, updated=updated, deactivated=deactivated, warnings=warnings)
