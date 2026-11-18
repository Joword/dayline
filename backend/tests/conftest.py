from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from app.llm.adapters.base import AdapterDone
from app.llm.endpoints import LLMConfig
from app.llm.gateway import LLMGateway
from app.llm.types import LLMRequest, LLMResponse, Message, TextBlock, TextDelta, ToolCall, Usage

REPO = Path(__file__).resolve().parents[2]
SKILLS = REPO / "skills"
TEMPLATES = REPO / "tools" / "templates"


def resp(
    text: str = "",
    *,
    calls: list[tuple[str, dict]] | None = None,
    stop: str | None = None,
    model: str = "m-large",
    usage: tuple[int, int] = (100, 50),
) -> LLMResponse:
    content: list = []
    if text:
        content.append(TextBlock(text=text))
    for i, (name, inp) in enumerate(calls or []):
        content.append(ToolCall(id=f"call_{i}", name=name, input=inp))
    return LLMResponse(
        message=Message(role="assistant", content=content),
        stop_reason=stop or ("tool_use" if calls else "end_turn"),
        usage=Usage(input_tokens=usage[0], output_tokens=usage[1]),
        model=model,
    )


class FakeAdapter:
    """Scripted adapter. ``script[endpoint]`` is a queue of LLMResponse | Exception |
    callable(req) -> LLMResponse; for streams, a list of str / Exception / LLMResponse."""

    name = "anthropic"

    def __init__(
        self, script: dict[str, list[Any]] | None = None, streams: dict[str, list[list[Any]]] | None = None
    ):
        self.script = script or {}
        self.streams = streams or {}
        self.calls: list[tuple[str, str, LLMRequest]] = []

    async def complete(self, req: LLMRequest, ep, model: str) -> LLMResponse:
        self.calls.append((ep.name, model, req))
        queue = self.script.get(ep.name)
        if not queue:
            raise AssertionError(f"unexpected call to {ep.name}")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            item = item(req)
        return item.model_copy(deep=True)

    async def stream(self, req: LLMRequest, ep, model: str) -> AsyncIterator[TextDelta | AdapterDone]:
        self.calls.append((ep.name, model, req))
        queue = self.streams.get(ep.name)
        if not queue:
            raise AssertionError(f"unexpected stream to {ep.name}")
        for ev in queue.pop(0):
            if isinstance(ev, Exception):
                raise ev
            if isinstance(ev, str):
                yield TextDelta(text=ev)
            else:
                yield AdapterDone(ev.model_copy(deep=True))

    async def aclose(self) -> None:
        pass


def make_config(*names: str, **overrides: dict[str, Any]) -> LLMConfig:
    names = names or ("e1", "e2", "e3")
    eps = []
    for n in names:
        ep = {
            "name": n,
            "adapter": "anthropic",
            "api_key_env": "TEST_KEY",
            "models": {"small": "m-small", "large": "m-large"},
            "features": {"tool_calling": True, "json_schema": True, "streaming": True},
        }
        ep.update(overrides.get(n, {}))
        eps.append(ep)
    return LLMConfig.model_validate(
        {
            "endpoints": eps,
            "failover": {
                "order": list(names),
                "retry": 2,
                "circuit_breaker": {"failures": 3, "cooldown_s": 60},
            },
            "pricing": {"m-large": {"input": 2.0, "output": 10.0}, "m-small": {"input": 0.1, "output": 0.5}},
        }
    )


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture(autouse=True)
def _test_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_KEY", "sk-test")
    monkeypatch.setenv("DAYLINE_HOME", str(REPO))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture
def sleeps() -> list[float]:
    return []


def make_gateway(
    adapter: FakeAdapter, config: LLMConfig | None = None, sleeps: list[float] | None = None, clock=None
):
    async def fake_sleep(s: float) -> None:
        if sleeps is not None:
            sleeps.append(s)

    kwargs: dict[str, Any] = {"sleep": fake_sleep}
    if clock is not None:
        kwargs["clock"] = clock
    return LLMGateway(config or make_config(), {"anthropic": adapter}, **kwargs)


def req(**kw: Any) -> LLMRequest:
    return LLMRequest(messages=[Message.user("hi")], **kw)


class served:
    """Run an ASGI app with uvicorn on a free port inside the test's event loop."""

    def __init__(self, app: Any, *, lifespan: str = "auto"):
        import uvicorn

        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan=lifespan)
        self.server = uvicorn.Server(config)

    async def __aenter__(self) -> str:
        import asyncio

        self.task = asyncio.create_task(self.server.serve())
        for _ in range(250):
            if self.server.started:
                break
            await asyncio.sleep(0.02)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    async def __aexit__(self, *exc: Any) -> None:
        self.server.should_exit = True
        await self.task


def load_template(monkeypatch: pytest.MonkeyPatch, name: str, token: str):
    """Import tools/templates/python/server.py as the member tool ``name``."""
    import importlib.util
    import sys

    monkeypatch.setenv("DAYLINE_TOOL_NAME", name)
    monkeypatch.setenv("DAYLINE_TOOL_TOKEN", token)
    monkeypatch.delenv("DAYLINE_TOOL_DEV", raising=False)
    mod_name = f"dayline_tool_template_{name.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(mod_name, TEMPLATES / "python" / "server.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


# --- MongoDB-backed tests ---------------------------------------------------------
# A real MongoDB (replica set not required) at DAYLINE_TEST_MONGO_URI; tests skip
# when none is reachable.

import os  # noqa: E402
import uuid  # noqa: E402

from pymongo import AsyncMongoClient  # noqa: E402

TEST_MONGO_URI = os.environ.get("DAYLINE_TEST_MONGO_URI", "mongodb://127.0.0.1:27018/?directConnection=true")

# boss
# ├── lisi    (payments lead)  -> zhangsan, wangwu, zhaoliu
# └── qianqi  (risk lead)      -> sunba
ORG_ROWS = [
    {"id": "boss", "name": "赵总"},
    {"id": "lisi", "name": "李四", "manager": "boss", "org_unit": "支付组"},
    {"id": "qianqi", "name": "钱七", "manager": "boss", "org_unit": "风控组"},
    {"id": "zhangsan", "name": "张三", "manager": "lisi", "org_unit": "支付组"},
    {"id": "wangwu", "name": "王五", "manager": "lisi", "org_unit": "支付组"},
    {"id": "zhaoliu", "name": "赵六", "manager": "lisi", "org_unit": "支付组"},
    {"id": "sunba", "name": "孙八", "manager": "qianqi", "org_unit": "风控组"},
]


@pytest.fixture
async def mongo_db():
    from app.db.indexes import ensure_indexes

    client = AsyncMongoClient(TEST_MONGO_URI, serverSelectionTimeoutMS=1500, tz_aware=True)
    try:
        await client.admin.command("ping")
    except Exception:
        await client.close()
        pytest.skip(f"no test MongoDB at {TEST_MONGO_URI}")
    db = client[f"dayline_test_{uuid.uuid4().hex[:8]}"]
    await ensure_indexes(db)
    try:
        yield db
    finally:
        await client.drop_database(db.name)
        await client.close()


@pytest.fixture
async def org(mongo_db):
    """Repos + Relay over a seeded org."""
    from types import SimpleNamespace

    from app.db.repositories.misc import AuditRepo, ReviewsRepo
    from app.db.repositories.reports import ReportsRepo
    from app.db.repositories.users import UsersRepo
    from app.org.importer import apply_plan, build_plan
    from app.relay.service import Relay

    users = UsersRepo(mongo_db)
    await apply_plan(users, build_plan([dict(r) for r in ORG_ROWS]))
    reports, audit, reviews = ReportsRepo(mongo_db), AuditRepo(mongo_db), ReviewsRepo(mongo_db)
    return SimpleNamespace(
        db=mongo_db,
        users=users,
        reports=reports,
        audit=audit,
        reviews=reviews,
        relay=Relay(users, reports, audit),
    )


async def submit(reports, user_id: str, date: str, items: list[dict], *, confirm: bool = True) -> dict:
    out = {
        "kind": "individual",
        "date": date,
        "lang": "zh",
        "own_items": items,
        "plan_check": [],
        "questions": [],
    }
    doc = await reports.save_draft(
        user_id=user_id,
        date=date,
        lang="zh",
        raw_input={"narrative": "x"},
        output=out,
        rendered_md="md",
        skill_name="generate-daily",
        skill_version="1.1.0",
        skill_run_id=None,
    )
    return await reports.confirm(doc["id"]) if confirm else doc


def item(kind: str, text: str, **kw) -> dict:
    return {"kind": kind, "text": text, "source": "manual", **kw}
