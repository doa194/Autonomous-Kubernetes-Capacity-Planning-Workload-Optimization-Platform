"""Host-side protection for lab secrets: Windows DPAPI per-user encryption (S08 §4.1).

Used for the offline root CA key, Vault unseal/root material, generated database passwords
and test-user passwords. On non-Windows hosts the files fall back to 0600 permissions, which
is weaker and documented as such. Values are never printed.
"""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    if os.name != "nt":
        return data
    crypt32 = ctypes.windll.crypt32
    buf = ctypes.create_string_buffer(data, len(data))
    src = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("DPAPI operation failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def write_secret(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_dpapi(data, protect=True))
    try:
        path.chmod(0o600)
    except OSError:
        pass


def read_secret(path: Path) -> bytes:
    return _dpapi(path.read_bytes(), protect=False)


def write_json(path: Path, value: dict) -> None:
    write_secret(path, json.dumps(value).encode())


def read_json(path: Path) -> dict:
    return json.loads(read_secret(path))
