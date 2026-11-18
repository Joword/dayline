"""ActivityService: collect a user's git and calendar activity for one day.

* credentials are decrypted only here, right before use;
* results are cached in ``activity_cache`` (TTL index): today's data is re-fetched after
  ``activity_fresh_minutes``, past days stay cached; the intranet ``collector`` can push
  results into the same cache (``ingest``) when the main system cannot reach a source;
* one failing integration does not hide the others: partial data plus per-source errors;
* each integration records its last success / error and, for calendars, the backend
  that worked (``auto`` mode remembers it).

Access control is not done here - Relay calls this service after its checks.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date as date_cls
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.core.config import Settings
from app.core.crypto import Crypto
from app.db.models import UserDoc, utcnow
from app.db.repositories.integrations import ActivityCacheRepo, IntegrationDoc, IntegrationsRepo
from app.integrations.calendar import CalendarSource, Event, summarize_calendar
from app.integrations.git import Commit, PullRequest, make_git_source, summarize_git
from app.integrations.http import TargetClient, TransportFactory
from app.integrations.models import (
    CalendarConfig,
    GitConfig,
    IntegrationConfig,
    IntegrationError,
    parse_config,
    reveal,
)

log = logging.getLogger("dayline.integrations")

SOURCES = ("git", "calendar")


class ActivityService:
    def __init__(
        self,
        *,
        integrations: IntegrationsRepo,
        cache: ActivityCacheRepo,
        crypto: Crypto,
        settings: Settings,
        transport_factory: TransportFactory | None = None,
    ):
        self.integrations = integrations
        self.cache = cache
        self.crypto = crypto
        self.settings = settings
        self.transport_factory = transport_factory

    # --- management -----------------------------------------------------------------

    async def add(self, user_id: str, cfg: IntegrationConfig) -> IntegrationDoc:
        doc = IntegrationDoc(
            user_id=user_id,
            type=cfg.type,
            provider=cfg.provider,
            label=cfg.label(),
            config_enc=self.crypto.encrypt(reveal(cfg)),
        )
        return await self.integrations.add(doc)

    def _http(self, cfg: IntegrationConfig) -> TargetClient:
        return TargetClient(
            cfg.proxy,
            fallback_proxy=self.settings.integration_proxy,
            ca_bundle=cfg.ca_bundle,
            transport_factory=self.transport_factory,
        )

    def _tz(self, user: UserDoc) -> ZoneInfo:
        return ZoneInfo(user.timezone or self.settings.timezone)

    def day_range(self, user: UserDoc, day: str) -> tuple[datetime, datetime]:
        start = datetime.combine(date_cls.fromisoformat(day), time.min, self._tz(user))
        return start, start + timedelta(days=1)

    # --- collection -------------------------------------------------------------------

    async def collect(
        self, user: UserDoc, day: str, *, refresh: bool = False, sources: tuple[str, ...] = SOURCES
    ) -> dict[str, Any]:
        out: dict[str, Any] = {"date": day, "git": None, "calendar": None, "errors": {}}
        for source in sources:
            docs = await self.integrations.for_user(user.id, source)
            cached = await self.cache.get(user.id, day, source)
            if cached and not refresh and (not docs or self._fresh(cached, user, day)):
                out[source] = cached["payload"]
                continue
            if not docs:
                continue
            try:
                payload, errors = await self._fetch(source, docs, user, day)
            except IntegrationError as e:
                out["errors"][source] = str(e)
                if cached:
                    out[source] = {**cached["payload"], "stale": True}
                continue
            if errors:
                out["errors"][source] = "; ".join(errors)
            await self.cache.put(user.id, day, source, payload)
            out[source] = payload
        return out

    def _fresh(self, cached: dict[str, Any], user: UserDoc, day: str) -> bool:
        if cached.get("origin") == "collector":
            return True
        today = datetime.now(self._tz(user)).date().isoformat()
        if day < today:
            return True
        fetched = cached["fetched_at"]
        return utcnow() - fetched < timedelta(minutes=self.settings.activity_fresh_minutes)

    async def _fetch(
        self, source: str, docs: list[IntegrationDoc], user: UserDoc, day: str
    ) -> tuple[dict[str, Any], list[str]]:
        start, end = self.day_range(user, day)
        cfgs: list[IntegrationConfig] = []
        for doc in docs:
            cfg = parse_config(self.crypto.decrypt(doc.config_enc))
            if isinstance(cfg, CalendarConfig) and not cfg.email and user.email:
                cfg = cfg.model_copy(update={"email": user.email})  # to recognise the user's own declines
            cfgs.append(cfg)
        payload, outcomes = await fetch_source(
            source,
            cfgs,
            start,
            end,
            self._tz(user),
            self._http,
            remembered=[d.state.get("mode_used") for d in docs],
        )
        errors = []
        for doc, cfg, o in zip(docs, cfgs, outcomes, strict=True):
            if o.error:
                log.warning(
                    "integration failed", extra={"integration": doc.id, "label": doc.label, "error": o.error}
                )
                await self.integrations.record_result(doc.id, ok=False, error=o.error)
                errors.append(f"{doc.label}: {o.error}")
                continue
            new_enc = None
            if o.rotated_refresh_token and isinstance(cfg, CalendarConfig) and cfg.graph:
                graph = cfg.graph.model_copy(update={"refresh_token": o.rotated_refresh_token})
                new_enc = self.crypto.encrypt(reveal(cfg.model_copy(update={"graph": graph})))
            state = {**doc.state, "route": o.route} | ({"mode_used": o.mode_used} if o.mode_used else {})
            await self.integrations.record_result(doc.id, ok=True, state=state, config_enc=new_enc)
        if payload is None:
            raise IntegrationError("; ".join(errors))
        return payload, errors

    async def ingest(self, user_id: str, day: str, source: str, payload: dict[str, Any]) -> None:
        """Results pushed by the intranet collector (plan §9.3)."""
        if source not in SOURCES:
            raise ValueError(f"unknown source {source}")
        await self.cache.put(user_id, day, source, payload, origin="collector")


@dataclass
class Outcome:
    error: str | None = None
    mode_used: str | None = None
    route: str | None = None
    rotated_refresh_token: str | None = None


async def fetch_source(
    source: str,
    cfgs: list[IntegrationConfig],
    start: datetime,
    end: datetime,
    tz: ZoneInfo,
    http_for: Callable[[IntegrationConfig], TargetClient],
    *,
    remembered: list[str | None] | None = None,
) -> tuple[dict[str, Any] | None, list[Outcome]]:
    """Fetch one source over several configs and summarise it. Returns (payload or None if
    every config failed, one outcome per config). Shared by the service and the collector."""
    commits: list[Commit] = []
    prs: list[PullRequest] = []
    events: list[Event] = []
    outcomes: list[Outcome] = []
    providers: list[str] = []
    for i, cfg in enumerate(cfgs):
        http = http_for(cfg)
        try:
            if isinstance(cfg, GitConfig):
                c, p = await make_git_source(cfg, http).fetch(start, end)
                commits += c
                prs += p
                outcomes.append(Outcome(route=http.route_used))
            else:
                src = CalendarSource(cfg, http, tz, remembered=(remembered or [None] * len(cfgs))[i])
                events += await src.fetch(start, end)
                outcomes.append(
                    Outcome(
                        mode_used=src.mode_used,
                        route=http.route_used,
                        rotated_refresh_token=src.rotated_refresh_token,
                    )
                )
            providers.append(cfg.provider)
        except IntegrationError as e:
            outcomes.append(Outcome(error=str(e)))
        finally:
            await http.aclose()
    if not providers:
        return None, outcomes
    if source == "git":
        return summarize_git(commits, prs, provider="+".join(providers), tz=tz), outcomes
    return summarize_calendar(events, start, end, tz=tz), outcomes
