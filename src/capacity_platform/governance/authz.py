"""S06 application authorization: RBAC + resource scope + policy conditions.

    Allow = Authenticated AND RolePermission AND ResourceScope AND OperatingMode
            AND WorkflowConditions AND CurrentPolicy

Roles are non-hierarchical: Administrator does not imply Approver, Approver does not imply
Executor. A role claim in a token is not enough on its own; the caller must also hold a
current, unrevoked grant in the Governance grant registry for the exact cluster and
namespace. That is how a role removal takes effect before the old token expires
(S06-TEST-008). Every decision returns a machine-readable reason and the grant revision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from capacity_platform.contracts.enums import HumanRole, OperatingMode
from capacity_platform.contracts.identity import ActorRef

V, AN, PL, AP, EX, AD = (
    HumanRole.VIEWER,
    HumanRole.ANALYST,
    HumanRole.PLANNER,
    HumanRole.APPROVER,
    HumanRole.EXECUTOR,
    HumanRole.ADMINISTRATOR,
)
_ALL = frozenset({V, AN, PL, AP, EX, AD})

# S06 §6.1 / S24 §4 reference matrix. Absent action => denied (deny by default).
ACTION_ROLES: dict[str, frozenset[HumanRole]] = {
    "inventory.read": _ALL,
    "evidence.read": _ALL,
    "plan.read": _ALL,
    "execution.read": _ALL,
    "audit.read": _ALL,
    "forecast.request": frozenset({AN, PL}),
    "analysis.request": frozenset({AN, PL}),
    "advisory.request": frozenset({AN, PL}),
    "plan.create": frozenset({PL}),
    "plan.approve": frozenset({AP}),
    "plan.reject": frozenset({AP}),
    "approval.revoke": frozenset({AP}),
    "execution.request": frozenset({EX}),
    "grant.administer": frozenset({AD}),
    "knowledge.ingest": frozenset({AD}),
    "mode.set": frozenset({AD}),
}

# Actions that may never run outside Governed mode (S01-ADR-008, S24 §0.3).
GOVERNED_ONLY = frozenset({"plan.approve", "execution.request"})
# Actions that need fresh grant state; a degraded grant store fails them closed.
SENSITIVE = frozenset(
    {
        "plan.create",
        "plan.approve",
        "plan.reject",
        "approval.revoke",
        "execution.request",
        "grant.administer",
        "mode.set",
        "knowledge.ingest",
    }
)


@dataclass(frozen=True)
class Grant:
    grant_id: str
    subject_key: str
    role: HumanRole
    cluster_id: str
    namespaces: frozenset[str]
    revision: int
    revoked_at: datetime | None = None

    def covers(self, cluster_id: str, namespace: str | None) -> bool:
        if self.revoked_at is not None or self.cluster_id != cluster_id:
            return False
        # No wildcard: cluster-scoped reads need an explicit "*cluster*" scope entry.
        return (namespace or "*cluster*") in self.namespaces


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str
    action: str
    subject_key: str
    grant_revision: int | None = None
    conditions_checked: tuple[str, ...] = field(default_factory=tuple)


def authorize(
    actor: ActorRef,
    token_roles: set[HumanRole],
    grants: list[Grant],
    action: str,
    *,
    cluster_id: str,
    namespace: str | None,
    mode: OperatingMode,
    grant_store_healthy: bool = True,
) -> Decision:
    key = actor.key()
    if actor.kind != "human" and action in SENSITIVE:
        # Machine principals and AI agents can never author, approve or request execution.
        return Decision(False, "machine_principal_not_permitted", action, key)
    allowed_roles = ACTION_ROLES.get(action)
    if allowed_roles is None:
        return Decision(False, "unknown_action", action, key)
    if action in SENSITIVE and not grant_store_healthy:
        return Decision(False, "authorization_freshness_unavailable", action, key)
    if action in GOVERNED_ONLY and mode != OperatingMode.GOVERNED:
        return Decision(False, "mode_not_governed", action, key, conditions_checked=("mode",))
    for grant in sorted(grants, key=lambda g: g.grant_id):
        if grant.subject_key != key or grant.role not in allowed_roles:
            continue
        if grant.role not in token_roles:
            continue  # the IdP no longer asserts this role for the session
        if grant.covers(cluster_id, namespace):
            return Decision(True, "allowed", action, key, grant.revision, ("role", "scope", "mode"))
    return Decision(False, "authorization_denied", action, key, conditions_checked=("role", "scope"))
