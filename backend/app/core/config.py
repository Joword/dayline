"""Application settings (pydantic-settings).

Everything is read from the environment (prefix ``DAYLINE_``) or a ``.env`` file.
Secrets for LLM endpoints are *not* listed here: each endpoint in
``config/llm_endpoints.yaml`` names the env var that holds its key.
"""

from __future__ import annotations

import getpass
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _find_home() -> Path:
    """Locate the project home: the directory holding ``config/`` and ``skills/``.

    Order: ``DAYLINE_HOME`` env var, then walk up from the CWD, then from this file.
    In the container the image sets ``DAYLINE_HOME=/app``.
    """
    env = os.environ.get("DAYLINE_HOME")
    if env:
        return Path(env).resolve()
    for start in (Path.cwd(), Path(__file__).resolve().parent):
        for p in (start, *start.parents):
            if (p / "skills" / "builtin").is_dir() or (p / "config" / "llm_endpoints.yaml").is_file():
                return p
    return Path.cwd()


class Settings(BaseSettings):
    # `.env` itself is loaded into os.environ by get_settings(), so endpoint keys
    # (ANTHROPIC_API_KEY, ...) that are not Settings fields are visible too.
    model_config = SettingsConfigDict(
        env_prefix="DAYLINE_",
        env_file_encoding="utf-8",
        env_ignore_empty=True,  # `DAYLINE_API_TOKEN=` in .env means "unset", not "empty token"
        extra="ignore",
        secrets_dir="/run/secrets" if Path("/run/secrets").is_dir() else None,
    )

    env: Literal["dev", "prod", "test"] = "dev"
    home: Path = Field(default_factory=_find_home)

    # logging
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"

    # mongo
    mongo_uri: str = "mongodb://localhost:27017/?replicaSet=rs0&directConnection=true"
    mongo_db: str = "dayline"
    mongo_timeout_ms: int = 3000

    # llm / skills (relative paths are resolved against `home`)
    llm_config: Path = Path("config/llm_endpoints.yaml")
    skills_dir: Path = Path("skills")
    skills_hot_reload: bool = True

    # behaviour
    default_lang: Literal["zh", "en"] = "zh"
    timezone: str = "Asia/Shanghai"
    user: str = Field(default_factory=lambda: os.environ.get("USER") or getpass.getuser())

    # API
    # M1 has no login yet (org + auth arrive in M2). When set, every non-health request
    # must send `Authorization: Bearer <token>`. Required when env=prod.
    api_token: SecretStr | None = None

    # worker schedule: config/schedules.yaml (per org unit, staggered by level); these two
    # are the defaults when that file is absent. HH:MM in `timezone`.
    schedule_config: Path = Path("config/schedules.yaml")
    deadline_time: str = Field(default="18:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    rollup_draft_time: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")

    # integrations (M3)
    # Fernet key(s) for stored credentials, comma-separated for rotation. Keep it out of the DB backups.
    encryption_key: SecretStr | None = None
    # Proxy for integration targets whose `proxy` is `auto` (direct first, then this). Falls back to HTTPS_PROXY.
    integration_proxy: str | None = None
    # Microsoft Graph app registration for Outlook (device-code sign-in)
    graph_client_id: str | None = None
    graph_tenant: str = "organizations"
    # activity cache freshness: today's data is re-fetched after this many minutes
    activity_fresh_minutes: int = 30

    # DingTalk (M6). Internal enterprise app: AppKey / AppSecret; the robot code of an
    # internal app's robot is its AppKey unless configured otherwise.
    dingtalk_app_key: str | None = None
    dingtalk_app_secret: SecretStr | None = None
    dingtalk_robot_code: str | None = None
    dingtalk_root_dept_id: int = 1
    dingtalk_report_template: str = "日报"  # DingTalk log template used for one-way sync
    dingtalk_template_path: Path = Path("config/templates/dingtalk_report.md")
    dingtalk_mapping_path: Path = Path("config/templates/dingtalk_mapping.yaml")
    dingtalk_log_sync: bool = True  # sync confirmed reports to DingTalk logs (when DingTalk is configured)
    # public web address, used for links in DingTalk cards (e.g. https://dayline.example.com)
    public_url: str | None = None

    @property
    def dingtalk_enabled(self) -> bool:
        return bool(self.dingtalk_app_key and self.dingtalk_app_secret)

    # member tools (M7). The secret derives each tool's own token and signs the short-lived
    # Relay grants handed to tools per call; member tools are off while it is unset.
    tools_secret: SecretStr | None = None
    # how tool containers reach the API on the `tools` network (Relay callbacks)
    tools_api_url: str = "http://api:8000"
    tools_sync_seconds: int = 30  # how often API / worker pick up enabled or disabled tools
    tools_verify_minutes: int = 10  # worker: re-check every enabled tool's live definition
    tools_compose_file: Path = Path("docker-compose.tools.yml")
    tools_env_dir: Path = Path("deploy/tools")  # per-tool env files (tokens); keep out of git

    @property
    def tools_enabled(self) -> bool:
        return self.tools_secret is not None

    # storage limits
    activity_cache_ttl_days: int = 7
    trace_max_bytes: int = 64_000

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    def resolve(self, p: Path) -> Path:
        return p if p.is_absolute() else (self.home / p).resolve()

    @property
    def llm_config_path(self) -> Path:
        return self.resolve(self.llm_config)

    @property
    def skills_path(self) -> Path:
        return self.resolve(self.skills_dir)

    def check_required(self) -> None:
        """Fail fast at service start-up (called by the API/worker, not by the CLI)."""
        problems = []
        if self.env == "prod" and self.api_token is None:
            problems.append("DAYLINE_API_TOKEN must be set when DAYLINE_ENV=prod")
        if not self.llm_config_path.is_file():
            problems.append(f"LLM endpoint config not found: {self.llm_config_path}")
        if not (self.skills_path / "builtin").is_dir():
            problems.append(f"builtin skills directory not found: {self.skills_path / 'builtin'}")
        if problems:
            raise RuntimeError("invalid configuration:\n  - " + "\n  - ".join(problems))


@lru_cache
def get_settings() -> Settings:
    # Existing environment variables win over the file (containers get theirs from compose).
    load_dotenv(_find_home() / ".env", override=False)
    return Settings()
