"""ResourceCandidate v1alpha1 (S20 §4): an immutable proposed field change.

A candidate is a capacity-owned analytical proposal. It is not a plan, not an approval,
and not directly executable: S24 must issue its own plan revision and S25 builds the
real patch from that plan. `path` is an allowlisted logical selector, never JSONPatch.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from capacity_platform.contracts.canonical import digest
from capacity_platform.contracts.enums import Horizon, ResourceField
from capacity_platform.contracts.identity import TargetRef

FIELD_PATHS = {
    ResourceField.CPU_REQUEST: "spec.template.spec.containers[name={c}].resources.requests.cpu",
    ResourceField.MEMORY_REQUEST: "spec.template.spec.containers[name={c}].resources.requests.memory",
    ResourceField.CPU_LIMIT: "spec.template.spec.containers[name={c}].resources.limits.cpu",
    ResourceField.MEMORY_LIMIT: "spec.template.spec.containers[name={c}].resources.limits.memory",
}
UNITS = {
    ResourceField.CPU_REQUEST: "millicores",
    ResourceField.CPU_LIMIT: "millicores",
    ResourceField.MEMORY_REQUEST: "bytes",
    ResourceField.MEMORY_LIMIT: "bytes",
}


class FieldChange(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    field: ResourceField
    path: str
    before: int | None  # None = field absent in the live template
    after: int | None
    unit: str

    @property
    def is_decrease(self) -> bool:
        if self.before is None or self.after is None:
            # Adding a limit where none existed caps usage: treat as a risk-bearing decrease.
            return self.before is None and self.after is not None and self.field.endswith("limit")
        return self.after < self.before

    @property
    def is_increase(self) -> bool:
        return self.before is not None and self.after is not None and self.after > self.before


class CandidateSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    inventorySnapshotRef: str
    observationRefs: list[str]
    forecastRefs: list[str]
    effectivePolicyDigest: str
    evidenceManifestDigest: str


class ResourceCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schemaVersion: str = "resource-candidate/v1alpha1"
    target: TargetRef
    mutationUnit: str = "single_controller"
    fields: tuple[FieldChange, ...] = Field(min_length=1)
    source: CandidateSource
    horizon: Horizon
    strategy: str
    basis: list[str]
    assumptions: list[str]
    candidateDigest: str = ""

    def finalize(self) -> ResourceCandidate:
        ordered = tuple(sorted(self.fields, key=lambda f: f.field.value))
        base = self.model_copy(update={"fields": ordered})
        return base.model_copy(update={"candidateDigest": digest(base, exclude=frozenset({"candidateDigest"}))})

    def changes(self) -> dict[ResourceField, int | None]:
        return {f.field: f.after for f in self.fields}

    def reservation_delta(self) -> dict[str, int]:
        """Per-Pod change in scheduler reservation. Not measured consumption saving."""
        out = {"cpu_m": 0, "memory_bytes": 0}
        for f in self.fields:
            if f.field == ResourceField.CPU_REQUEST and f.before is not None and f.after is not None:
                out["cpu_m"] = f.after - f.before
            if f.field == ResourceField.MEMORY_REQUEST and f.before is not None and f.after is not None:
                out["memory_bytes"] = f.after - f.before
        return out


def make_change(field: ResourceField, container: str, before: int | None, after: int | None) -> FieldChange:
    return FieldChange(
        field=field, path=FIELD_PATHS[field].format(c=container), before=before, after=after, unit=UNITS[field]
    )
