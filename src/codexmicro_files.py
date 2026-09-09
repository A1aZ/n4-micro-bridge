"""In-memory Work Louder/openmicrokbd file RPC store.

The physical firmware exposes two fixed file slots.  This module mirrors the
observable protocol contract for the emulator while deliberately keeping all
state in memory.  It is shared by the Python sideband endpoint and tests; no
driver or host filesystem access is performed.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Optional


DEFAULT_KEYMAP = (
    '{"version":1,"activeProfileId":0,"profiles":[{"id":0,"name":"Codex",'
    '"layers":[{"id":0,"name":"ChatGPT","color":16711680,"os":0,"layout":'
    '{"keymap":[["KV_OAI_AG00","KV_OAI_AG01"],["KV_OAI_AG02","KV_OAI_AG03",'
    '"KV_OAI_AG04","KV_OAI_AG05"],["KV_OAI_ACT06","KV_OAI_ACT07",'
    '"KV_OAI_ACT08","KV_OAI_ACT09"],["KV_OAI_ACT10","KV_OAI_ACT11",'
    '"KV_OAI_ACT12"]],"encoders":[["KV_OAI_ENC_CC","KV_OAI_ENC_CW",'
    '"KV_OAI_ENC_CLK"]],"buttons":[["KC_MPLY"]],"joystick":{"type":"VENDOR",'
    '"sectors":[]}}}],"macrosUsed":[],"multiActionsUsed":[]}],"multiActions":[],'
    '"macros":[],"macrosGroups":[],"multiActionsGroups":[],"linkedApps":[]}'
)

FILE_SPECS = {
    "keymap.json": {"max_bytes": 0x3000, "default": DEFAULT_KEYMAP},
    "smart_actions.json": {"max_bytes": 0x1800, "default": None},
}
FILE_ORDER = tuple(FILE_SPECS)
MAX_FILE_NAME = 32
READ_CHUNK = 384


def normalize_file_name(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    name = value[1:] if value.startswith("/") else value
    if (
        not name
        or len(name) > MAX_FILE_NAME
        or "/" in name
        or "\\" in name
        or name not in FILE_SPECS
    ):
        return None
    return name


def sha1_hex(value: bytes) -> str:
    return hashlib.sha1(value).hexdigest()


def strict_base64(value: Any) -> bytes:
    if not isinstance(value, str):
        raise ValueError("data must be base64 text")
    compact = "".join(value.split())
    # The firmware accepts URL-safe alphabet and omitted padding.
    unpadded = compact.rstrip("=")
    if any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/_-" for char in unpadded):
        raise ValueError("invalid base64 data")
    if len(unpadded) % 4 == 1:
        raise ValueError("invalid base64 data")
    normalized = unpadded.replace("-", "+").replace("_", "/")
    try:
        return base64.b64decode(normalized + "=" * ((4 - len(normalized) % 4) % 4), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid base64 data") from exc


def json_bytes(value: Any) -> bytes:
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"data is not JSON encodable: {exc}") from exc
    return text.encode("utf-8")


@dataclass
class _PendingWrite:
    name: str
    chunks: list[bytes]
    size: int = 0
    failed: bool = False


class MicroFileStore:
    """Bounded, process-local implementation of the two Micro file slots."""

    def __init__(self, initial: Optional[Mapping[str, Any]] = None) -> None:
        self.files: dict[str, bytes] = {}
        self.pending: Optional[_PendingWrite] = None
        if isinstance(initial, Mapping):
            for name, value in initial.items():
                normalized = normalize_file_name(name)
                if normalized is None:
                    continue
                try:
                    body = bytes(value)
                except (TypeError, ValueError):
                    continue
                if len(body) <= FILE_SPECS[normalized]["max_bytes"]:
                    self.files[normalized] = body

    def reset(self) -> None:
        self.files.clear()
        self.pending = None

    def normalize_name(self, value: Any) -> Optional[str]:
        return normalize_file_name(value)

    def max_bytes(self, name: Any) -> int:
        normalized = normalize_file_name(name)
        return FILE_SPECS[normalized]["max_bytes"] if normalized else 0

    def read(self, name: Any) -> Optional[bytes]:
        normalized = normalize_file_name(name)
        if normalized is None:
            return None
        if normalized in self.files:
            return bytes(self.files[normalized])
        fallback = FILE_SPECS[normalized]["default"]
        return fallback.encode("utf-8") if fallback is not None else None

    def list(self) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for name in FILE_ORDER:
            body = self.read(name)
            if body is None:
                continue
            result.append({
                "name": name,
                # The reference firmware serialises size as a JSON string.
                "size": str(len(body)),
                "checksum": sha1_hex(body),
            })
        return result

    def begin_write(self, name: Any) -> bool:
        normalized = normalize_file_name(name)
        self.pending = None
        if normalized is None:
            return False
        self.pending = _PendingWrite(normalized, [])
        return True

    def write(self, value: Any) -> bool:
        if self.pending is None:
            return False
        try:
            body = bytes(value)
        except (TypeError, ValueError):
            self.pending.failed = True
            return False
        if self.pending.size + len(body) > FILE_SPECS[self.pending.name]["max_bytes"]:
            self.pending.failed = True
            return False
        if body:
            self.pending.chunks.append(body)
        self.pending.size += len(body)
        return True

    def finish_write(self) -> bool:
        if self.pending is None or self.pending.failed:
            self.pending = None
            return False
        pending = self.pending
        self.files[pending.name] = b"".join(pending.chunks)
        self.pending = None
        return True

    def abort_write(self) -> None:
        self.pending = None

    def delete(self, name: Any) -> bool:
        normalized = normalize_file_name(name)
        if normalized is None:
            return False
        self.pending = None
        self.files.pop(normalized, None)
        # Erasing an empty firmware slot succeeds; keymap.json then falls back
        # to the built-in default on the next read/list operation.
        return True

    def write_binary_chunk(self, name: Any, data: Any, completed: bool = False) -> tuple[bool, int]:
        normalized = normalize_file_name(name)
        if normalized is None:
            return False, 0
        if self.pending is None or self.pending.name != normalized:
            if not self.begin_write(normalized):
                return False, 0
        try:
            body = strict_base64(data if data is not None else "")
        except ValueError:
            self.abort_write()
            return False, 0
        if not self.write(body):
            self.abort_write()
            return False, 0
        if completed and not self.finish_write():
            return False, 0
        return True, len(body)

    def read_binary(self, name: Any, offset: int = 0, length: int = READ_CHUNK) -> Optional[dict[str, Any]]:
        body = self.read(name)
        if body is None:
            return None
        safe_offset = offset if isinstance(offset, int) and offset >= 0 else 0
        safe_length = length if isinstance(length, int) and length >= 0 else READ_CHUNK
        safe_length = min(safe_length, READ_CHUNK)
        start = min(safe_offset, len(body))
        end = min(start + safe_length, len(body))
        return {"total_size": len(body), "data": base64.b64encode(body[start:end]).decode("ascii")}

    def snapshot(self) -> dict[str, Any]:
        pending = None
        if self.pending is not None:
            pending = {"name": self.pending.name, "size": self.pending.size}
        return {"files": self.list(), "pending": pending}


__all__ = [
    "DEFAULT_KEYMAP",
    "FILE_ORDER",
    "FILE_SPECS",
    "MAX_FILE_NAME",
    "READ_CHUNK",
    "MicroFileStore",
    "json_bytes",
    "normalize_file_name",
    "sha1_hex",
    "strict_base64",
]
