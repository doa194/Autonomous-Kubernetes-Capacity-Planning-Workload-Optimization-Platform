"""Single container entrypoint: `capacity-service <role>` starts exactly one of the six roles.

The six Deployments share one image for build economy, but each runs one role, with its
own ServiceAccount, database role, network policy and configuration. The role argument
decides which modules are imported and which credentials are expected (S02 §3.4).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import threading
from pathlib import Path

import uvicorn

from capacity_platform.shared.db import connect, dsn_from_environment
from capacity_platform.shared.web import configure_logging

ROLES = ("api", "observer", "capacity", "ai", "governance", "executor")


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise SystemExit(f"missing required setting {name}")
    return value


def _users():
    from capacity_platform.shared.auth.oidc import AccessTokenValidator, OidcSettings

    return AccessTokenValidator(
        OidcSettings(
            issuer=_env("OIDC_ISSUER"),
            audience=_env("OIDC_AUDIENCE", "capacity-api"),
            allowed_clients=frozenset(_env("OIDC_ALLOWED_CLIENTS", "capacityctl").split(",")),
            jwks_url=os.environ.get("OIDC_JWKS_URL"),
            jwks_file=os.environ.get("OIDC_JWKS_FILE"),
            ca_bundle=os.environ.get("OIDC_CA_BUNDLE"),
        )
    )


def _machines():
    from capacity_platform.shared.auth.service_identity import ServiceTokenVerifier

    return ServiceTokenVerifier(issuer=_env("SA_ISSUER"), jwks_file=_env("SA_JWKS_FILE"))


def _cluster_incarnation() -> str:
    from kubernetes import client, config

    config.load_incluster_config()
    uid = client.CoreV1Api().read_namespace("kube-system").metadata.uid
    return "inc-" + hashlib.sha256(f"capacity-lab|{uid}".encode()).hexdigest()[:16]


def build(role: str):
    dsn = dsn_from_environment()

    def conn():
        return connect(dsn)

    stop = threading.Event()
    if role == "governance":
        from capacity_platform.governance.policy_sync import sync_effective_policies
        from capacity_platform.governance.service import GovernanceService, build_app

        sync_effective_policies(conn, Path(_env("POLICY_FILE")), Path(_env("GOVERNANCE_POLICY_FILE")))
        mtls = os.environ.get("SERVE_MTLS_PORT")
        return build_app(GovernanceService(conn), _users(), _machines(), mtls_port=int(mtls) if mtls else None)
    if role == "capacity":
        from capacity_platform.capacity.service import CapacityWorker, build_app
        from capacity_platform.capacity.telemetry.registry import PrometheusClient

        worker = CapacityWorker(conn, PrometheusClient(_env("PROMETHEUS_URL")), Path(_env("POLICY_FILE")))
        threading.Thread(target=worker.run_forever, args=("capacity-0", stop), daemon=True).start()
        return build_app(conn, _users(), _machines(), worker)
    if role == "observer":
        from capacity_platform.observer.service import Observer, build_app

        observer = Observer(conn)
        observer.start()
        return build_app(observer)
    if role == "executor":
        from capacity_platform.capacity.telemetry.registry import PrometheusClient
        from capacity_platform.executor.service import ExecutorRuntime, build_app
        from capacity_platform.executor.verification import load_verification_policy

        runtime = ExecutorRuntime(
            conn,
            _cluster_incarnation(),
            load_verification_policy(Path(_env("VERIFICATION_POLICY_FILE"))),
            PrometheusClient(_env("PROMETHEUS_URL")),
        )
        threading.Thread(target=runtime.process_intents, args=(stop,), daemon=True).start()
        threading.Thread(target=runtime.verification_loop, args=(stop,), daemon=True).start()
        return build_app(runtime)
    if role == "ai":
        from capacity_platform.ai.agents.workflow import GatewayChatClientFactory, agent_framework_model_call
        from capacity_platform.ai.inference.client import GatewayClient, load_profile
        from capacity_platform.ai.service import AdvisoryWorker, HttpToolBackend, build_app

        profile = load_profile(Path(_env("INFERENCE_PROFILE_FILE")))
        gateway = GatewayClient(profile, os.environ.get("GATEWAY_KEY_FILE"), os.environ.get("GATEWAY_CA_BUNDLE"))
        backend = HttpToolBackend(_env("CAPACITY_URL"), conn, os.environ.get("INTERNAL_CA_BUNDLE"), gateway)
        model_ref = {
            "name": profile.generationModel.name,
            "digest": profile.generationModel.digest,
            "profile": f"{profile.profileId}@{profile.revision}",
            "qualified": profile.qualified,
        }
        worker = AdvisoryWorker(
            conn, backend, agent_framework_model_call(GatewayChatClientFactory(gateway)), model_ref, profile.qualified
        )
        threading.Thread(target=worker.run_forever, args=(stop,), daemon=True).start()
        return build_app(conn, _users(), _machines(), worker, gateway)
    if role == "api":
        from capacity_platform.api.app import Relay, build_app

        cert = os.environ.get("CLIENT_TLS_CERT")
        client_cert = (cert, _env("CLIENT_TLS_KEY")) if cert else None
        return build_app(
            conn,
            _users(),
            Relay(_env("GOVERNANCE_URL"), _env("AI_URL"), os.environ.get("INTERNAL_CA_BUNDLE"), client_cert),
        )
    raise SystemExit(f"unknown role {role!r}; expected one of {ROLES}")


def main() -> None:
    role = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("CAPACITY_ROLE", "")
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    app = build(role)
    tls = {}
    if os.environ.get("SERVE_TLS_CERT"):
        tls = {"ssl_certfile": os.environ["SERVE_TLS_CERT"], "ssl_keyfile": os.environ["SERVE_TLS_KEY"]}
    port = int(os.environ.get("PORT", "8443" if tls else "8080"))
    servers = [uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, log_config=None, **tls))]  # noqa: S104
    mtls_port = os.environ.get("SERVE_MTLS_PORT")
    if mtls_port:
        import ssl

        # Selective mTLS (S07 L3): privileged internal routes are served only on this listener,
        # which refuses any TLS client without a certificate from the platform CA. Probes and
        # metrics stay on the TLS-only port because kubelet/Prometheus present no client cert.
        servers.append(
            uvicorn.Server(
                uvicorn.Config(
                    app,
                    host="0.0.0.0",  # noqa: S104 - inside the Pod; NetworkPolicy limits callers
                    port=int(mtls_port),
                    log_config=None,
                    **tls,
                    ssl_ca_certs=os.environ["SERVE_TLS_CLIENT_CA"],
                    ssl_cert_reqs=ssl.CERT_REQUIRED,
                )
            )
        )

    async def serve_all() -> None:
        await asyncio.gather(*(s.serve() for s in servers))

    asyncio.run(serve_all())
