"""Canonical JSON serialization and SHA-256 content digests.

Plans, approvals, candidates, evaluations and evidence are identified by the digest of a
canonical serialization (S13 §5.3, S24 §2). Two producers that see the same semantic
content must compute the same digest regardless of key order or how they were assembled.

A digest detects accidental change. It is not a signature: anyone who can rewrite the
bytes can rewrite the digest. Audit integrity uses separately signed checkpoints.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel

CANONICALIZATION_REVISION = 1
_DIGEST_PREFIX = "sha256:"


class CanonicalizationError(ValueError):
    """Raised for values that have no canonical form (NaN, naive datetimes, ...)."""


def _normalize(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _normalize(value.model_dump(mode="python", by_alias=True))
    if isinstance(value, Enum):
        return _normalize(value.value)
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalizationError(f"non-string key {key!r}")
            out[key] = _normalize(item)
        return out
    if isinstance(value, list | tuple):
        return [_normalize(item) for item in value]
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalizationError("NaN/Infinity have no canonical form")
        # Integral floats are written as integers so 200.0 and 200 hash the same.
        return int(value) if value.is_integer() else repr(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise CanonicalizationError("non-finite decimal")
        normalized = value.normalize()
        return int(normalized) if normalized == normalized.to_integral_value() else str(normalized)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise CanonicalizationError("naive datetime; all platform times are UTC-aware")
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    raise CanonicalizationError(f"unsupported type {type(value).__name__}")


def canonical_json(value: Any, *, exclude: frozenset[str] = frozenset()) -> bytes:
    """Serialize with sorted keys and no whitespace. Top-level keys in `exclude` are dropped
    (used to leave the digest field itself out of its own hash)."""
    normalized = _normalize(value)
    if exclude and isinstance(normalized, dict):
        normalized = {k: v for k, v in normalized.items() if k not in exclude}
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value: Any, *, exclude: frozenset[str] = frozenset()) -> str:
    return _DIGEST_PREFIX + hashlib.sha256(canonical_json(value, exclude=exclude)).hexdigest()


def digest_bytes(data: bytes) -> str:
    return _DIGEST_PREFIX + hashlib.sha256(data).hexdigest()


def is_digest(text: str) -> bool:
    return (
        isinstance(text, str)
        and text.startswith(_DIGEST_PREFIX)
        and len(text) == len(_DIGEST_PREFIX) + 64
        and all(c in "0123456789abcdef" for c in text[len(_DIGEST_PREFIX) :])
    )


def utcnow() -> datetime:
    return datetime.now(UTC)
