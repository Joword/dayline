"""Intranet collector (plan §9.3, reserved for when the main system runs outside the intranet).

Deployed inside the intranet, it reads sources the main system cannot reach (self-hosted
GitLab, intranet Coremail) and pushes only the *summaries* to
``POST /internal/activity`` using the service token with ``X-Dayline-Client: collector``.
Credentials stay in the collector's own config file (values may be ``${ENV}``), never in
the main system.

    dl collector push --config collector.yaml [--date 2026-11-17]
"""

from __future__ import annotations

from datetime import date as date_cls
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import yaml
from pydantic import BaseModel, Field

from app.integrations.http import TargetClient
from app.integrations.models import CalendarConfig, GitConfig, IntegrationConfig
from app.integrations.service import fetch_source
from app.llm.endpoints import expand_env


class CollectorUser(BaseModel):
    user_id: str
    timezone: str = "Asia/Shanghai"
    git: list[GitConfig] = Field(default_factory=list)
    calendar: list[CalendarConfig] = Field(default_factory=list)


class CollectorConfig(BaseModel):
    target: str  # main system base URL, e.g. https://dayline.example.com
    token: str  # the main system's service token (use ${DAYLINE_COLLECTOR_TOKEN})
    integration_proxy: str | None = None
    users: list[CollectorUser]


def load_collector_config(path: Path) -> CollectorConfig:
    return CollectorConfig.model_validate(expand_env(yaml.safe_load(path.read_text(encoding="utf-8")) or {}))


async def collect_and_push(
    cfg: CollectorConfig,
    day: str,
    *,
    transport_factory=None,
    push_transport: httpx.AsyncBaseTransport | None = None,
) -> list[dict[str, Any]]:
    def http_for(c: IntegrationConfig) -> TargetClient:
        return TargetClient(
            c.proxy,
            fallback_proxy=cfg.integration_proxy,
            ca_bundle=c.ca_bundle,
            transport_factory=transport_factory,
        )

    report = []
    headers = {"Authorization": f"Bearer {cfg.token}", "X-Dayline-Client": "collector"}
    async with httpx.AsyncClient(
        base_url=cfg.target, headers=headers, timeout=30, transport=push_transport
    ) as push:
        for u in cfg.users:
            tz = ZoneInfo(u.timezone)
            start = datetime.combine(date_cls.fromisoformat(day), time.min, tz)
            for source, cfgs in (("git", u.git), ("calendar", u.calendar)):
                if not cfgs:
                    continue
                payload, outcomes = await fetch_source(
                    source, list(cfgs), start, start + timedelta(days=1), tz, http_for
                )
                errors = [o.error for o in outcomes if o.error]
                pushed = False
                if payload is not None:
                    resp = await push.post(
                        "/internal/activity",
                        json={"user_id": u.user_id, "date": day, "source": source, "payload": payload},
                    )
                    pushed = resp.status_code == 200
                    if not pushed:
                        errors.append(f"push failed: HTTP {resp.status_code}")
                report.append({"user_id": u.user_id, "source": source, "pushed": pushed, "errors": errors})
    return report
