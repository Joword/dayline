"""Visibility policy (plan §3.3). Pure functions: no I/O, exhaustively testable.

| viewer               | report                                  | activity (git/calendar)          |
|----------------------|-----------------------------------------|----------------------------------|
| self                 | full                                    | full                             |
| direct manager       | full (submitted reports only)           | only with the member's consent   |
| skip-level manager   | none (three-layer model)                | none                             |
| peer manager         | cross-team dependency items only        | none                             |
| anyone else          | none                                    | none                             |

System roles (admin / developer) grant no data access.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.db.models import UserDoc

Relation = Literal["self", "direct_manager", "skip_level", "peer_manager", "other"]
Access = Literal["full", "dependencies", "none"]
Resource = Literal["report", "activity"]

# Scope a client must hold per resource. The internal app holds "*"; external
# clients of the future relay-mcp service (E1) get narrower grants.
RESOURCE_SCOPES: dict[str, str] = {"report": "read:reports", "activity": "read:activity"}


@dataclass(frozen=True)
class Viewer:
    user_id: str
    client_id: str = "dayline"
    scopes: frozenset[str] = field(default_factory=lambda: frozenset({"*"}))

    def has_scope(self, scope: str) -> bool:
        return "*" in self.scopes or scope in self.scopes


@dataclass(frozen=True)
class Decision:
    allowed: bool
    access: Access
    relation: Relation
    reason: str


def relation_of(viewer: UserDoc, target: UserDoc, *, viewer_manages: bool, target_manages: bool) -> Relation:
    if viewer.id == target.id:
        return "self"
    if target.manager_id == viewer.id:
        return "direct_manager"
    if target.org_path.startswith(viewer.org_path):
        return "skip_level"
    if viewer_manages and target_manages and viewer.manager_id and viewer.manager_id == target.manager_id:
        return "peer_manager"
    return "other"


def decide(
    viewer: Viewer,
    relation: Relation,
    resource: Resource,
    *,
    target: UserDoc | None = None,
    activity_scope: str | None = None,
) -> Decision:
    def deny(reason: str) -> Decision:
        return Decision(False, "none", relation, reason)

    if not viewer.has_scope(RESOURCE_SCOPES[resource]):
        return deny(f"client '{viewer.client_id}' lacks scope {RESOURCE_SCOPES[resource]}")
    if relation == "self":
        return Decision(True, "full", relation, "own data")

    if resource == "report":
        if relation == "direct_manager":
            return Decision(True, "full", relation, "direct manager")
        if relation == "peer_manager":
            return Decision(True, "dependencies", relation, "peer manager: cross-team dependencies only")
        if relation == "skip_level":
            return deny("skip-level access is outside the three-layer model")
        return deny("no reporting relationship")

    # activity: raw git/calendar data used for cross-checking
    if relation != "direct_manager":
        return deny("activity data is visible to the person and, with consent, their direct manager")
    granted = target is not None and any(
        c.scope == activity_scope and c.grantee_id == viewer.user_id for c in target.consents
    )
    if granted:
        return Decision(True, "full", relation, f"member consented to {activity_scope}")
    return deny(f"member has not consented to share {activity_scope} data with their manager")
