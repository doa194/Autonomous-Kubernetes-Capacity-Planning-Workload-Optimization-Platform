"""Thin, logged wrappers around the pinned host tools capacity-lab drives (S03 §8.1).

Python coordinates `docker`, `kind`, `kubectl` and `helm`; no Bash/PowerShell business logic.
Every command runs with an explicit kube context so nothing silently targets another
cluster, and secret material is passed through stdin or files, never through arguments.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
TOOLS = ROOT / ".tools" / "bin"
HOME = Path(os.environ.get("CAPACITY_LAB_HOME", Path.home() / ".capacity-lab"))
CLUSTER = "capacity-lab"
CONTEXT = f"kind-{CLUSTER}"


class ToolError(RuntimeError):
    pass


def binary(name: str) -> str:
    local = TOOLS / (name + (".exe" if os.name == "nt" else ""))
    if local.exists():
        return str(local)
    found = shutil.which(name)
    if not found:
        raise ToolError(f"required tool not found: {name}")
    return found


def run(
    args: list[str], *, input_text: str | None = None, check: bool = True, timeout: int = 600, quiet: bool = False
) -> subprocess.CompletedProcess:
    if not quiet:
        print("  $ " + " ".join(a if len(a) < 120 else a[:117] + "..." for a in args[:12]))
    proc = subprocess.run(  # noqa: S603 - argv list built by this module, no shell
        args,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        raise ToolError(f"{Path(args[0]).name} failed ({proc.returncode}): {proc.stderr.strip()[-1500:]}")
    return proc


def kubectl(
    *args: str,
    input_text: str | None = None,
    check: bool = True,
    timeout: int = 300,
    context: str = CONTEXT,
    quiet: bool = False,
) -> subprocess.CompletedProcess:
    return run(
        [binary("kubectl"), "--context", context, *args],
        input_text=input_text,
        check=check,
        timeout=timeout,
        quiet=quiet,
    )


def kubectl_json(*args: str, context: str = CONTEXT) -> Any:
    return json.loads(kubectl(*args, "-o", "json", context=context, quiet=True).stdout)


def apply(manifest: str, *, server_side: bool = False) -> None:
    extra = ["--server-side", "--force-conflicts"] if server_side else []
    kubectl("apply", *extra, "-f", "-", input_text=manifest, timeout=300)


def wait_for(description: str, predicate, timeout: int = 600, interval: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                print(f"  ✓ {description}")
                return
        except Exception as exc:  # transient API errors while things start
            last_error = exc
        time.sleep(interval)
    raise ToolError(f"timed out waiting for {description}" + (f": {last_error}" if last_error else ""))


def rollout(namespace: str, kind_name: str, timeout: str = "600s") -> None:
    kubectl("-n", namespace, "rollout", "status", kind_name, f"--timeout={timeout}", timeout=900)


def docker(*args: str, check: bool = True, timeout: int = 1800, quiet: bool = False) -> subprocess.CompletedProcess:
    return run([binary("docker"), *args], check=check, timeout=timeout, quiet=quiet)
