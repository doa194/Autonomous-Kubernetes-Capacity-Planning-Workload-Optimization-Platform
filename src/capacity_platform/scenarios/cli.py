"""`capacity-scenario` — S05 experiment CLI (separate from capacityctl and capacity-lab).

Exit codes distinguish the S05 §13 outcomes so automation can react precisely:
0 success, 2 schema invalid, 3 preflight rejected, 4 runtime aborted, 5 cleanup failed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer
from pydantic import ValidationError

from capacity_platform.scenarios.demand import build_plan
from capacity_platform.scenarios.journal import Journal
from capacity_platform.scenarios.schema import load_scenario

app = typer.Typer(no_args_is_help=True, help="Run seeded, bounded, journaled capacity experiments (S05).")
STATE = Path(os.environ.get("CAPACITY_LAB_HOME", Path.home() / ".capacity-lab"))


def _journal() -> Journal:
    return Journal(STATE / "scenario-journal.sqlite")


@app.command()
def validate(path: Path) -> None:
    """Structural + semantic validation; no cluster access."""
    try:
        scenario = load_scenario(path)
    except (ValidationError, ValueError) as exc:
        typer.echo(f"INVALID: {exc}")
        raise typer.Exit(2) from exc
    typer.echo(f"valid {scenario.metadata.id} r{scenario.metadata.revision} digest={scenario.digest()}")


@app.command()
def plan(path: Path, seed: int | None = None) -> None:
    """Print the deterministic demand plan summary and side-effect preview (no writes)."""
    scenario = load_scenario(path)
    p = build_plan(scenario, seed)
    typer.echo(
        json.dumps(
            {
                "planDigest": p.planDigest,
                "seed": p.seed,
                "phases": p.phases,
                "plannedArrivals": {f"{a.phaseId}/{a.actionIndex}": len(a.arrivals) for a in p.actions},
                "sideEffects": [
                    "ConfigMap plan-*",
                    "Secret cap-* (short-lived capability)",
                    "Job driver-* in capacity-experiments",
                ],
            },
            indent=2,
        )
    )


@app.command()
def run(path: Path, seed: int | None = None, driver_image: str = typer.Option(..., envvar="DRIVER_IMAGE")) -> None:
    from capacity_platform.scenarios.runner import ScenarioRunner

    scenario = load_scenario(path)
    runner = ScenarioRunner(
        _journal(), STATE / "evidence" / "experiments", STATE / "keys" / "capability-signing.pem", driver_image
    )
    run_id = runner.run(scenario, seed)
    row = _journal().state(run_id)
    typer.echo(f"{run_id} state={row['state']} outcome={row['outcome']}")
    if row["state"] == "Rejected":
        raise typer.Exit(3)
    if row["state"] == "CleanupFailed":
        raise typer.Exit(5)
    if row["outcome"] == "aborted":
        raise typer.Exit(4)


@app.command()
def status(run_id: str) -> None:
    row = _journal().state(run_id)
    if row is None:
        raise typer.Exit(1)
    typer.echo(json.dumps(dict(row), indent=2))


@app.command()
def abort(run_id: str, reason: str = "operator_abort") -> None:
    _journal().request_abort(run_id, reason)
    typer.echo(f"abort requested for {run_id}")


@app.command()
def recover(driver_image: str = typer.Option("unused", envvar="DRIVER_IMAGE")) -> None:
    """Reconcile runs interrupted by a runner crash: clean owned objects by UID, never resume."""
    from capacity_platform.scenarios.runner import ScenarioRunner, recover_interrupted

    journal = _journal()
    runner = ScenarioRunner(
        journal, STATE / "evidence" / "experiments", STATE / "keys" / "capability-signing.pem", driver_image
    )
    typer.echo(json.dumps(recover_interrupted(journal, runner)))


if __name__ == "__main__":
    app()
