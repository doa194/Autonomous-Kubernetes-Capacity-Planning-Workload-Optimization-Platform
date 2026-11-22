"""Secret redaction applied before logging, evidence export, chunking and embedding (S08 §6).

Redaction is defence in depth, not the primary control: Restricted data is excluded by
design (no secret-reading tools, field allowlists). These patterns catch accidental leaks
of common credential shapes. Matches are replaced by a typed placeholder so reviewers can
see that something was removed without seeing what.
"""

from __future__ import annotations

import re

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("dsn_password", re.compile(r"(?i)(postgres(?:ql)?://[^:\s/]+:)([^@\s]+)(@)")),
    ("kv_secret", re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key|client[_-]?secret)\b(\s*[:=]\s*)(\S+)")),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("hex_secret", re.compile(r"(?i)\b(?:capability|signing|hmac)[_-]?key\s*[:=]\s*[0-9a-f]{32,}\b")),
    ("canary", re.compile(r"CANARY-SECRET-[A-Za-z0-9]{6,}")),
)
REDACTION_REVISION = "redaction-v1"


def redact(text: str) -> tuple[str, list[str]]:
    """Return redacted text and the list of pattern names that matched."""
    hits: list[str] = []
    out = text
    for name, pattern in _PATTERNS:
        if not pattern.search(out):
            continue
        hits.append(name)
        if name == "dsn_password":
            out = pattern.sub(r"\1[REDACTED]\3", out)
        elif name == "kv_secret":
            out = pattern.sub(r"\1\2[REDACTED]", out)
        else:
            out = pattern.sub(f"[REDACTED:{name}]", out)
    return out, hits


def contains_secret(text: str) -> bool:
    return bool(redact(text)[1])
