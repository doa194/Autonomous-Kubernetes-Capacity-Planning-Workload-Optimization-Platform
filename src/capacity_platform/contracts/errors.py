"""Typed platform errors and the S27 error envelope.

Services raise `PlatformError` with a stable code. The API maps the category to an HTTP
status. Messages are written to be safe for the caller: they never contain secrets,
stack traces or the existence of objects in namespaces the caller cannot see.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel


class ErrorCategory(StrEnum):
    AUTHENTICATION = "authentication"  # 401
    AUTHORIZATION = "authorization"  # 403 (or opaque 404)
    NOT_FOUND = "not_found"  # 404
    VALIDATION = "validation"  # 422
    PRECONDITION = "precondition"  # 409/412: stale revision, digest mismatch, mode
    CONFLICT = "conflict"  # 409: idempotency key reused with a different payload
    RATE_LIMITED = "rate_limited"  # 429
    UNAVAILABLE = "unavailable"  # 503: a dependency needed for this call is down
    INTERNAL = "internal"  # 500


HTTP_STATUS = {
    ErrorCategory.AUTHENTICATION: 401,
    ErrorCategory.AUTHORIZATION: 403,
    ErrorCategory.NOT_FOUND: 404,
    ErrorCategory.VALIDATION: 422,
    ErrorCategory.PRECONDITION: 409,
    ErrorCategory.CONFLICT: 409,
    ErrorCategory.RATE_LIMITED: 429,
    ErrorCategory.UNAVAILABLE: 503,
    ErrorCategory.INTERNAL: 500,
}


class PlatformError(Exception):
    """A refusal with a stable, machine-readable reason code."""

    def __init__(
        self,
        code: str,
        category: ErrorCategory,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.category = category
        self.message = message
        self.retryable = retryable
        self.details = details or {}

    @property
    def http_status(self) -> int:
        return HTTP_STATUS[self.category]


def denied(code: str = "AUTHORIZATION_DENIED") -> PlatformError:
    # Deliberately generic: the same message is returned whether or not the target exists.
    return PlatformError(code, ErrorCategory.AUTHORIZATION, "The request is not permitted for this caller")


def precondition(code: str, message: str) -> PlatformError:
    return PlatformError(code, ErrorCategory.PRECONDITION, message)


def validation(code: str, message: str) -> PlatformError:
    return PlatformError(code, ErrorCategory.VALIDATION, message)


def unavailable(code: str, message: str) -> PlatformError:
    return PlatformError(code, ErrorCategory.UNAVAILABLE, message, retryable=True)


class ErrorBody(BaseModel):
    code: str
    category: ErrorCategory
    message: str
    retryable: bool
    correlationId: str | None = None


class ErrorEnvelope(BaseModel):
    schemaVersion: str = "platform-error/v1alpha1"
    error: ErrorBody


def to_envelope(err: PlatformError, correlation_id: str | None) -> ErrorEnvelope:
    return ErrorEnvelope(
        error=ErrorBody(
            code=err.code,
            category=err.category,
            message=err.message,
            retryable=err.retryable,
            correlationId=correlation_id,
        )
    )
