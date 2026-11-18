"""Integration configs (credentials) and errors.

A config is validated, then stored encrypted as a whole (``config_enc``); only a
non-secret label (provider + host) stays readable for listings.
"""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class IntegrationError(Exception):
    """Base: a source could not be read. The message is safe to show the user."""


class IntegrationAuthError(IntegrationError):
    pass


class IntegrationUnreachable(IntegrationError):
    pass


class IntegrationNotConfigured(IntegrationError):
    pass


class RateLimited(IntegrationError):
    def __init__(self, msg: str, reset_at: int | None = None):
        super().__init__(msg)
        self.reset_at = reset_at


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # "auto": direct first, then the configured proxy; "none": direct only; or a proxy URL
    proxy: str = "auto"
    ca_bundle: str | None = None  # path to a company CA bundle (self-hosted GitLab, intranet Coremail)


class GitConfig(_Base):
    type: Literal["git"] = "git"
    provider: Literal["github", "gitlab"]
    base_url: str | None = None  # GitHub Enterprise: https://ghe.corp/api/v3 · GitLab: https://gitlab.corp
    token: SecretStr  # read-only: GitHub fine-grained token / GitLab PAT with read_api
    username: str | None = None  # GitHub login (required); GitLab: resolved from the token
    emails: list[str] = Field(default_factory=list)  # commit author emails that are this user

    @model_validator(mode="after")
    def _check(self) -> GitConfig:
        if self.provider == "github" and not self.username:
            raise ValueError("GitHub needs `username` (the account login)")
        return self

    def label(self) -> str:
        host = (
            urlparse(self.base_url).hostname
            if self.base_url
            else ("github.com" if self.provider == "github" else "gitlab.com")
        )
        return f"{self.provider}:{host}"


class GraphCreds(BaseModel):
    tenant: str
    client_id: str
    refresh_token: SecretStr


class CalendarConfig(_Base):
    type: Literal["calendar"] = "calendar"
    provider: Literal["outlook", "coremail"]
    mode: Literal["auto", "graph", "ics", "caldav", "api", "imap"] = "auto"
    email: str | None = None  # the user's own address, to read their own attendee response
    ics_url: SecretStr | None = None  # published calendar URLs grant read access: treat as secret
    caldav_url: str | None = None
    imap_host: str | None = None
    imap_port: int = 993
    username: str | None = None
    # Coremail: client-specific password / authorization code only - never the mailbox password
    password: SecretStr | None = None
    graph: GraphCreds | None = None

    @model_validator(mode="after")
    def _check(self) -> CalendarConfig:
        allowed = {"outlook": {"auto", "graph", "ics"}, "coremail": {"auto", "caldav", "api", "ics", "imap"}}
        if self.mode not in allowed[self.provider]:
            raise ValueError(f"{self.provider} supports modes {sorted(allowed[self.provider])}")
        if self.mode == "graph" and not self.graph:
            raise ValueError("mode graph needs a Microsoft sign-in (dl integrations connect-outlook)")
        if self.mode == "ics" and not self.ics_url:
            raise ValueError("mode ics needs ics_url")
        if self.mode in ("caldav", "imap") and not (self.username and self.password):
            raise ValueError(f"mode {self.mode} needs username and a client-specific password")
        if self.mode == "caldav" and not self.caldav_url:
            raise ValueError("mode caldav needs caldav_url")
        if self.mode == "imap" and not self.imap_host:
            raise ValueError("mode imap needs imap_host")
        if self.mode == "auto" and not (self.graph or self.ics_url or self.caldav_url or self.imap_host):
            raise ValueError("configure at least one of: graph sign-in, ics_url, caldav_url, imap_host")
        return self

    def label(self) -> str:
        host = urlparse(self.caldav_url).hostname if self.caldav_url else (self.imap_host or "")
        return f"{self.provider}:{self.mode}" + (f":{host}" if host else "")


IntegrationConfig = GitConfig | CalendarConfig


def reveal(cfg: BaseModel) -> dict[str, Any]:
    """Dump with secrets in clear text - only for encryption."""

    def walk(v: Any) -> Any:
        if isinstance(v, SecretStr):
            return v.get_secret_value()
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        return v

    return walk(cfg.model_dump())


def parse_config(data: dict[str, Any]) -> IntegrationConfig:
    return (
        GitConfig.model_validate(data) if data.get("type") == "git" else CalendarConfig.model_validate(data)
    )
