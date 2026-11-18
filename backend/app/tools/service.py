"""Member tool lifecycle (plan §8.3): pending -> approved -> enabled, plus disabled / rejected.

submit    CI (or the owner / a developer) registers tool.yaml + the tools/list it captured
          from the built image. A re-submission voids earlier approvals and takes the tool
          out of use until it is approved and enabled again; an identical one is a no-op.
approve   a developer other than the owner. Locks the definition hash. ``manager_data``
          tools need two developers and an explicit acknowledgement from each.
enable    after the pinned image is deployed: the live tools/list must hash to the
          approved definition.
disable   a developer or the owner; also automatic when the live definition changes.

Visibility (who may use an enabled tool):
  private  the owner only
  team     the owner's team: everyone under the owner's manager
  level    the owner's department: everyone under the owner's manager's manager
  company  everyone
A tool whose owner is no longer active is unavailable until a developer reassigns it.
"""

from __future__ import annotations

import logging
from typing import Any

from app.db.models import ToolApproval, ToolDoc, ToolEvent, ToolHealth, UserDoc, utcnow
from app.db.repositories.tools import ToolsRepo
from app.db.repositories.users import UsersRepo
from app.notify.service import Notifier
from app.tools.contract import Finding, check_definition, definition_hash, normalize
from app.tools.manifest import DIGEST_RE, ToolManifest

log = logging.getLogger("dayline.tools")

CI_ACTOR = "ci"


class ToolError(Exception):
    def __init__(self, msg: str, findings: list[Finding] | None = None):
        super().__init__(msg)
        self.findings = findings or []


class ToolForbidden(ToolError):
    pass


class ToolNotFound(ToolError):
    pass


def approvals_needed(doc: ToolDoc) -> int:
    return 2 if doc.manager_data else 1


def current_approvals(doc: ToolDoc) -> list[ToolApproval]:
    return [a for a in doc.approvals if a.definition_hash == doc.definition_hash]


async def unavailable_reason(users: UsersRepo, doc: ToolDoc, user_id: str) -> str | None:
    owner = await users.get(doc.owner)
    if owner is None:
        return "its owner is no longer active"
    if user_id == owner.id or doc.visibility == "company":
        return None
    if doc.visibility == "private":
        return "it is private to its owner"
    user = await users.get(user_id)
    if user is None:
        return "you are not in the org"
    anchor: UserDoc | None = await users.get(owner.manager_id) if owner.manager_id else None
    if doc.visibility == "level" and anchor is not None and anchor.manager_id:
        anchor = await users.get(anchor.manager_id) or anchor
    if anchor is None or user.org_path.startswith(anchor.org_path):
        return None
    return f"it is shared with the owner's {'team' if doc.visibility == 'team' else 'department'} only"


class ToolService:
    def __init__(
        self,
        *,
        repo: ToolsRepo,
        users: UsersRepo,
        notifier: Notifier | None = None,
        connector: Any = None,  # app.tools.mcp_adapter.McpConnector; None without DAYLINE_TOOLS_SECRET
        source: Any = None,  # app.tools.mcp_adapter.McpToolSource
    ):
        self.repo = repo
        self.users = users
        self.notifier = notifier or Notifier(None)
        self.connector = connector
        self.source = source

    # --- helpers ---------------------------------------------------------------------------

    async def get(self, name: str) -> ToolDoc:
        doc = await self.repo.get(name)
        if doc is None:
            raise ToolNotFound(f"no tool named '{name}'")
        return doc

    async def _user(self, user_id: str) -> UserDoc:
        u = await self.users.get(user_id)
        if u is None:
            raise ToolForbidden(f"unknown or inactive user: {user_id}")
        return u

    async def _developer(self, user_id: str) -> UserDoc:
        u = await self._user(user_id)
        if "developer" not in u.system_roles:
            raise ToolForbidden("only developers can do this")
        return u

    async def developers(self) -> list[str]:
        return [u.id for u in await self.users.all() if "developer" in u.system_roles]

    def _need_connector(self):
        if self.connector is None:
            raise ToolError("member tools are off: set DAYLINE_TOOLS_SECRET")
        return self.connector

    def _changed(self) -> None:
        if self.source is not None:
            self.source.invalidate()

    async def unavailable_reason(self, doc: ToolDoc, user_id: str) -> str | None:
        return await unavailable_reason(self.users, doc, user_id)

    async def usable_by(self, user_id: str) -> list[ToolDoc]:
        return [
            d
            for d in await self.repo.list(["enabled"])
            if await unavailable_reason(self.users, d, user_id) is None
        ]

    async def live_definition(self, doc: ToolDoc) -> list[dict[str, Any]]:
        try:
            return await self._need_connector().definition(doc)
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"cannot reach {doc.endpoint}: {type(e).__name__}: {e}") from e

    # --- submit ---------------------------------------------------------------------------

    async def submit(
        self,
        actor_id: str,
        manifest: ToolManifest,
        *,
        definition: list[dict[str, Any]],
        image_digest: str | None = None,
    ) -> tuple[ToolDoc, list[Finding], bool]:
        """Returns (doc, warnings for the reviewer, changed)."""
        await self._user(manifest.owner)
        existing = await self.repo.get(manifest.name)
        if actor_id != CI_ACTOR:
            actor = await self._user(actor_id)
            is_dev = "developer" in actor.system_roles
            if not is_dev and actor.id != manifest.owner:
                raise ToolForbidden("only the owner, a developer or CI can submit a tool")
            if existing is not None and existing.owner != manifest.owner and not is_dev:
                raise ToolForbidden(
                    f"'{manifest.name}' belongs to {existing.owner}; ask a developer to transfer it"
                )
        digest = image_digest or manifest.image_digest
        if digest is None or not DIGEST_RE.fullmatch(digest):
            raise ToolError("an image digest (sha256:...) is required: tools run pinned to a built image")
        if manifest.image_digest and image_digest and manifest.image_digest != image_digest:
            raise ToolError("tool.yaml names a different image digest than the one given")
        definition = normalize(definition)
        findings = check_definition(manifest.name, definition)
        if errors := [f for f in findings if f.level == "error"]:
            raise ToolError("the tool definition failed the contract checks", errors)
        h = definition_hash(definition)
        doc = ToolDoc(
            name=manifest.name,
            owner=manifest.owner,
            version=manifest.version,
            language=manifest.language,
            description=manifest.description,
            image=manifest.image_name,
            image_digest=digest,
            endpoint=manifest.resolved_endpoint(),
            permissions=manifest.permissions,
            egress=manifest.network_egress,
            visibility=manifest.visibility,
            manager_data=manifest.manager_data,
            timeout_s=manifest.timeout_s,
            resources=manifest.resources.model_dump(),
            status="pending",
            definition=definition,
            definition_hash=h,
            submitted_by=actor_id,
        )
        warnings = [f for f in findings if f.level == "warning"]
        if existing is not None and existing.status != "rejected" and _same(existing, doc):
            return existing, warnings, False
        saved = await self.repo.upsert_submission(
            doc, ToolEvent(actor_id=actor_id, action="submitted", note=f"v{doc.version} {h}")
        )
        self._changed()
        await self.notifier.notify(
            await self.developers(),
            "tool.submitted",
            f"成员工具 {doc.name} v{doc.version} 等待审阅",
            body=f"负责人 {doc.owner} · 权限 {', '.join(doc.permissions) or '无'} · 出网 {', '.join(doc.egress) or '无'}"
            + (" · 需要管理者数据(两人审批)" if doc.manager_data else ""),
            dedupe_key=f"tool.submitted:{doc.name}:{h}",
        )
        return saved, warnings, True

    # --- review ---------------------------------------------------------------------------

    async def approve(self, actor_id: str, name: str, *, ack_manager_data: bool = False) -> ToolDoc:
        dev = await self._developer(actor_id)
        doc = await self.get(name)
        if doc.status != "pending":
            raise ToolError(f"'{name}' is {doc.status}; only pending tools can be approved")
        if dev.id == doc.owner:
            raise ToolForbidden("the owner cannot approve their own tool")
        approvals = current_approvals(doc)
        if any(a.by == dev.id for a in approvals):
            raise ToolError("you have already approved this definition; it needs another developer")
        if doc.manager_data and not ack_manager_data:
            raise ToolError("this tool reads manager data: approve with the manager-data acknowledgement")
        done = len(approvals) + 1 >= approvals_needed(doc)
        approval = ToolApproval(
            by=dev.id, definition_hash=doc.definition_hash, manager_data_ack=ack_manager_data
        )
        updated = await self.repo.transition(
            name,
            expect_status=["pending"],
            expect_hash=doc.definition_hash,
            event=ToolEvent(actor_id=dev.id, action="approved", note=doc.definition_hash),
            set_={"status": "approved"} if done else {},
            push_approval=approval.model_dump(),
        )
        if updated is None:
            raise ToolError(f"'{name}' changed meanwhile; review it again")
        if done:
            await self.notifier.notify(
                [doc.owner],
                "tool.approved",
                f"成员工具 {name} v{doc.version} 已通过审阅",
                body="部署锁定版本的镜像后,由 developer 启用即可使用。",
                dedupe_key=f"tool.approved:{name}:{doc.definition_hash}",
            )
        return updated

    async def reject(self, actor_id: str, name: str, note: str) -> ToolDoc:
        dev = await self._developer(actor_id)
        if not note.strip():
            raise ToolError("say why the tool is rejected")
        updated = await self.repo.transition(
            name,
            expect_status=["pending", "approved"],
            event=ToolEvent(actor_id=dev.id, action="rejected", note=note),
            set_={"status": "rejected"},
        )
        if updated is None:
            raise ToolError(f"'{name}' is not pending or approved")
        await self.notifier.notify([updated.owner], "tool.rejected", f"成员工具 {name} 未通过审阅", body=note)
        return updated

    # --- run state ------------------------------------------------------------------------

    async def enable(self, actor_id: str, name: str) -> ToolDoc:
        dev = await self._developer(actor_id)
        doc = await self.get(name)
        if doc.status not in ("approved", "disabled"):
            raise ToolError(f"'{name}' is {doc.status}; it must be approved first")
        if len(current_approvals(doc)) < approvals_needed(doc):
            raise ToolError(f"'{name}' does not have the approvals its current definition needs")
        live = definition_hash(await self.live_definition(doc))
        if live != doc.definition_hash:
            raise ToolError(
                f"the deployed tool's definition ({live}) differs from the approved one ({doc.definition_hash}); "
                "deploy the approved image or submit the new version for review"
            )
        updated = await self.repo.transition(
            name,
            expect_status=["approved", "disabled"],
            expect_hash=doc.definition_hash,
            event=ToolEvent(actor_id=dev.id, action="enabled"),
            set_={"status": "enabled", "disabled_reason": None, "health": ToolHealth(ok=True).model_dump()},
        )
        if updated is None:
            raise ToolError(f"'{name}' changed meanwhile")
        self._changed()
        await self.notifier.notify(
            [doc.owner], "tool.enabled", f"成员工具 {name} 已上线", body=f"v{doc.version}"
        )
        return updated

    async def disable(self, actor_id: str, name: str, reason: str) -> ToolDoc:
        actor = await self._user(actor_id)
        doc = await self.get(name)
        if "developer" not in actor.system_roles and actor.id != doc.owner:
            raise ToolForbidden("only a developer or the owner can disable a tool")
        if not reason.strip():
            raise ToolError("say why the tool is disabled")
        updated = await self.repo.transition(
            name,
            expect_status=["enabled", "approved"],
            event=ToolEvent(actor_id=actor.id, action="disabled", note=reason),
            set_={"status": "disabled", "disabled_reason": reason},
        )
        if updated is None:
            raise ToolError(f"'{name}' is not enabled or approved")
        self._changed()
        recipients = [doc.owner] if actor.id != doc.owner else await self.developers()
        await self.notifier.notify(recipients, "tool.disabled", f"成员工具 {name} 已停用", body=reason)
        return updated

    async def definition_changed(self, doc: ToolDoc, live_hash: str) -> None:
        """The live tools/list no longer matches the approval: stop using the tool."""
        reason = f"definition changed: approved {doc.definition_hash[:19]}, live {live_hash[:19]}"
        updated = await self.repo.transition(
            doc.name,
            expect_status=["enabled"],
            expect_hash=doc.definition_hash,
            event=ToolEvent(actor_id="system", action="definition_changed", note=f"live {live_hash}"),
            set_={"status": "disabled", "disabled_reason": reason},
        )
        self._changed()
        if updated is None:
            return  # already handled by another process
        log.warning("tool disabled: definition changed", extra={"tool": doc.name, "live": live_hash})
        await self.notifier.notify(
            [*await self.developers(), doc.owner],
            "tool.definition_changed",
            f"成员工具 {doc.name} 的定义发生变化,已自动停用",
            body="线上 tools/list 与审批时不一致。确认变更后重新提交审阅;若非预期,检查镜像和部署。",
            dedupe_key=f"tool.changed:{doc.name}:{live_hash}",
        )

    async def verify(self, names: list[str] | None = None) -> list[tuple[str, str]]:
        """Re-check enabled tools against their approved definition. Returns (name, outcome)."""
        out = []
        for doc in await self.repo.list(["enabled"]):
            if names and doc.name not in names:
                continue
            try:
                live = definition_hash(await self.live_definition(doc))
            except ToolError as e:
                await self.repo.set_health(doc.name, ToolHealth(ok=False, error=str(e)[:500]).model_dump())
                out.append((doc.name, f"unreachable: {e}"))
                continue
            if live != doc.definition_hash:
                await self.definition_changed(doc, live)
                out.append((doc.name, "definition changed: disabled"))
                continue
            await self.repo.set_health(doc.name, ToolHealth(ok=True, checked_at=utcnow()).model_dump())
            out.append((doc.name, "ok"))
        return out


def _same(a: ToolDoc, b: ToolDoc) -> bool:
    keys = (
        "owner",
        "version",
        "language",
        "description",
        "image",
        "image_digest",
        "endpoint",
        "permissions",
        "egress",
        "visibility",
        "manager_data",
        "timeout_s",
        "resources",
        "definition_hash",
    )
    return all(getattr(a, k) == getattr(b, k) for k in keys)
