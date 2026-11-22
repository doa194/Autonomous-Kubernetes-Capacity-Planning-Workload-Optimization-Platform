"""The executor's only window onto Kubernetes: a narrow port plus the real adapter.

The port exposes exactly what S25 needs — read one Deployment, apply one JSON Patch
(optionally as a server-side dry run). There is no generic "apply manifest" or "delete"
method, so even buggy executor code cannot reach beyond the approved action catalog.
The ServiceAccount's RBAC (get/patch on Deployments in demo namespaces) is the second,
independent limit.
"""

from __future__ import annotations

from typing import Any, Protocol


class KubeConflict(Exception):
    """409, or a failed JSON Patch `test` precondition (422): the object changed under us."""


class KubeNotFound(Exception):
    pass


class KubeUncertain(Exception):
    """Timeout or connection loss after sending a write: the effect is UNKNOWN, not failed."""


class KubeRejected(Exception):
    """Admission or validation rejected the request; no effect occurred."""


class KubernetesPort(Protocol):
    def get_deployment(self, namespace: str, name: str) -> dict[str, Any]: ...

    def patch_deployment(
        self, namespace: str, name: str, patch: list[dict[str, Any]], *, dry_run: bool
    ) -> dict[str, Any]: ...


class KubernetesClientAdapter:
    """Real adapter over the official Python client, using the executor's projected SA token."""

    def __init__(self, api_client=None, timeout_seconds: float = 10.0) -> None:
        from kubernetes import client, config

        if api_client is None:
            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config()
            api_client = client.ApiClient()
        self._apps = client.AppsV1Api(api_client)
        self._timeout = timeout_seconds

    def get_deployment(self, namespace: str, name: str) -> dict[str, Any]:
        from kubernetes.client.exceptions import ApiException

        try:
            obj = self._apps.read_namespaced_deployment(name, namespace, _request_timeout=self._timeout)
        except ApiException as exc:
            if exc.status == 404:
                raise KubeNotFound(name) from exc
            raise
        return self._apps.api_client.sanitize_for_serialization(obj)

    def patch_deployment(self, namespace, name, patch, *, dry_run):
        import urllib3
        from kubernetes.client.exceptions import ApiException

        try:
            obj = self._apps.patch_namespaced_deployment(
                name,
                namespace,
                patch,
                dry_run="All" if dry_run else None,
                field_manager="capacity-executor",
                _request_timeout=self._timeout,
                _content_type="application/json-patch+json",
            )
        except ApiException as exc:
            if exc.status in (409, 422) and ("test" in (exc.body or "") or exc.status == 409):
                raise KubeConflict(exc.reason) from exc
            if exc.status == 404:
                raise KubeNotFound(name) from exc
            if exc.status in (400, 403, 422):
                raise KubeRejected(f"{exc.status} {exc.reason}") from exc
            if exc.status >= 500:
                raise KubeUncertain(f"{exc.status}") from exc
            raise
        except (urllib3.exceptions.HTTPError, TimeoutError, ConnectionError) as exc:
            raise KubeUncertain(type(exc).__name__) from exc
        return self._apps.api_client.sanitize_for_serialization(obj)
