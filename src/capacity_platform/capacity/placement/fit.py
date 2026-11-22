"""S21 scheduler-aware node fit and shape-specific fragmentation.

This is a transparent approximation of kube-scheduler filtering for a documented subset of
constraints, not a scheduler. It never mutates Pods. Per node and per resource:

    residual[n, r] = allocatable[n, r] - sum(effective requests of Pods assigned to n)

A Pod fits a node only if *every* dimension (CPU, memory, Pod slots) and every supported
hard constraint passes; one node's spare CPU cannot cover another node's memory shortage.
Any required constraint outside the supported subset makes the answer `indeterminate`,
never an optimistic "fits" (S21-FR-007).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

SUPPORTED_OPERATORS = {"In", "NotIn", "Exists", "DoesNotExist", "Gt", "Lt"}
HARD_TAINT_EFFECTS = {"NoSchedule", "NoExecute"}
PLACEMENT_REVISION = "s21-fit-v1"


@dataclass(frozen=True)
class NodeState:
    name: str
    uid: str
    ready: bool
    unschedulable: bool
    labels: dict[str, str]
    taints: tuple[tuple[str, str, str], ...]  # (key, value, effect)
    alloc_cpu_m: int
    alloc_memory_bytes: int
    alloc_pods: int
    assigned_cpu_m: int
    assigned_memory_bytes: int
    assigned_pods: int

    @property
    def residual_cpu_m(self) -> int:
        return self.alloc_cpu_m - self.assigned_cpu_m

    @property
    def residual_memory_bytes(self) -> int:
        return self.alloc_memory_bytes - self.assigned_memory_bytes

    @property
    def residual_pods(self) -> int:
        return self.alloc_pods - self.assigned_pods

    def add(self, cpu_m: int, memory_bytes: int, pods: int = 1) -> NodeState:
        return replace(
            self,
            assigned_cpu_m=self.assigned_cpu_m + cpu_m,
            assigned_memory_bytes=self.assigned_memory_bytes + memory_bytes,
            assigned_pods=self.assigned_pods + pods,
        )


@dataclass(frozen=True)
class PodShape:
    cpu_m: int
    memory_bytes: int
    node_selector: dict[str, str] = field(default_factory=dict)
    required_node_affinity: tuple = ()  # tuple of terms; term = tuple of expression dicts
    tolerations: tuple = ()
    unsupported: tuple[str, ...] = ()


@dataclass(frozen=True)
class NodeFit:
    node: str
    uid: str
    fits: bool
    reasons: tuple[str, ...]
    unknown: tuple[str, ...]


@dataclass(frozen=True)
class FitResult:
    status: str  # feasible | infeasible | indeterminate
    per_node: tuple[NodeFit, ...]

    @property
    def fit_capable_nodes(self) -> int:
        return sum(1 for n in self.per_node if n.fits)


def _tolerates(tolerations, key: str, value: str, effect: str) -> bool:
    for tol in tolerations:
        op = tol.get("operator", "Equal")
        tkey = tol.get("key", "")
        if tkey and tkey != key:
            continue
        if not tkey and op != "Exists":
            continue
        if op == "Equal" and tol.get("value", "") != value:
            continue
        if tol.get("effect") and tol.get("effect") != effect:
            continue
        return True
    return False


def _expression(labels: dict[str, str], expr: dict) -> bool | None:
    op, key, values = expr.get("operator"), str(expr.get("key") or ""), expr.get("values") or []
    if op not in SUPPORTED_OPERATORS:
        return None
    present = key in labels
    if op == "In":
        return present and labels[key] in values
    if op == "NotIn":
        return not present or labels[key] not in values
    if op == "Exists":
        return present
    if op == "DoesNotExist":
        return not present
    try:
        number = int(labels[key]) if present else None
        bound = int(values[0])
    except (ValueError, IndexError):
        return False
    if number is None:
        return False
    return number > bound if op == "Gt" else number < bound


def node_fit(node: NodeState, shape: PodShape) -> NodeFit:
    reasons: list[str] = []
    unknown = list(shape.unsupported)
    if not node.ready:
        reasons.append("node_not_ready")
    if node.unschedulable:
        reasons.append("node_unschedulable")
    for key, value in shape.node_selector.items():
        if node.labels.get(key) != value:
            reasons.append("node_selector_mismatch")
            break
    if shape.required_node_affinity:
        term_results: list[bool | None] = []
        for term in shape.required_node_affinity:
            results = [_expression(node.labels, expr) for expr in term]
            if any(r is None for r in results):
                unknown.append("node_affinity_operator_unsupported")
                term_results.append(None)
            else:
                term_results.append(all(results))
        if not any(r for r in term_results if r is not None) and None not in term_results:
            reasons.append("node_affinity_mismatch")
    for key, value, effect in node.taints:
        if effect in HARD_TAINT_EFFECTS and not _tolerates(shape.tolerations, key, value, effect):
            reasons.append("taint_not_tolerated")
            break
    if node.residual_cpu_m < shape.cpu_m:
        reasons.append("insufficient_cpu")
    if node.residual_memory_bytes < shape.memory_bytes:
        reasons.append("insufficient_memory")
    if node.residual_pods < 1:
        reasons.append("insufficient_pods")
    return NodeFit(node.name, node.uid, not reasons and not unknown, tuple(reasons), tuple(sorted(set(unknown))))


def fit_single(nodes: list[NodeState], shape: PodShape) -> FitResult:
    per_node = tuple(node_fit(n, shape) for n in sorted(nodes, key=lambda n: n.name))
    if any(n.fits for n in per_node):
        return FitResult("feasible", per_node)
    # Indeterminate if some node failed only because of an unsupported constraint.
    if any(n.unknown and not n.reasons for n in per_node):
        return FitResult("indeterminate", per_node)
    return FitResult("infeasible", per_node)


@dataclass(frozen=True)
class MultiPlacement:
    status: str  # feasible_under_heuristic | not_found_under_heuristic | proven_infeasible | indeterminate
    assignment: tuple[str, ...]
    exhaustive: bool
    branches_explored: int


def place_many(nodes: list[NodeState], shapes: list[PodShape], branch_budget: int = 5000) -> MultiPlacement:
    """Bounded backtracking placement of several hypothetical Pods.

    A failure inside the budget is a proof of infeasibility for the supported model; running
    out of budget is only "not found", never a hard infeasible verdict (S21 §3).
    """
    if any(s.unsupported for s in shapes):
        return MultiPlacement("indeterminate", (), False, 0)
    order = sorted(range(len(shapes)), key=lambda i: (-shapes[i].cpu_m, -shapes[i].memory_bytes, i))
    state = {n.name: n for n in sorted(nodes, key=lambda n: n.name)}
    assignment: dict[int, str] = {}
    explored = 0
    exhausted = False

    def search(k: int) -> bool:
        nonlocal explored, exhausted
        if k == len(order):
            return True
        idx = order[k]
        for name in list(state):
            explored += 1
            if explored > branch_budget:
                exhausted = True
                return False
            if node_fit(state[name], shapes[idx]).fits:
                previous = state[name]
                state[name] = previous.add(shapes[idx].cpu_m, shapes[idx].memory_bytes)
                assignment[idx] = name
                if search(k + 1):
                    return True
                state[name] = previous
                del assignment[idx]
        return False

    if search(0):
        return MultiPlacement(
            "feasible_under_heuristic", tuple(assignment[i] for i in range(len(shapes))), not exhausted, explored
        )
    if exhausted:
        return MultiPlacement("not_found_under_heuristic", (), False, explored)
    return MultiPlacement("proven_infeasible", (), True, explored)


@dataclass(frozen=True)
class FragmentationReport:
    """Shape-specific: "stranded" only means unusable for *this* Pod shape."""

    aggregate_free_cpu_m: int
    aggregate_free_memory_bytes: int
    eligible_nodes: int
    fit_capable_nodes: int
    stranded_cpu_m_for_shape: int
    stranded_memory_bytes_for_shape: int
    aggregate_would_suggest_fit: bool


def fragmentation(nodes: list[NodeState], shape: PodShape) -> FragmentationReport:
    result = fit_single(nodes, shape)
    resource_reasons = {"insufficient_cpu", "insufficient_memory", "insufficient_pods"}
    eligible = [
        (n, f)
        for n, f in zip(sorted(nodes, key=lambda n: n.name), result.per_node, strict=True)
        if not (set(f.reasons) - resource_reasons) and not f.unknown
    ]
    free_cpu = sum(max(n.residual_cpu_m, 0) for n, _ in eligible)
    free_mem = sum(max(n.residual_memory_bytes, 0) for n, _ in eligible)
    stranded_cpu = sum(max(n.residual_cpu_m, 0) for n, f in eligible if not f.fits)
    stranded_mem = sum(max(n.residual_memory_bytes, 0) for n, f in eligible if not f.fits)
    return FragmentationReport(
        aggregate_free_cpu_m=free_cpu,
        aggregate_free_memory_bytes=free_mem,
        eligible_nodes=len(eligible),
        fit_capable_nodes=result.fit_capable_nodes,
        stranded_cpu_m_for_shape=stranded_cpu,
        stranded_memory_bytes_for_shape=stranded_mem,
        aggregate_would_suggest_fit=free_cpu >= shape.cpu_m and free_mem >= shape.memory_bytes,
    )
