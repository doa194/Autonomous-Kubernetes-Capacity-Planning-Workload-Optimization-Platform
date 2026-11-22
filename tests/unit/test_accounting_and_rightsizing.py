"""S11 scheduler accounting, quantity parsing and S20 generation properties."""

import pytest
from tests import factories as f

from capacity_platform.capacity.rightsizing.generator import generate_candidates
from capacity_platform.contracts.enums import ResourceField
from capacity_platform.contracts.units import (
    MIB,
    QuantityError,
    bytes_to_quantity,
    cpu_to_millicores,
    memory_to_bytes,
    millicores_to_quantity,
)
from capacity_platform.observer.normalize import (
    effective_pod_request,
    holds_node_reservation,
    node_accounting,
    scheduling_status,
)


@pytest.mark.parametrize(("text", "millis"), [("250m", 250), ("1", 1000), ("0.5", 500), ("1500m", 1500)])
def test_cpu_quantities_parse_to_exact_millicores(text, millis):
    assert cpu_to_millicores(text) == millis


@pytest.mark.parametrize(("text", "num"), [("256Mi", 256 * MIB), ("1Gi", 1024 * MIB), ("1G", 10**9), ("1500", 1500)])
def test_memory_quantities_distinguish_binary_and_decimal(text, num):
    assert memory_to_bytes(text) == num


@pytest.mark.parametrize("bad", ["0.0001", "abc", "-1", "1.5.2"])
def test_invalid_or_sub_millicore_cpu_is_rejected(bad):
    with pytest.raises(QuantityError):
        cpu_to_millicores(bad)


def test_quantity_rendering_round_trips():
    assert millicores_to_quantity(2000) == "2" and millicores_to_quantity(150) == "150m"
    assert bytes_to_quantity(256 * MIB) == "256Mi"
    assert memory_to_bytes(bytes_to_quantity(123_456_789)) == 123_456_789


def _c(name, cpu, mem, **extra):
    return {"name": name, "resources": {"requests": {"cpu": cpu, "memory": mem}}, **extra}


def test_effective_request_uses_init_max_and_sidecars():
    spec = {
        "initContainers": [
            _c("sidecar", "100m", "64Mi", restartPolicy="Always"),
            _c("migrate", "500m", "128Mi"),  # runs while the sidecar is already up
        ],
        "containers": [_c("app", "200m", "256Mi")],
        "overhead": {"cpu": "10m", "memory": "8Mi"},
    }
    eff = effective_pod_request(spec)
    # main = app + sidecar = 300m/320Mi ; init peak = 500m+100m / 128Mi+64Mi = 600m/192Mi
    assert eff.vector.cpu_m == 600 + 10
    assert eff.vector.memory_bytes == 320 * MIB + 8 * MIB


def test_extended_resources_are_flagged_not_dropped():
    spec = {"containers": [{"name": "app", "resources": {"requests": {"cpu": "1", "example.com/fpga": "1"}}}]}
    assert effective_pod_request(spec).unsupported_dimensions == ("example.com/fpga",)


def test_terminating_pod_still_holds_reservation_and_pending_pod_does_not():
    running = {"metadata": {"deletionTimestamp": "x"}, "spec": {"nodeName": "n1"}, "status": {"phase": "Running"}}
    pending = {"metadata": {}, "spec": {}, "status": {"phase": "Pending"}}
    assert holds_node_reservation(running) and scheduling_status(running) == "Terminating"
    assert not holds_node_reservation(pending) and scheduling_status(pending) == "UnassignedPending"


def test_node_accounting_keeps_negative_residual_visible():
    node = {
        "metadata": {"name": "n1", "uid": "u"},
        "spec": {},
        "status": {
            "allocatable": {"cpu": "1", "memory": "1Gi", "pods": "10"},
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }
    pods = [
        {
            "metadata": {},
            "spec": {"nodeName": "n1", "containers": [_c("a", "800m", "512Mi")]},
            "status": {"phase": "Running"},
        }
        for _ in range(2)
    ]
    acct = node_accounting(node, pods)
    assert acct.unrequested.cpu_m == -600
    assert acct.free_pod_slots == 8


def test_under_reserved_workload_gets_increase_candidates():
    ev = f.good_evidence("w01-latency-api", cpu_p95=260.0, forecast_cpu_upper=280.0)
    result = generate_candidates(f.target_state("w01-latency-api"), ev, f.policy("w01-latency-api"))
    cpu = [c for c in result.candidates if ResourceField.CPU_REQUEST in c.changes()]
    assert cpu and all(c.reservation_delta()["cpu_m"] > 0 for c in cpu)


def test_missing_cpu_evidence_is_a_refusal_not_a_fabricated_candidate():
    ev = f.good_evidence("w04-cpu-worker", cpu=None)
    result = generate_candidates(f.target_state("w04-cpu-worker"), ev, f.policy("w04-cpu-worker"))
    assert not any(ResourceField.CPU_REQUEST in c.changes() for c in result.candidates)
    assert any(
        r.field == ResourceField.CPU_REQUEST and r.reasonCode == "coverage_insufficient" for r in result.refusals
    )


def test_candidate_count_is_bounded_and_ordering_stable():
    ev = f.good_evidence("w04-cpu-worker", cpu_p95=40.0)
    a = generate_candidates(f.target_state("w04-cpu-worker"), ev, f.policy("w04-cpu-worker"))
    b = generate_candidates(f.target_state("w04-cpu-worker"), ev, f.policy("w04-cpu-worker"))
    assert len(a.candidates) <= f.policy("w04-cpu-worker").maxCandidates
    assert [c.candidateDigest for c in a.candidates] == [c.candidateDigest for c in b.candidates]
