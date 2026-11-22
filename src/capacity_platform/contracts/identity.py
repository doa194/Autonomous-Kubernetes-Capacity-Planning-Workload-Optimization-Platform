"""Identity references that travel between services.

A logical name such as `w01-latency-api` is not proof that the live object is the one
that was analysed. Decisions bind the cluster incarnation (changes when kind is rebuilt)
and the exact Kubernetes UID (changes when an object is recreated). See S11 §5.1.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class ClusterRef(BaseModel):
    model_config = _FROZEN
    logicalId: str = Field(min_length=1, max_length=63)
    incarnationId: str = Field(min_length=1, max_length=128)


class ControllerRef(BaseModel):
    model_config = _FROZEN
    apiVersion: str
    kind: str
    namespace: str
    name: str
    uid: str = Field(min_length=1)


class TargetRef(BaseModel):
    """Exact optimization target: one container of one controller in one cluster incarnation."""

    model_config = _FROZEN
    cluster: ClusterRef
    namespace: str
    logicalWorkloadId: str
    controller: ControllerRef
    containerName: str

    def target_key(self) -> str:
        """Durable mutex key used by the executor (S25.2: serialize by controller)."""
        return "|".join((self.cluster.incarnationId, self.namespace, self.controller.uid, self.containerName))


class ActorRef(BaseModel):
    """A verified human or machine principal. Only issuer+subject identify a human;
    display names and e-mails are never used for separation-of-duty checks."""

    model_config = _FROZEN
    issuer: str
    subject: str
    kind: str = Field(default="human", pattern="^(human|machine)$")

    def key(self) -> str:
        return f"{self.kind}:{self.issuer}#{self.subject}"
