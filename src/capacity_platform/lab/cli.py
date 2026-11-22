"""`capacity-lab` — host bootstrap, verification, backup and teardown (S03 §8.2, S29 §3.1).

`capacity-lab bootstrap` runs every stage in dependency order and records each completed
stage in a journal under ~/.capacity-lab, so a re-run resumes instead of duplicating work.
A failed fail-closed gate (for example default-deny not actually enforced) stops the run.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime

import typer

from capacity_platform.lab import cluster, install
from capacity_platform.lab.tools import HOME

app = typer.Typer(no_args_is_help=True, help="Create, verify, back up and tear down the local capacity lab.")
JOURNAL = HOME / "bootstrap-journal.json"

STAGES = [
    "preflight",
    "registry",
    "cluster",
    "calico",
    "base",
    "network-dynamic",
    "verify-network",
    "pki",
    "secrets",
    "database",
    "identity",
    "observability",
    "images",
    "platform",
    "workloads",
    "grants",
    "host-identities",
]


def _journal() -> dict:
    return json.loads(JOURNAL.read_text()) if JOURNAL.exists() else {"stages": {}}


def _mark(stage: str, detail: dict | None = None) -> None:
    j = _journal()
    j["stages"][stage] = {"completedAt": datetime.now(UTC).isoformat(), **(detail or {})}
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    JOURNAL.write_text(json.dumps(j, indent=2))


def run_stage(stage: str) -> dict | None:
    from capacity_platform.lab import verify

    started = time.monotonic()
    typer.echo(f"== {stage}")
    result = None
    if stage == "preflight":
        result = cluster.preflight()
        if not result["ok"]:
            typer.echo(json.dumps(result, indent=2))
            raise typer.Exit(2)
    elif stage == "registry":
        cluster.ensure_registry()
    elif stage == "cluster":
        cluster.create_cluster()
    elif stage == "calico":
        cluster.install_calico()
    elif stage == "base":
        cluster.apply_base()
    elif stage == "network-dynamic":
        result = cluster.network_facts()
        install.apply_dynamic_network(result)
    elif stage == "verify-network":
        result = verify.network_enforced()
        if not result["ok"]:  # S03-TEST-016: never declare secure readiness without real enforcement
            typer.echo(json.dumps(result, indent=2))
            raise typer.Exit(3)
    elif stage == "pki":
        install.install_pki()
    elif stage == "secrets":
        install.install_secrets()
    elif stage == "database":
        install.install_database()
    elif stage == "identity":
        install.install_identity()
    elif stage == "observability":
        install.install_observability()
    elif stage == "images":
        result = install.build_images()
    elif stage == "platform":
        install.deploy_platform()
    elif stage == "workloads":
        install.deploy_workloads()
    elif stage == "grants":
        result = {"users": list(install.seed_grants())}
    elif stage == "host-identities":
        result = verify.host_identities()
    _mark(stage, {"seconds": round(time.monotonic() - started, 1)})
    return result


@app.command()
def preflight() -> None:
    typer.echo(json.dumps(cluster.preflight(), indent=2))


@app.command()
def bootstrap(
    from_stage: str = typer.Option(None, help="resume from this stage"),
    only: str = typer.Option(None, help="run exactly one stage"),
    force: bool = False,
) -> None:
    done = _journal()["stages"]
    stages = [only] if only else STAGES[STAGES.index(from_stage) :] if from_stage else STAGES
    for stage in stages:
        if stage not in STAGES:
            raise typer.BadParameter(f"unknown stage {stage}")
        if stage in done and not force and not only and not from_stage:
            typer.echo(f"== {stage} (already completed; skipping)")
            continue
        run_stage(stage)
    typer.echo("bootstrap complete")


@app.command()
def status() -> None:
    typer.echo(json.dumps(_journal(), indent=2))


@app.command("verify")
def verify_cmd(area: str = typer.Argument("all", help="networking|security|observability|scheduling|all")) -> None:
    from capacity_platform.lab import verify

    report = verify.run_checks(area)
    out = HOME / "evidence" / f"verify-{area}-{datetime.now(UTC):%Y%m%dT%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str))
    typer.echo(json.dumps({k: v["verdict"] for k, v in report["checks"].items()}, indent=2))
    typer.echo(f"evidence: {out}")
    if any(v["verdict"] == "fail" for v in report["checks"].values()):
        raise typer.Exit(1)


@app.command()
def destroy(confirm: str = typer.Option(None), dry_run: bool = True) -> None:
    typer.echo(json.dumps(cluster.destroy(confirm, dry_run), indent=2, default=str))


@app.command()
def unseal() -> None:
    """Unseal Vault after a cluster or Docker restart (the lab uses manual, single-share unseal)."""
    from capacity_platform.lab.secrets import init_and_unseal

    init_and_unseal()
    typer.echo("vault unsealed")


@app.command()
def resume() -> None:
    """Bring the lab back after Docker Desktop or the host restarted.

    Docker may give the kind node containers new IP addresses, which makes the rendered
    API-server network policies stale (observer, executor, Prometheus and kube-state-metrics then
    lose the Kubernetes API). Vault also restarts sealed. This re-discovers the addresses,
    re-renders the policies, restarts the Pods that hold stale connections, and unseals Vault.
    """
    from capacity_platform.lab import recovery

    typer.echo(json.dumps(recovery.resume(), indent=2))


@app.command()
def credentials(user: str) -> None:
    """Print one lab test user's password (explicit request only; never logged)."""
    from capacity_platform.lab.identity import ensure_passwords

    typer.echo(ensure_passwords()[user])


backup_app = typer.Typer(no_args_is_help=True)
app.add_typer(backup_app, name="backup")


@backup_app.command("create")
def backup_create() -> None:
    from capacity_platform.lab.backup import create

    typer.echo(json.dumps(create(), indent=2))


@backup_app.command("verify")
def backup_verify(path: str = typer.Argument(None)) -> None:
    from capacity_platform.lab.backup import verify_restore

    result = verify_restore(path)
    typer.echo(json.dumps(result, indent=2))
    if not result["restored"]:
        raise typer.Exit(1)


audit_app = typer.Typer(no_args_is_help=True)
app.add_typer(audit_app, name="audit")


@audit_app.command("checkpoint")
def audit_checkpoint() -> None:
    from capacity_platform.lab.backup import audit_checkpoint as cp

    typer.echo(json.dumps(cp(), indent=2))


@audit_app.command("verify")
def audit_verify() -> None:
    from capacity_platform.lab.backup import audit_verify as av

    result = av()
    typer.echo(json.dumps(result, indent=2))
    if not result["intact"]:
        raise typer.Exit(1)


model_app = typer.Typer(no_args_is_help=True)
app.add_typer(model_app, name="model")


@model_app.command("gateway")
def model_gateway() -> None:
    """Prepare and start the protected host model gateway (foreground)."""
    from capacity_platform.lab.model import run_gateway

    run_gateway()


@model_app.command("qualify")
def model_qualify(samples: int = 6) -> None:
    from capacity_platform.lab.model import qualify

    typer.echo(json.dumps(qualify(samples), indent=2))


release_app = typer.Typer(no_args_is_help=True, help="S30-S32 test evidence and release qualification.")
app.add_typer(release_app, name="release")


@release_app.command("test")
def release_test(
    suite: list[str] = typer.Option(None, help="unit|security|component|integration|operational|e2e|dotnet"),
) -> None:
    """Run the test suites and keep their junit results as release evidence."""
    from capacity_platform.release.bundle import run_test_suites

    summary = run_test_suites(suite or None)
    typer.echo(json.dumps({k: {"exit": v["exitCode"], "counts": v["counts"]} for k, v in summary.items()}, indent=2))


@release_app.command("qualify")
def release_qualify() -> None:
    """Compute the six S32 gates from recorded evidence (never runs experiments itself)."""
    from capacity_platform.release.bundle import qualify

    report = qualify()
    typer.echo(
        json.dumps(
            {
                "outcome": report["outcome"],
                "gates": {g["gate"]: g["verdict"] for g in report["gates"]},
                "folder": report["folder"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    app()
