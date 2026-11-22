"""Encrypted database backup, isolated restore check and signed audit checkpoints (S08 §6, S13 §7, S29 §6).

Why this file exists: a backup that was never restored is only a hope. `create()` takes an
encrypted logical dump of the platform database; `verify_restore()` restores it into a
throwaway, network-less PostgreSQL container and proves the restored data is usable (all
migrations present, audit hash chain intact, and the audit event recorded at backup time is
still there with the same hash).

`audit_checkpoint()` signs the current head of the audit hash chain with a host-held Ed25519
key. A database superuser could rewrite events AND recompute the whole chain, which the chain
alone cannot detect; a signed checkpoint kept outside the cluster can (`audit_verify()`).

Lab simplifications (see docs/production-considerations.md): backups and keys live on the same
Windows host under ~/.capacity-lab, protected with DPAPI, not in separate off-site storage.
"""

from __future__ import annotations

import hashlib
import json
import secrets as pysecrets
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from capacity_platform.lab.cluster import LOCK
from capacity_platform.lab.protect import read_secret, write_secret
from capacity_platform.lab.tools import CONTEXT, HOME, ToolError, binary, docker

BACKUPS = HOME / "backups"
BACKUP_KEY = BACKUPS / "backup.key.dpapi"
AUDIT_DIR = HOME / "audit"
CHECKPOINT_KEY = AUDIT_DIR / "checkpoint.key.dpapi"
CHECKPOINT_PUB = AUDIT_DIR / "checkpoint.pub"
CHECKPOINTS = AUDIT_DIR / "checkpoints.jsonl"
POD = ("capacity-data", "postgres-0")
RESTORE_READY_TIMEOUT_S = 600
MIGRATIONS_SQL = "SELECT string_agg(version, ',' ORDER BY version) FROM platform_meta.schema_migrations"
TABLE_ROWS_SQL = "SELECT json_object_agg(schemaname || '.' || relname, n_live_tup) FROM pg_stat_user_tables"


# ------------------------------------------------------------------------------------- helpers
def _exec_bytes(*cmd: str) -> bytes:
    """Run a command inside the PostgreSQL pod and return raw stdout (dumps are binary)."""
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [binary("kubectl"), "--context", CONTEXT, "-n", POD[0], "exec", POD[1], "--", *cmd],
        capture_output=True,
        timeout=900,
        check=False,
    )
    if proc.returncode != 0:
        raise ToolError(f"{cmd[0]} failed: {proc.stderr.decode(errors='replace')[-800:]}")
    return proc.stdout


def _psql(sql: str) -> str:
    """Query through the pod's local socket as the database owner (no network credential needed)."""
    return (
        _exec_bytes("psql", "-U", "migrator", "-d", "capacity", "-At", "-v", "ON_ERROR_STOP=1", "-c", sql)
        .decode()
        .strip()
    )


def _key(path: Path, size: int = 32) -> bytes:
    if not path.exists():
        write_secret(path, pysecrets.token_bytes(size))
    return read_secret(path)


def _encrypt(data: bytes, label: str) -> bytes:
    nonce = pysecrets.token_bytes(12)
    return nonce + AESGCM(_key(BACKUP_KEY)).encrypt(nonce, data, label.encode())


def _decrypt(blob: bytes, label: str) -> bytes:
    # AES-GCM authenticates the content: any tampering makes decryption fail instead of
    # silently restoring altered data. The label binds each blob to its role in the backup.
    return AESGCM(_key(BACKUP_KEY)).decrypt(blob[:12], blob[12:], label.encode())


def _chain_head() -> dict:
    row = _psql("SELECT seq || '|' || event_hash FROM audit.chain_head()")
    if not row:
        return {"seq": 0, "eventHash": "genesis"}
    seq, event_hash = row.split("|")
    return {"seq": int(seq), "eventHash": event_hash}


# ------------------------------------------------------------------------------------- backup
def create() -> dict:
    head = _chain_head()  # captured BEFORE the dump, so the dump must contain this event
    migrations = _psql(MIGRATIONS_SQL)
    dump = _exec_bytes("pg_dump", "-U", "migrator", "-d", "capacity", "-Fc", "--no-password")
    roles = _exec_bytes("pg_dumpall", "-U", "migrator", "--roles-only", "--no-role-passwords")
    backup_id = f"capacity-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    folder = BACKUPS / backup_id
    folder.mkdir(parents=True, exist_ok=True)
    files = {"capacity.dump.enc": _encrypt(dump, "dump"), "roles.sql.enc": _encrypt(roles, "roles")}
    for name, blob in files.items():
        (folder / name).write_bytes(blob)
    manifest = {
        "backupId": backup_id,
        "createdAt": datetime.now(UTC).isoformat(),
        "format": "pg_dump-custom+aes256gcm",
        "auditHeadAtBackup": head,
        "migrations": migrations.split(",") if migrations else [],
        "sha256": {name: hashlib.sha256(blob).hexdigest() for name, blob in files.items()},
        "plainBytes": {"dump": len(dump), "roles": len(roles)},
    }
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def _latest() -> Path:
    candidates = sorted(p for p in BACKUPS.glob("capacity-*") if (p / "manifest.json").exists())
    if not candidates:
        raise ToolError("no backup found; run `capacity-lab backup create` first")
    return candidates[-1]


def verify_restore(path: str | None = None) -> dict:
    folder = Path(path) if path else _latest()
    started = time.monotonic()  # restore duration is the measured RTO for the database tier
    manifest = json.loads((folder / "manifest.json").read_text())
    checks: dict[str, bool] = {}
    blobs = {name: (folder / name).read_bytes() for name in manifest["sha256"]}
    checks["ciphertext_digest_matches"] = all(
        hashlib.sha256(blob).hexdigest() == manifest["sha256"][name] for name, blob in blobs.items()
    )
    dump = _decrypt(blobs["capacity.dump.enc"], "dump")
    roles = _decrypt(blobs["roles.sql.enc"], "roles")
    name = f"capacity-restore-{pysecrets.token_hex(4)}"
    # --network none: the restored copy cannot talk to anything, so a restore test can never
    # leak data or be mistaken for the live database.
    docker(
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "--network",
        "none",
        "-e",
        "POSTGRES_USER=migrator",
        "-e",
        f"POSTGRES_PASSWORD={pysecrets.token_urlsafe(16)}",
        "-e",
        "POSTGRES_DB=capacity",
        LOCK["images"]["postgres"],
        quiet=True,
    )

    def query(sql: str) -> str:
        cmd = ["psql", "-U", "migrator", "-d", "capacity", "-At", "-c", sql]
        return _docker_stdin(name, cmd, b"").stdout.decode().strip()

    restore_error = ""
    try:
        _wait_ready(name)
        # Roles first (an "already exists" error for the owner role is expected and harmless).
        _docker_stdin(name, ["psql", "-U", "migrator", "-d", "postgres", "-q"], roles)
        restore_cmd = ["pg_restore", "-U", "migrator", "-d", "capacity", "--exit-on-error"]
        restore = _docker_stdin(name, restore_cmd, dump)
        checks["pg_restore_succeeded"] = restore.returncode == 0
        # Keep the reason a drill failed; pg_restore errors name objects, never data or secrets.
        restore_error = "" if restore.returncode == 0 else restore.stderr.decode(errors="replace")[-500:]
        checks["migrations_match"] = query(MIGRATIONS_SQL).split(",") == manifest["migrations"]
        chain = query("SELECT coalesce(audit.verify_chain()::text, 'intact')")
        checks["audit_chain_intact"] = chain == "intact"
        head = manifest["auditHeadAtBackup"]
        checks["audit_head_present"] = (
            head["seq"] == 0
            or query(f"SELECT event_hash FROM audit.events WHERE seq = {int(head['seq'])}") == head["eventHash"]
        )
        counts = query(TABLE_ROWS_SQL)
    finally:
        docker("rm", "-f", name, check=False, quiet=True)
    result = {
        "backupId": manifest["backupId"],
        "restored": all(checks.values()),
        "checks": checks,
        "restoredTableRows": json.loads(counts) if counts else {},
        **({"restoreError": restore_error} if restore_error else {}),
        "durationSeconds": round(time.monotonic() - started, 1),
        "verifiedAt": datetime.now(UTC).isoformat(),
    }
    (folder / "restore-verification.json").write_text(json.dumps(result, indent=2))
    return result


def _docker_stdin(name: str, cmd: list[str], data: bytes) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [binary("docker"), "exec", "-i", name, *cmd],
        input=data,
        capture_output=True,
        timeout=900,
        check=False,
    )


def _wait_ready(name: str) -> None:
    """Wait until the restore container's *final* PostgreSQL server is up.

    The postgres image first runs a temporary server for initialisation, then stops it and starts
    the real one. The temporary server listens only on the Unix socket, so probing over TCP
    (127.0.0.1 exists even with `--network none`) never mistakes it for the real server; a restore
    started against the temporary server fails when it shuts down. Initialisation is disk-bound and
    can take minutes on a hard-disk Docker host.
    """
    probe = ["pg_isready", "-h", "127.0.0.1", "-U", "migrator", "-d", "capacity"]
    deadline, good = time.monotonic() + RESTORE_READY_TIMEOUT_S, 0
    while time.monotonic() < deadline and good < 2:
        time.sleep(1)
        ready = _docker_stdin(name, probe, b"").returncode == 0
        good = good + 1 if ready else 0
    if good < 2:
        raise ToolError("restore container did not become ready")


# ------------------------------------------------------------------------------------- audit checkpoints
def _signing_key() -> Ed25519PrivateKey:
    if not CHECKPOINT_KEY.exists():
        key = Ed25519PrivateKey.generate()
        pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        write_secret(CHECKPOINT_KEY, pem)
        CHECKPOINT_PUB.write_bytes(
            key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        )
    return serialization.load_pem_private_key(read_secret(CHECKPOINT_KEY), password=None)  # type: ignore[return-value]


def _payload(entry: dict) -> bytes:
    return json.dumps({k: entry[k] for k in ("seq", "eventHash", "checkpointAt")}, sort_keys=True).encode()


def audit_checkpoint() -> dict:
    head = _chain_head()
    entry = {**head, "checkpointAt": datetime.now(UTC).isoformat()}
    entry["signature"] = _signing_key().sign(_payload(entry)).hex()
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    with CHECKPOINTS.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
    return entry


def audit_verify() -> dict:
    """Chain recomputation + every signed checkpoint still matches the live database."""
    public = serialization.load_pem_public_key(CHECKPOINT_PUB.read_bytes()) if CHECKPOINT_PUB.exists() else None
    lines = CHECKPOINTS.read_text().splitlines() if CHECKPOINTS.exists() else []
    entries = [json.loads(line) for line in lines if line.strip()]
    broken_at = _psql("SELECT coalesce(audit.verify_chain()::text, '')")
    head = _chain_head()
    problems: list[dict] = []
    for entry in entries:
        try:
            public.verify(bytes.fromhex(entry["signature"]), _payload(entry))  # type: ignore[union-attr]
        except (InvalidSignature, AttributeError, ValueError):
            problems.append({"seq": entry["seq"], "problem": "checkpoint_signature_invalid"})
            continue
        if entry["seq"] == 0:
            continue
        if entry["seq"] > head["seq"]:
            problems.append({"seq": entry["seq"], "problem": "events_missing_after_checkpoint"})  # truncation
            continue
        live = _psql(f"SELECT event_hash FROM audit.events WHERE seq = {int(entry['seq'])}")
        if live != entry["eventHash"]:
            problems.append({"seq": entry["seq"], "problem": "event_rewritten_since_checkpoint"})
    return {
        "intact": not broken_at and not problems,
        "chainBrokenAtSeq": int(broken_at) if broken_at else None,
        "head": head,
        "checkpointsChecked": len(entries),
        "problems": problems,
        "verifiedAt": datetime.now(UTC).isoformat(),
    }
