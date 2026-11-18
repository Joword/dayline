"""Visibility policy, Relay (with MongoDB), consent and audit."""

from __future__ import annotations

import pytest

from app.org.importer import OrgImportError, apply_plan, build_plan
from app.relay.policy import Viewer, decide, relation_of
from app.relay.service import RelayForbidden, RelayNotFound
from tests.conftest import ORG_ROWS, item, submit

PLAN = {u.id: u for u in build_plan([dict(r) for r in ORG_ROWS]).users}
MANAGERS = {"boss", "lisi", "qianqi"}


def rel(viewer: str, target: str) -> str:
    return relation_of(
        PLAN[viewer], PLAN[target], viewer_manages=viewer in MANAGERS, target_manages=target in MANAGERS
    )


@pytest.mark.parametrize(
    ("viewer", "target", "relation", "access"),
    [
        ("zhangsan", "zhangsan", "self", "full"),
        ("lisi", "zhangsan", "direct_manager", "full"),
        ("boss", "lisi", "direct_manager", "full"),  # my manager sees my report incl. team summary
        ("boss", "zhangsan", "skip_level", "none"),  # ...but not my reports' individual reports
        ("qianqi", "lisi", "peer_manager", "dependencies"),
        ("qianqi", "zhangsan", "other", "none"),
        ("zhangsan", "wangwu", "other", "none"),  # peers who are not managers
        ("zhangsan", "lisi", "other", "none"),  # upward
    ],
)
def test_report_visibility_table(viewer, target, relation, access):
    r = rel(viewer, target)
    d = decide(Viewer(viewer), r, "report", target=PLAN[target])
    assert (r, d.access, d.allowed) == (relation, access, access != "none")


def test_activity_needs_consent_for_current_manager_only():
    from app.db.models import Consent

    z = PLAN["zhangsan"]
    assert not decide(Viewer("lisi"), "direct_manager", "activity", target=z, activity_scope="git").allowed
    z2 = z.model_copy(update={"consents": [Consent(scope="git", grantee_id="lisi")]})
    assert decide(Viewer("lisi"), "direct_manager", "activity", target=z2, activity_scope="git").allowed
    assert not decide(
        Viewer("lisi"), "direct_manager", "activity", target=z2, activity_scope="calendar"
    ).allowed
    old = z.model_copy(update={"consents": [Consent(scope="git", grantee_id="someone-else")]})
    assert not decide(Viewer("lisi"), "direct_manager", "activity", target=old, activity_scope="git").allowed
    assert not decide(Viewer("boss"), "skip_level", "activity", target=z2, activity_scope="git").allowed


def test_external_client_scopes():
    v = Viewer("lisi", client_id="other-app", scopes=frozenset({"read:activity"}))
    d = decide(v, "direct_manager", "report", target=PLAN["zhangsan"])
    assert not d.allowed and "lacks scope" in d.reason


# --- org import ------------------------------------------------------------------


def test_import_paths_and_levels():
    assert PLAN["zhangsan"].org_path == "/boss/lisi/zhangsan/" and PLAN["zhangsan"].level == 2
    assert PLAN["boss"].org_path == "/boss/" and PLAN["boss"].level == 0


@pytest.mark.parametrize(
    ("rows", "error"),
    [
        ([{"id": "a", "name": "A", "manager": "b"}, {"id": "b", "name": "B", "manager": "a"}], "cycle"),
        ([{"id": "a", "name": "A", "manager": "ghost"}], "not in the file"),
        ([{"id": "a", "name": "A"}, {"id": "a", "name": "A2"}], "duplicate id"),
        ([{"id": "a/b", "name": "A"}], "invalid id"),
        ([{"id": "a", "name": "A", "email": "x@c"}, {"id": "b", "name": "B", "email": "X@c"}], "email"),
    ],
)
def test_import_rejects_bad_files(rows, error):
    with pytest.raises(OrgImportError, match=error):
        build_plan(rows)


async def test_reimport_keeps_tokens_drops_stale_consents(org):
    from app.db.models import ApiToken, Consent

    await org.users.add_token("zhangsan", ApiToken(id="t1", hash="h"))
    await org.users.set_consent("zhangsan", Consent(scope="git", grantee_id="lisi"))
    moved = [dict(r) for r in ORG_ROWS]
    for r in moved:
        if r["id"] == "zhangsan":
            r["manager"] = "qianqi"
    res = await apply_plan(
        org.users, build_plan([r for r in moved if r["id"] != "sunba"]), deactivate_missing=True
    )
    z = await org.users.get("zhangsan")
    assert z.manager_id == "qianqi" and z.org_path == "/boss/qianqi/zhangsan/"
    assert [t.id for t in z.api_tokens] == ["t1"] and z.consents == []
    assert res.deactivated == ["sunba"] and await org.users.get("sunba") is None


# --- relay ------------------------------------------------------------------------


async def test_manager_sees_submitted_reports_only_and_access_is_audited(org):
    await submit(org.reports, "zhangsan", "2026-11-17", [item("done", "上线重试")])
    await submit(org.reports, "zhangsan", "2026-11-18", [item("done", "草稿")], confirm=False)
    lisi = await org.relay.viewer("lisi")
    v = await org.relay.get_report(lisi, "zhangsan", "2026-11-17")
    assert v.access == "full" and v.raw_input is None  # the narrative stays with the author
    with pytest.raises(RelayNotFound):
        await org.relay.get_report(lisi, "zhangsan", "2026-11-18")  # draft: invisible
    me = await org.relay.get_report(await org.relay.viewer("zhangsan"), "zhangsan", "2026-11-18")
    assert me.status == "draft" and me.raw_input == {"narrative": "x"}
    logs = await org.audit.recent(actor_id="lisi")
    assert {entry["action"] for entry in logs} == {"report.read"} and all(entry["allowed"] for entry in logs)


async def test_skip_level_and_strangers_are_denied_and_audited(org):
    await submit(org.reports, "zhangsan", "2026-11-17", [item("done", "x")])
    for who in ("boss", "wangwu", "qianqi"):
        with pytest.raises(RelayForbidden):
            await org.relay.get_report(await org.relay.viewer(who), "zhangsan", "2026-11-17")
    denied = [e for e in await org.audit.recent() if not e["allowed"]]
    assert {e["actor_id"] for e in denied} == {"boss", "wangwu", "qianqi"}


async def test_peer_manager_sees_only_cross_team_items(org):
    await submit(
        org.reports,
        "lisi",
        "2026-11-17",
        [item("done", "内部重构"), item("blocker", "等风控接口文档", cross_team=True, need_from="风控组")],
    )
    v = await org.relay.get_report(await org.relay.viewer("qianqi"), "lisi", "2026-11-17")
    assert v.access == "dependencies"
    assert [i["text"] for i in v.own_items] == ["等风控接口文档"]
    assert "内部重构" not in v.rendered_md


async def test_team_snapshot_lists_missing_members(org):
    await submit(org.reports, "zhangsan", "2026-11-17", [item("done", "a")])
    await submit(org.reports, "wangwu", "2026-11-17", [item("done", "b")], confirm=False)
    snap = await org.relay.team_snapshot(await org.relay.viewer("lisi"), "2026-11-17")
    assert [m.user.id for m in snap.submitted] == ["zhangsan"]
    assert {u.id for u in snap.missing} == {"wangwu", "zhaoliu"}  # a draft counts as not submitted
    with pytest.raises(RelayForbidden):
        await org.relay.team_snapshot(await org.relay.viewer("zhangsan"), "2026-11-17")


async def test_consent_is_bound_to_the_current_manager(org):
    z = await org.relay.viewer("zhangsan")
    c = await org.relay.grant_consent(z, "git")
    assert c.grantee_id == "lisi"
    assert [x.scope for x in await org.relay.consents(z)] == ["git"]
    d, _ = await org.relay.check(
        await org.relay.viewer("lisi"), "zhangsan", "activity", action="activity.read", activity_scope="git"
    )
    assert d.allowed
    assert await org.relay.revoke_consent(z, "git")
    with pytest.raises(RelayForbidden):
        await org.relay.check(
            await org.relay.viewer("lisi"),
            "zhangsan",
            "activity",
            action="activity.read",
            activity_scope="git",
        )
    with pytest.raises(RelayForbidden):
        await org.relay.grant_consent(await org.relay.viewer("boss"), "git")  # no manager


async def test_reports_versioning_and_confirm_rules(org):
    from app.db.repositories.reports import ReportConflict

    d1 = await submit(org.reports, "zhangsan", "2026-11-17", [item("done", "a")], confirm=False)
    d2 = await submit(org.reports, "zhangsan", "2026-11-17", [item("done", "b")], confirm=False)
    assert (d1["version"], d2["version"]) == (1, 2)
    with pytest.raises(ReportConflict):
        await org.reports.confirm(d1["id"])  # outdated version
    assert (await org.reports.confirm(d2["id"]))["status"] == "confirmed"
    assert await org.reports.confirm(d2["id"]) is None  # already confirmed
    assert await org.reports.get("not-an-object-id") is None
