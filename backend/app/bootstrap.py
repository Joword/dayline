"""Builds the service graph shared by the API, the CLI and the worker."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from app.core.config import Settings
from app.core.crypto import Crypto
from app.db.client import Mongo
from app.db.indexes import ensure_indexes
from app.db.repositories.escalations import EscalationsRepo, NotificationsRepo
from app.db.repositories.integrations import ActivityCacheRepo, IntegrationsRepo
from app.db.repositories.misc import AuditRepo, ReviewsRepo
from app.db.repositories.reports import ReportsRepo
from app.db.repositories.skill_runs import SkillRunsRepo
from app.db.repositories.users import UsersRepo
from app.dingtalk.client import client_from_settings
from app.dingtalk.outbox import OutboxRepo, OutboxService
from app.escalations.service import EscalationService
from app.integrations.service import ActivityService
from app.llm.endpoints import load_llm_config
from app.llm.gateway import LLMGateway
from app.notify.service import Notifier
from app.relay.service import Relay
from app.reports.service import ReportService
from app.rules.engine import RuleEngine
from app.skills.registry import SkillRegistry
from app.skills.router import SkillRouter
from app.skills.runner import SkillRunner
from app.tools.registry import ToolRegistry
from app.tools.service import ToolService

if TYPE_CHECKING:
    from app.tools.mcp_adapter import McpToolSource

log = logging.getLogger("dayline")


@dataclass
class Services:
    settings: Settings
    gateway: LLMGateway
    tools: ToolRegistry
    rules: RuleEngine
    registry: SkillRegistry
    router: SkillRouter
    runner: SkillRunner
    mongo: Mongo | None = None
    reports: ReportsRepo | None = None
    runs: SkillRunsRepo | None = None
    users: UsersRepo | None = None
    reviews: ReviewsRepo | None = None
    audit: AuditRepo | None = None
    relay: Relay | None = None
    report_service: ReportService | None = None
    integrations: IntegrationsRepo | None = None
    activity: ActivityService | None = None
    notifications: NotificationsRepo | None = None
    notifier: Notifier | None = None
    escalations: EscalationService | None = None
    tool_service: ToolService | None = None
    tool_source: McpToolSource | None = None
    db_error: str | None = None

    async def aclose(self) -> None:
        await self.gateway.aclose()
        if self.mongo is not None:
            await self.mongo.close()


async def build_services(settings: Settings, *, db: Literal["api", "cli", "off"] = "cli") -> Services:
    """``api``: repositories always wired (readiness reports DB health);
    ``cli``: DB used only if reachable, otherwise runs are not persisted;
    ``off``: no DB at all."""
    gateway = LLMGateway(load_llm_config(settings.llm_config_path))
    tools = ToolRegistry.with_local_tools()
    rules = RuleEngine()
    registry = SkillRegistry(
        settings.skills_path,
        known_tools=tools.permissions(),
        known_rules=rules.names(),
        hot_reload=settings.skills_hot_reload,
    )
    services = Services(
        settings=settings,
        gateway=gateway,
        tools=tools,
        rules=rules,
        registry=registry,
        router=SkillRouter(registry),
        runner=None,  # type: ignore[arg-type]  # set below
    )
    if db != "off":
        mongo = Mongo(settings)
        reachable = await mongo.ping()
        if reachable:
            try:
                await ensure_indexes(mongo.db, settings.activity_cache_ttl_days)
            except Exception as e:  # e.g. replica set still initialising
                log.warning("could not ensure indexes", extra={"error": str(e)})
        if reachable or db == "api":
            if not reachable:
                services.db_error = "MongoDB not reachable at start-up"
                log.error(services.db_error)
            services.mongo = mongo
            services.reports = ReportsRepo(mongo.db)
            services.runs = SkillRunsRepo(mongo.db)
            services.users = UsersRepo(mongo.db)
            services.reviews = ReviewsRepo(mongo.db)
            services.audit = AuditRepo(mongo.db)
            services.integrations = IntegrationsRepo(mongo.db)
            secret = settings.encryption_key.get_secret_value() if settings.encryption_key else None
            services.activity = ActivityService(
                integrations=services.integrations,
                cache=ActivityCacheRepo(mongo.db),
                crypto=Crypto(secret),
                settings=settings,
            )
            services.relay = Relay(services.users, services.reports, services.audit, services.activity)
        else:
            services.db_error = "MongoDB not reachable"
            await mongo.close()
    services.runner = SkillRunner(
        registry=registry,
        gateway=gateway,
        tools=tools,
        rules=rules,
        runs_repo=services.runs,
        relay=services.relay,
        default_lang=settings.default_lang,
        timezone=settings.timezone,
        trace_max_bytes=settings.trace_max_bytes,
    )
    if services.relay is not None and services.mongo is not None:
        services.notifications = NotificationsRepo(services.mongo.db)
        services.dingtalk = client_from_settings(settings)
        services.outbox = OutboxService(
            repo=OutboxRepo(services.mongo.db),
            client=services.dingtalk,
            reports=services.reports,  # type: ignore[arg-type]
            users=services.users,  # type: ignore[arg-type]
            settings=settings,
        )
        services.notifier = Notifier(services.notifications, outbox=services.outbox)
        services.escalations = EscalationService(
            repo=EscalationsRepo(services.mongo.db),
            users=services.users,  # type: ignore[arg-type]
            relay=services.relay,
            notifier=services.notifier,
        )
        services.report_service = ReportService(
            runner=services.runner,
            relay=services.relay,
            reports=services.reports,  # type: ignore[arg-type]
            reviews=services.reviews,  # type: ignore[arg-type]
            escalations=services.escalations,
            notifier=services.notifier,
            outbox=services.outbox,
        )
        await wire_tools(services)
    return services


async def wire_tools(services: Services) -> None:
    """Member MCP tools (M7). The lifecycle works without the secret; running tools needs it."""
    from app.db.repositories.tools import ToolsRepo

    st = services.settings
    if st.tools_secret is not None:
        from app.tools.mcp_adapter import McpConnector, McpToolSource
    repo = ToolsRepo(services.mongo.db)  # type: ignore[union-attr]
    connector = McpConnector(st.tools_secret.get_secret_value()) if st.tools_secret is not None else None
    services.tool_service = ToolService(
        repo=repo,
        users=services.users,  # type: ignore[arg-type]
        notifier=services.notifier,
        connector=connector,
    )
    if connector is None:
        return
    source = McpToolSource(
        repo=repo,
        users=services.users,  # type: ignore[arg-type]
        connector=connector,
        api_url=st.tools_api_url,
        availability=services.tool_service.unavailable_reason,
        sync_interval_s=st.tools_sync_seconds,
    )
    source.on_definition_changed = services.tool_service.definition_changed
    services.tool_service.source = source
    services.tool_source = source
    await services.tools.add_source(source)
