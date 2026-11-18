from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.bootstrap import Services
from app.core.config import Settings
from app.llm.types import EndpointLLMError
from app.skills.router import SkillRouter
from tests.conftest import FakeAdapter, resp
from tests.test_runner import GOOD, make_runner


def client(adapter: FakeAdapter, **settings_kw) -> TestClient:
    runner, _ = make_runner(adapter)
    settings = Settings(**settings_kw)
    services = Services(
        settings=settings,
        gateway=runner.gateway,
        tools=runner.tools,
        rules=runner.rules,
        registry=runner.registry,
        router=SkillRouter(runner.registry),
        runner=runner,
    )
    return TestClient(create_app(settings, services=services))


def test_health_and_readiness():
    with client(FakeAdapter()) as c:
        assert c.get("/healthz").json() == {"status": "ok"}
        r = c.get("/readyz")
        assert r.status_code == 503  # no Mongo in this test
        assert r.json()["mongo"] == "unreachable" and r.json()["llm_endpoints"]["e1"] == "ready"
        assert r.headers["x-request-id"]


def test_list_and_get_skills_expose_form_metadata():
    with client(FakeAdapter()) as c:
        skills = {s["name"]: s for s in c.get("/skills").json()}
        gd = skills["generate-daily"]
        assert gd["template"] == "daily_report" and "/gen" in gd["commands"]
        assert [i["name"] for i in gd["inputs"]] == ["date", "narrative", "answers"]
        assert c.get("/skills/review-member").json()["layer"] == "builtin"
        assert c.get("/skills/nope").status_code == 404


def test_run_skill():
    a = FakeAdapter({"e1": [resp(json.dumps(GOOD))]})
    with client(a) as c:
        r = c.post(
            "/skills/generate-daily/run",
            json={"inputs": {"narrative": "上线了重试", "date": "2026-11-17"}, "lang": "zh"},
            headers={"x-dayline-user": "alice", "x-request-id": "req-1"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok" and body["output"]["own_items"][0]["source"] == "manual"
    assert r.headers["x-request-id"] == "req-1"


@pytest.mark.parametrize(
    ("payload", "code"),
    [({"inputs": {}}, 422), ({"inputs": {"narrative": "x", "bogus": 1}}, 422)],
)
def test_run_skill_input_errors(payload, code):
    with client(FakeAdapter()) as c:
        assert c.post("/skills/generate-daily/run", json=payload).status_code == code


def test_run_skill_llm_unavailable_is_503():
    a = FakeAdapter({n: [EndpointLLMError("401")] for n in ("e1", "e2", "e3")})
    with client(a) as c:
        r = c.post("/skills/review-member/run", json={"inputs": {"report": "x"}})
    assert r.status_code == 503 and r.json()["detail"]["run_id"]


def test_api_token_required_when_configured():
    with client(FakeAdapter(), api_token="s3cret") as c:
        assert c.get("/healthz").status_code == 200  # probes stay open
        assert c.get("/skills").status_code == 401
        assert c.get("/skills", headers={"authorization": "Bearer wrong"}).status_code == 401
        assert c.get("/skills", headers={"authorization": "Bearer s3cret"}).status_code == 200
