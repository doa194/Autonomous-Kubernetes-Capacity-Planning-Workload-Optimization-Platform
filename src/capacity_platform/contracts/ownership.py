"""GitOps vs direct ownership detection shared by analysis (S20) and execution (S25).

Ownership is decided from several signals: Flux/Argo labels or annotations, the
managedFields managers, and the platform's own `capacity.lab/ownership` declaration. When
signals disagree the answer is `ambiguous`, which every consumer treats as a hard refusal.
"""

from __future__ import annotations

GITOPS_MARKERS = (
    "kustomize.toolkit.fluxcd.io/name",
    "helm.toolkit.fluxcd.io/name",
    "argocd.argoproj.io/instance",
)
GITOPS_MANAGERS = {"kustomize-controller", "helm-controller", "argocd-controller"}


def detect_ownership(obj: dict) -> str:
    meta = obj.get("metadata", {})
    labels = {**(meta.get("labels") or {}), **(meta.get("annotations") or {})}
    managers = {m.get("manager") for m in meta.get("managedFields") or []}
    gitops = any(k in labels for k in GITOPS_MARKERS) or bool(managers & GITOPS_MANAGERS)
    declared = labels.get("capacity.lab/ownership")
    if gitops and declared == "direct":
        return "ambiguous"  # two signals disagree: fail closed
    if gitops:
        return "gitops_managed"
    return "direct_managed" if declared in (None, "direct") else "ambiguous"
