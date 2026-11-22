"""Kubernetes resource quantity parsing into canonical integer units.

Every capacity value in the platform is carried as an integer: CPU in millicores and
memory in bytes (S11 §6.1, Domain E §1). Floats are never used for scheduling-fit
comparisons, because rounding errors could turn "does not fit" into "fits".
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# Binary (IEC) and decimal (SI) suffixes accepted by Kubernetes resource.Quantity.
_BINARY = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60}
_DECIMAL = {
    "n": Decimal("1e-9"),
    "u": Decimal("1e-6"),
    "m": Decimal("1e-3"),
    "": Decimal(1),
    "k": Decimal(10**3),
    "M": Decimal(10**6),
    "G": Decimal(10**9),
    "T": Decimal(10**12),
    "P": Decimal(10**15),
    "E": Decimal(10**18),
}
_QUANTITY = re.compile(r"^([+-]?[0-9.]+(?:[eE][+-]?[0-9]+)?)(Ki|Mi|Gi|Ti|Pi|Ei|n|u|m|k|M|G|T|P|E)?$")


class QuantityError(ValueError):
    """Raised when a quantity string is malformed or cannot be represented exactly."""


def _parse(text: str) -> Decimal:
    if not isinstance(text, str):
        raise QuantityError(f"quantity must be a string, got {type(text).__name__}")
    match = _QUANTITY.match(text.strip())
    if not match:
        raise QuantityError(f"malformed quantity: {text!r}")
    number, suffix = match.group(1), match.group(2) or ""
    # Scientific notation like "1e3" is valid in Kubernetes; "E" alone is the exa suffix.
    try:
        value = Decimal(number)
    except InvalidOperation as exc:
        raise QuantityError(f"malformed quantity: {text!r}") from exc
    if not value.is_finite():
        raise QuantityError(f"non-finite quantity: {text!r}")
    if suffix in _BINARY:
        return value * _BINARY[suffix]
    return value * _DECIMAL[suffix]


def cpu_to_millicores(text: str) -> int:
    """Convert a CPU quantity ("250m", "1", "0.5") to integer millicores.

    Sub-millicore precision is rejected instead of silently rounded, as S11 §6.1 requires.
    """
    cores = _parse(text)
    if cores < 0:
        raise QuantityError(f"negative CPU quantity: {text!r}")
    millis = cores * 1000
    if millis != millis.to_integral_value():
        raise QuantityError(f"CPU quantity below 1m precision: {text!r}")
    return int(millis)


def memory_to_bytes(text: str) -> int:
    """Convert a memory quantity ("256Mi", "1G", "1500") to integer bytes."""
    value = _parse(text)
    if value < 0:
        raise QuantityError(f"negative memory quantity: {text!r}")
    if value != value.to_integral_value():
        raise QuantityError(f"fractional byte quantity: {text!r}")
    return int(value)


def millicores_to_quantity(millicores: int) -> str:
    """Render millicores as the canonical Kubernetes string used in executor patches."""
    if millicores < 0:
        raise QuantityError("negative millicores")
    if millicores % 1000 == 0:
        return str(millicores // 1000)
    return f"{millicores}m"


def bytes_to_quantity(num_bytes: int) -> str:
    """Render bytes using the largest exact binary suffix (keeps patches exact)."""
    if num_bytes < 0:
        raise QuantityError("negative bytes")
    for suffix, factor in (("Gi", 2**30), ("Mi", 2**20), ("Ki", 2**10)):
        if num_bytes and num_bytes % factor == 0:
            return f"{num_bytes // factor}{suffix}"
    return str(num_bytes)


MIB = 2**20
GIB = 2**30
