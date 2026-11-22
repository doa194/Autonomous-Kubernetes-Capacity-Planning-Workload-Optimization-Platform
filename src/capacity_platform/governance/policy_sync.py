"""Governance is the authority for which optimization policy revision is current (S19 §5).

At start-up, governance resolves the effective policy for each optimizable workload from the
versioned policy files mounted into its Pod and records the digests. The capacity engine
resolves the same files independently and refuses to analyse when its digest disagrees, so
a stale or tampered capacity copy cannot silently drive decisions.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import yaml

from capacity_platform.capacity.policy.model import load_policy, resolve_effective_policy
from capacity_platform.contracts.workloads import CATALOG
from capacity_platform.governance.store import GovernanceStore
from capacity_platform.shared.db import Connection, transaction


def sync_effective_policies(
    connect: Callable[[], Connection],
    policy_file: Path,
    governance_policy_file: Path,
    cluster_id: str = "capacity-lab",
) -> dict[str, str]:
    doc = load_policy(policy_file)
    gov = yaml.safe_load(governance_policy_file.read_text(encoding="utf-8"))
    if gov.get("schemaVersion") != "governance-policy/v1alpha1":
        raise ValueError("unsupported governance policy schema")
    allowed = gov.get("deterministicOnlyExecutionAllowed") or {}
    digests: dict[str, str] = {}
    with connect() as conn, transaction(conn):
        store = GovernanceStore(conn)
        for entry in CATALOG.values():
            if entry.namespace not in doc.allowedNamespaces:
                continue
            eff = resolve_effective_policy(
                doc,
                cluster_id=cluster_id,
                namespace=entry.namespace,
                workload_id=entry.workload_id,
                criticality=entry.criticality,
            )
            store.set_effective_policy(
                entry.workload_id, eff.policyId, eff.revision, eff.digest, bool(allowed.get(entry.workload_id, False))
            )
            digests[entry.workload_id] = eff.digest
    return digests
