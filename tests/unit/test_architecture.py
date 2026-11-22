"""Architecture rules that keep authority where S02/S09/S25 put it.

The six services share one Python package and one image, so nothing at runtime stops one
service's code from importing another's. These checks make the important boundaries explicit:
* only the observer (read) and executor (governed write) talk to the Kubernetes API;
* the AI, API, capacity and governance code never imports executor code, so no path other
  than the executor can apply a change;
* contracts stay pure data (no web, database or Kubernetes dependencies);
* exactly six platform Deployments exist.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "capacity_platform"

FORBIDDEN = {
    "ai": {"capacity_platform.executor", "kubernetes"},
    "api": {"capacity_platform.executor", "kubernetes"},
    "capacity": {"capacity_platform.executor", "capacity_platform.ai", "kubernetes"},
    "governance": {"capacity_platform.executor", "capacity_platform.ai", "kubernetes"},
    "observer": {"capacity_platform.executor", "capacity_platform.governance", "capacity_platform.ai"},
    "contracts": {"capacity_platform.", "kubernetes", "fastapi", "psycopg", "httpx"},
}


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


@pytest.mark.parametrize("package", sorted(FORBIDDEN))
def test_service_package_does_not_cross_authority_boundaries(package):
    violations = []
    for path in (SRC / package).rglob("*.py"):
        for name in _imports(path):
            for banned in FORBIDDEN[package]:
                allowed_self = package == "contracts" and name.startswith("capacity_platform.contracts")
                if name.startswith(banned) and not allowed_self:
                    violations.append(f"{path.relative_to(SRC)} imports {name}")
    assert not violations, violations


def test_exactly_six_platform_deployments():
    from capacity_platform.lab.manifests import platform
    from capacity_platform.shared.serve import ROLES

    deployments = [
        o
        for o in platform(
            "localhost:5001/capacity-platform@sha256:" + "0" * 64,
            "https://kubernetes.default.svc.cluster.local",
            "capacityctl",
        )
        if o["kind"] == "Deployment"
    ]
    assert len(ROLES) == 6 and len(deployments) == 6
    assert all(d["spec"]["replicas"] == 1 for d in deployments)
