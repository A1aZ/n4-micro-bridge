"""User-mode client for the Codex Micro bridge sideband contract.

The UMDF tree currently contains a *scaffold* for a private device interface
next to the HID collection.  This module intentionally keeps the boundary
boring and reviewable: it computes the IOCTL values from the constants in
``protocol/codexmicro_protocol.h``, opens an interface with ``CreateFileW`` and
moves fixed-size 64-byte reports with ``DeviceIoControl``.

Important architecture limitation: Microsoft's ``vhidmini2`` HID minidriver
sample documents that conventional custom IOCTL/WMI communication is not a
reliable user-mode transport for a HID minidriver. Consequently, a successful
legacy contract probe here is *not* proof that Codex can reach this UMDF HID
device. The active production direction is the dual-TLC HID route: the driver
exposes a Codex collection (FF00/Report ID 6) and a companion collection
(FF70/Report ID 7) on one device, with a shared HID read queue. The native
bridge uses hidapi on the FF70 collection. The custom GUID/IOCTL path remains
scaffold-only; an independent control device may be designed later, but is not
a prerequisite for the FF70 transport.

No driver is installed by this module.  Importing it and using ``--dry-run``
is safe on non-Windows hosts and on machines where the WDK/driver has not been
installed.  The N4 SDK is imported lazily, only for the optional experimental
live mode.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wintypes
import json
import math
import os
import struct
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from codexmicro_files import MicroFileStore, READ_CHUNK, json_bytes


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _finite_json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite JSON number")
    return number


# ---------------------------------------------------------------------------
# Values mirrored from driver/codexmicro-umdf/protocol/codexmicro_protocol.h
# ---------------------------------------------------------------------------

FILE_DEVICE_UNKNOWN = 0x00000022
METHOD_BUFFERED = 0
FILE_ANY_ACCESS = 0
FILE_READ_ACCESS = 0x0001
FILE_WRITE_ACCESS = 0x0002

MIRABOX_CODEX_MICRO_VID = 0x303A
MIRABOX_CODEX_MICRO_PID = 0x8360
MIRABOX_CODEX_MICRO_RELEASE = 0x0100
MIRABOX_CODEX_MICRO_USAGE_PAGE = 0xFF00
MIRABOX_CODEX_MICRO_USAGE = 0x0001
MIRABOX_CODEX_MICRO_REPORT_ID = 0x06
MIRABOX_CODEX_MICRO_REPORT_LENGTH = 64
MIRABOX_CODEX_MICRO_BODY_LENGTH = 63
MIRABOX_CODEX_MICRO_MESSAGE_TYPE = 0x02
MIRABOX_CODEX_MICRO_PAYLOAD_MAX = 61
MIRABOX_CODEX_INFO_LENGTH = 12  # packed USHORT/USHORT/USHORT/USHORT/UCHAR/UCHAR/2 bytes

BRIDGE_INTERFACE_GUID = "{7F5A2E91-1C0E-4D1E-9C2A-1A6B8E537044}"

# The values below are deliberately surfaced in probe/CLI JSON so callers do
# not mistake a matching GUID/GET_INFO response for an end-to-end HID path.
# See docs/codexmicro-sideband.md for the architecture decision and the
# Microsoft vhidmini2 sample note about custom IOCTL/WMI.
SIDEBAND_ARCHITECTURE = "hid-minidriver-scaffold"
SIDEBAND_OPERATIONAL = False
SIDEBAND_ARCHITECTURE_WARNING = (
    "Contract-only scaffold: Microsoft vhidmini2 documents that custom "
    "IOCTL/WMI communication is not a reliable HID-minidriver transport. A "
    "matching GUID/GET_INFO response does not prove Codex HID reachability; "
    "use the separately probed FF70 companion HID collection for production. "
    "An independent companion control device may be designed later, but is not "
    "a prerequisite for this transport."
)


def sideband_capability() -> dict[str, Any]:
    """Return explicit capability metadata shared by probe and CLI output."""

    return {
        "architecture": SIDEBAND_ARCHITECTURE,
        "operational": SIDEBAND_OPERATIONAL,
        "architectureWarning": SIDEBAND_ARCHITECTURE_WARNING,
    }


def ctl_code(device_type: int, function: int, method: int = METHOD_BUFFERED,
             access: int = FILE_ANY_ACCESS) -> int:
    """Compute the Windows ``CTL_CODE`` value used by the C header."""

    return (
        ((int(device_type) & 0xFFFF) << 16)
        | ((int(access) & 0x3) << 14)
        | ((int(function) & 0xFFF) << 2)
        | (int(method) & 0x3)
    )


IOCTL_MIRABOX_CODEX_PUSH_INPUT = ctl_code(
    FILE_DEVICE_UNKNOWN, 0x801, METHOD_BUFFERED, FILE_WRITE_ACCESS
)
IOCTL_MIRABOX_CODEX_READ_OUTPUT = ctl_code(
    FILE_DEVICE_UNKNOWN, 0x802, METHOD_BUFFERED, FILE_READ_ACCESS
)
IOCTL_MIRABOX_CODEX_GET_INFO = ctl_code(
    FILE_DEVICE_UNKNOWN, 0x803, METHOD_BUFFERED, FILE_READ_ACCESS
)
IOCTL_MIRABOX_CODEX_RESET = ctl_code(
    FILE_DEVICE_UNKNOWN, 0x804, METHOD_BUFFERED, FILE_WRITE_ACCESS
)

IOCTL_NAMES = {
    IOCTL_MIRABOX_CODEX_PUSH_INPUT: "IOCTL_MIRABOX_CODEX_PUSH_INPUT",
    IOCTL_MIRABOX_CODEX_READ_OUTPUT: "IOCTL_MIRABOX_CODEX_READ_OUTPUT",
    IOCTL_MIRABOX_CODEX_GET_INFO: "IOCTL_MIRABOX_CODEX_GET_INFO",
    IOCTL_MIRABOX_CODEX_RESET: "IOCTL_MIRABOX_CODEX_RESET",
}

# Win32 constants used by the small ctypes wrapper.  Keeping these explicit
# makes the code auditable without requiring pywin32.
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x00000080
FILE_FLAG_OVERLAPPED = 0x40000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_HANDLE = 6
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_OPERATION_ABORTED = 995
ERROR_IO_PENDING = 997
ERROR_NO_MORE_ITEMS = 259

WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
INFINITE = 0xFFFFFFFF

DIGCF_PRESENT = 0x00000002
DIGCF_DEVICEINTERFACE = 0x00000010


class SidebandError(RuntimeError):
    """Base class for recoverable sideband errors."""


class SidebandUnavailable(SidebandError):
    """The host cannot use the sideband (wrong OS or no installed device)."""


class SidebandTimeout(SidebandError):
    """A bounded sideband operation did not complete before its deadline."""


class ReportValidationError(ValueError):
    """A report is not exactly the 64-byte Codex Micro shape."""


class SidebandIoError(SidebandError):
    """A Win32 call failed, retaining the numeric error for diagnostics."""

    def __init__(self, operation: str, error_code: int, message: str = "") -> None:
        self.operation = operation
        self.error_code = int(error_code)
        detail = message or f"WinError {self.error_code}"
        super().__init__(f"{operation} failed ({detail})")


def parse_hex_bytes(value: str) -> bytes:
    """Parse ``41 43 4b``/``41434b`` text without silently dropping junk."""

    if not isinstance(value, str):
        raise TypeError("hex report must be a string")
    text = value.strip()
    for separator in (" ", "\t", "\r", "\n", ",", ":", "-"):
        text = text.replace(separator, "")
    if text.lower().startswith("0x"):
        text = text[2:]
    if not text:
        return b""
    if len(text) % 2:
        raise ValueError("hex value must contain complete bytes")
    try:
        return bytes.fromhex(text)
    except ValueError as exc:
        raise ValueError(f"invalid hex value: {exc}") from exc


def validate_report(value: Any, *, report_id: int = MIRABOX_CODEX_MICRO_REPORT_ID,
                    require_report_id: bool = True) -> bytes:
    """Return a report after enforcing the protocol's exact 64-byte shape.

    The sideband contract carries the HID report ID in byte zero.  A caller
    that deliberately works with a 63-byte body must add the ID itself; doing
    so here avoids accidentally sending a malformed request to the UMDF
    queue.  ``require_report_id=False`` is useful only for low-level tests.
    """

    if isinstance(value, str):
        value = parse_hex_bytes(value)
    elif isinstance(value, memoryview):
        value = value.tobytes()
    elif isinstance(value, bytearray):
        value = bytes(value)
    elif not isinstance(value, bytes):
        try:
            value = bytes(value)
        except (TypeError, ValueError) as exc:
            raise ReportValidationError(f"unsupported report type: {type(value).__name__}") from exc
    if len(value) != MIRABOX_CODEX_MICRO_REPORT_LENGTH:
        raise ReportValidationError(
            f"Codex Micro report must be exactly {MIRABOX_CODEX_MICRO_REPORT_LENGTH} bytes; got {len(value)}"
        )
    if require_report_id and value[0] != (int(report_id) & 0xFF):
        raise ReportValidationError(
            f"Codex Micro report ID must be 0x{int(report_id) & 0xFF:02x}; got 0x{value[0]:02x}"
        )
    return bytes(value)


def encode_micro_message(message: Mapping[str, Any]) -> list[bytes]:
    """Encode one JSON-RPC-ish message into 64-byte HID input reports."""

    if not isinstance(message, Mapping):
        raise TypeError("Micro message must be a JSON object")
    try:
        payload = (json.dumps(
            dict(message), ensure_ascii=False, separators=(",", ":"),
            allow_nan=False,
        ) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"message is not JSON encodable: {exc}") from exc
    reports: list[bytes] = []
    for offset in range(0, len(payload), MIRABOX_CODEX_MICRO_PAYLOAD_MAX):
        chunk = payload[offset : offset + MIRABOX_CODEX_MICRO_PAYLOAD_MAX]
        report = bytearray(MIRABOX_CODEX_MICRO_REPORT_LENGTH)
        report[0] = MIRABOX_CODEX_MICRO_REPORT_ID
        report[1] = MIRABOX_CODEX_MICRO_MESSAGE_TYPE
        report[2] = len(chunk)
        report[3 : 3 + len(chunk)] = chunk
        reports.append(bytes(report))
    # JSON objects are never empty, but retain a defensive fallback if a
    # custom encoder is introduced later.
    if not reports:
        reports.append(bytes([MIRABOX_CODEX_MICRO_REPORT_ID,
                              MIRABOX_CODEX_MICRO_MESSAGE_TYPE, 0]) + bytes(61))
    return reports


class MicroMessageDecoder:
    """Incremental decoder for the 64-byte Micro JSON framing."""

    def __init__(self, limit: int = 16384) -> None:
        if not isinstance(limit, int) or limit < 1:
            raise ValueError("decoder limit must be a positive integer")
        self.limit = limit
        self._buffer = bytearray()

    def reset(self) -> None:
        self._buffer.clear()

    def feed(self, report: Any) -> list[dict[str, Any]]:
        raw = validate_report(report)
        if raw[1] != MIRABOX_CODEX_MICRO_MESSAGE_TYPE or raw[2] > MIRABOX_CODEX_MICRO_PAYLOAD_MAX:
            self.reset()
            raise ReportValidationError("invalid Micro message framing")
        count = raw[2]
        self._buffer.extend(raw[3 : 3 + count])
        if len(self._buffer) > self.limit:
            self.reset()
            raise ReportValidationError("Micro message exceeds decoder limit")
        return self._extract_objects()

    def _extract_objects(self) -> list[dict[str, Any]]:
        # JSON structural bytes are ASCII, so scanning the UTF-8 byte buffer is
        # safe even when a multi-byte code point straddles two HID reports.
        data = bytes(self._buffer)
        start = -1
        depth = 0
        quoted = False
        escaped = False
        consumed = 0
        result: list[dict[str, Any]] = []
        try:
            for index, char in enumerate(data):
                if start < 0:
                    if char in (9, 10, 13, 32):
                        consumed = index + 1
                        continue
                    if char != ord("{"):
                        raise ValueError("expected JSON object")
                    start = index
                    depth = 1
                    continue
                if quoted:
                    if escaped:
                        escaped = False
                    elif char == ord("\\"):
                        escaped = True
                    elif char == ord('"'):
                        quoted = False
                elif char == ord('"'):
                    quoted = True
                elif char in (ord("{"), ord("[")):
                    depth += 1
                elif char in (ord("}"), ord("]")):
                    depth -= 1
                    if depth < 0:
                        raise ValueError("unbalanced JSON")
                    if depth == 0:
                        parsed = json.loads(data[start : index + 1].decode("utf-8"),
                                            parse_constant=_reject_json_constant, parse_float=_finite_json_float)
                        if not isinstance(parsed, dict):
                            raise ValueError("Micro message must be a JSON object")
                        result.append(parsed)
                        consumed = index + 1
                        start = -1
            # A complete object followed by malformed trailing data should be
            # rejected on the next feed, matching the JS decoder behavior.
            self._buffer = bytearray(data[consumed:])
            return result
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            self.reset()
            raise


@dataclass(frozen=True)
class SidebandInfo:
    """Packed ``MIRABOX_CODEX_INFO`` returned by GET_INFO."""

    vendor_id: int
    product_id: int
    release: int
    usage_page: int
    report_id: int
    report_length: int

    @classmethod
    def from_bytes(cls, value: bytes) -> "SidebandInfo":
        if len(value) < MIRABOX_CODEX_INFO_LENGTH:
            raise SidebandError(
                f"GET_INFO returned {len(value)} bytes; expected {MIRABOX_CODEX_INFO_LENGTH}"
            )
        vendor, product, release, usage_page, report_id, report_length, _reserved = struct.unpack(
            "<HHHHBB2s", bytes(value[:MIRABOX_CODEX_INFO_LENGTH])
        )
        return cls(vendor, product, release, usage_page, report_id, report_length)

    def as_dict(self) -> dict[str, int]:
        return {
            "vendorId": self.vendor_id,
            "productId": self.product_id,
            "release": self.release,
            "usagePage": self.usage_page,
            "reportId": self.report_id,
            "reportLength": self.report_length,
        }

    def compatibility_errors(self) -> list[str]:
        expected = {
            "vendorId": MIRABOX_CODEX_MICRO_VID,
            "productId": MIRABOX_CODEX_MICRO_PID,
            "release": MIRABOX_CODEX_MICRO_RELEASE,
            "usagePage": MIRABOX_CODEX_MICRO_USAGE_PAGE,
            "reportId": MIRABOX_CODEX_MICRO_REPORT_ID,
            "reportLength": MIRABOX_CODEX_MICRO_REPORT_LENGTH,
        }
        actual = self.as_dict()
        return [
            f"{key}=0x{actual[key]:x} (expected 0x{value:x})"
            for key, value in expected.items()
            if actual[key] != value
        ]

    @property
    def compatible(self) -> bool:
        return not self.compatibility_errors()


# ctypes declarations are defined even on non-Windows; loading the DLLs is
# deferred to _WindowsApi.__init__, which keeps dry-run/unit-test imports safe.
class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", wintypes.BYTE * 8),
    ]


class _SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("InterfaceClassGuid", _GUID),
        ("Flags", wintypes.DWORD),
        ("Reserved", ctypes.c_void_p),
    ]


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


def _guid_from_text(value: str = BRIDGE_INTERFACE_GUID) -> _GUID:
    """Parse the fixed bridge GUID without relying on uuid's platform ABI."""

    text = value.strip().strip("{}")
    parts = text.split("-")
    if [len(part) for part in parts] != [8, 4, 4, 4, 12]:
        raise ValueError(f"invalid bridge GUID: {value}")
    try:
        # Assign the first three fields as their displayed numeric values;
        # ctypes applies the native little-endian memory layout. Data4 stays
        # in the byte order written in the GUID string.
        data4 = bytes.fromhex(parts[3] + parts[4])
        return _GUID(
            int(parts[0], 16), int(parts[1], 16), int(parts[2], 16),
            (wintypes.BYTE * 8).from_buffer_copy(data4),
        )
    except ValueError as exc:
        raise ValueError(f"invalid bridge GUID: {value}") from exc


class _WindowsApi:
    """Small, testable ctypes binding for kernel32/setupapi."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise SidebandUnavailable(
                "Codex Micro sideband requires Windows; --dry-run is available on this host"
            )
        try:
            self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self.setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
        except (AttributeError, OSError) as exc:
            raise SidebandUnavailable(f"Windows API unavailable: {exc}") from exc
        self._bind_functions()

    def _bind_functions(self) -> None:
        k = self.kernel32
        s = self.setupapi
        k.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        k.CreateFileW.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL
        k.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        k.CreateEventW.restype = wintypes.HANDLE
        k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(_OVERLAPPED)]
        k.CancelIoEx.restype = wintypes.BOOL
        k.DeviceIoControl.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(_OVERLAPPED),
        ]
        k.DeviceIoControl.restype = wintypes.BOOL
        k.GetOverlappedResult.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(_OVERLAPPED), ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
        ]
        k.GetOverlappedResult.restype = wintypes.BOOL

        s.SetupDiGetClassDevsW.argtypes = [
            ctypes.POINTER(_GUID), wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD,
        ]
        s.SetupDiGetClassDevsW.restype = wintypes.HANDLE
        s.SetupDiEnumDeviceInterfaces.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(_GUID), wintypes.DWORD,
            ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA),
        ]
        s.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
        s.SetupDiGetDeviceInterfaceDetailW.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA), ctypes.c_void_p,
            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
        ]
        s.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
        s.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
        s.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL

    @staticmethod
    def _error(operation: str, code: Optional[int] = None) -> SidebandIoError:
        error_code = int(ctypes.get_last_error() if code is None else code)
        try:
            detail = ctypes.FormatError(error_code).strip()
        except (AttributeError, OSError):
            detail = f"WinError {error_code}"
        return SidebandIoError(operation, error_code, detail)

    def enumerate_paths(self, guid_text: str = BRIDGE_INTERFACE_GUID) -> list[str]:
        guid = _guid_from_text(guid_text)
        info_set = self.setupapi.SetupDiGetClassDevsW(
            ctypes.byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE
        )
        if info_set in (None, INVALID_HANDLE_VALUE):
            error = ctypes.get_last_error()
            if error in (ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND):
                return []
            raise self._error("SetupDiGetClassDevsW", error)
        paths: list[str] = []
        try:
            index = 0
            while True:
                item = _SP_DEVICE_INTERFACE_DATA()
                item.cbSize = ctypes.sizeof(_SP_DEVICE_INTERFACE_DATA)
                ok = self.setupapi.SetupDiEnumDeviceInterfaces(
                    info_set, None, ctypes.byref(guid), index, ctypes.byref(item)
                )
                if not ok:
                    error = ctypes.get_last_error()
                    if error == ERROR_NO_MORE_ITEMS:
                        break
                    raise self._error("SetupDiEnumDeviceInterfaces", error)
                required = wintypes.DWORD(0)
                self.setupapi.SetupDiGetDeviceInterfaceDetailW(
                    info_set, ctypes.byref(item), None, 0, ctypes.byref(required), None
                )
                error = ctypes.get_last_error()
                if error != ERROR_INSUFFICIENT_BUFFER or required.value == 0:
                    raise self._error("SetupDiGetDeviceInterfaceDetailW(size)", error)
                buffer = ctypes.create_string_buffer(required.value)
                # cbSize is DWORD; DevicePath starts at byte offset 4 even on
                # x64 (the struct's trailing WCHAR array is unaligned).
                cb_size = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
                ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD))[0] = cb_size
                ok = self.setupapi.SetupDiGetDeviceInterfaceDetailW(
                    info_set, ctypes.byref(item), ctypes.cast(buffer, ctypes.c_void_p),
                    required.value, ctypes.byref(required), None
                )
                if not ok:
                    raise self._error("SetupDiGetDeviceInterfaceDetailW", ctypes.get_last_error())
                path_address = ctypes.addressof(buffer) + ctypes.sizeof(wintypes.DWORD)
                path = ctypes.wstring_at(path_address)
                if path:
                    paths.append(path)
                index += 1
        finally:
            self.setupapi.SetupDiDestroyDeviceInfoList(info_set)
        return paths

    def create_file(self, path: str) -> Any:
        handle = self.kernel32.CreateFileW(
            path,
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OVERLAPPED,
            None,
        )
        if handle in (None, INVALID_HANDLE_VALUE):
            raise self._error(f"CreateFileW({path})")
        return handle

    def close_handle(self, handle: Any) -> None:
        if handle in (None, INVALID_HANDLE_VALUE):
            return
        # Best effort cancellation: a READ_OUTPUT request may be waiting in
        # the driver's manual queue.  CloseHandle still owns final cleanup.
        try:
            self.kernel32.CancelIoEx(handle, None)
        except (AttributeError, OSError):
            pass
        self.kernel32.CloseHandle(handle)

    def device_io_control(self, handle: Any, code: int, input_data: bytes = b"",
                          output_size: int = 0, timeout_ms: Optional[int] = 5000) -> bytes:
        if output_size < 0:
            raise ValueError("output_size cannot be negative")
        in_buffer = ctypes.create_string_buffer(input_data) if input_data else None
        out_buffer = (ctypes.c_ubyte * output_size)() if output_size else None
        in_ptr = ctypes.cast(in_buffer, ctypes.c_void_p) if in_buffer is not None else None
        out_ptr = ctypes.cast(out_buffer, ctypes.c_void_p) if out_buffer is not None else None
        event = self.kernel32.CreateEventW(None, True, False, None)
        if event in (None, 0, INVALID_HANDLE_VALUE):
            raise self._error("CreateEventW")
        overlapped = _OVERLAPPED()
        overlapped.hEvent = event
        returned = wintypes.DWORD(0)
        try:
            ok = self.kernel32.DeviceIoControl(
                handle, int(code), in_ptr, len(input_data), out_ptr, output_size,
                ctypes.byref(returned), ctypes.byref(overlapped),
            )
            if not ok:
                error = ctypes.get_last_error()
                if error != ERROR_IO_PENDING:
                    raise self._error(IOCTL_NAMES.get(code, f"DeviceIoControl(0x{code:x})"), error)
                wait_ms = INFINITE if timeout_ms is None else max(0, int(timeout_ms))
                waited = self.kernel32.WaitForSingleObject(event, wait_ms)
                if waited == WAIT_TIMEOUT:
                    self.kernel32.CancelIoEx(handle, ctypes.byref(overlapped))
                    # Wait for the driver's cancellation completion before the
                    # event is destroyed.  This avoids dangling OVERLAPPED data.
                    self.kernel32.WaitForSingleObject(event, INFINITE)
                    raise SidebandTimeout(
                        f"{IOCTL_NAMES.get(code, 'DeviceIoControl')} timed out after {wait_ms} ms"
                    )
                if waited != WAIT_OBJECT_0:
                    raise self._error("WaitForSingleObject", waited)
                if not self.kernel32.GetOverlappedResult(
                    handle, ctypes.byref(overlapped), ctypes.byref(returned), False
                ):
                    raise self._error(IOCTL_NAMES.get(code, "GetOverlappedResult"), ctypes.get_last_error())
            count = min(int(returned.value), output_size)
            return bytes(out_buffer[:count]) if out_buffer is not None else b""
        finally:
            self.kernel32.CloseHandle(event)


class SidebandClient:
    """Thread-safe client for the four custom bridge IOCTLs."""

    def __init__(self, path: Optional[str] = None, *, api: Any = None,
                 timeout_ms: int = 5000) -> None:
        if timeout_ms < 0:
            raise ValueError("timeout_ms cannot be negative")
        self.path = path
        self.timeout_ms = int(timeout_ms)
        self.api = api
        self.handle: Any = None
        self._lock = threading.RLock()

    @property
    def opened(self) -> bool:
        return self.handle not in (None, INVALID_HANDLE_VALUE)

    def _api(self) -> Any:
        if self.api is None:
            self.api = _WindowsApi()
        return self.api

    def resolve_paths(self) -> list[str]:
        if self.path:
            return [self.path]
        return list(self._api().enumerate_paths())

    def open(self) -> "SidebandClient":
        with self._lock:
            if self.opened:
                return self
            paths = self.resolve_paths()
            if not paths:
                raise SidebandUnavailable(
                    "Codex Micro sideband interface was not found. Build/install the unsigned scaffold first; "
                    "this client does not install drivers. Use --dry-run without hardware."
                )
            last_error: Optional[Exception] = None
            for candidate in paths:
                try:
                    self.handle = self._api().create_file(candidate)
                    self.path = candidate
                    return self
                except Exception as exc:  # try another interface instance
                    last_error = exc
            if isinstance(last_error, SidebandError):
                raise last_error
            raise SidebandUnavailable(f"unable to open sideband interface: {last_error}")

    def close(self) -> None:
        with self._lock:
            if self.opened:
                self._api().close_handle(self.handle)
            self.handle = None

    def __enter__(self) -> "SidebandClient":
        return self.open()

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()

    def _ioctl(self, code: int, *, input_data: bytes = b"", output_size: int = 0,
               timeout_ms: Optional[int] = None) -> bytes:
        with self._lock:
            self.open()
            timeout = self.timeout_ms if timeout_ms is None else timeout_ms
            return self._api().device_io_control(
                self.handle, code, input_data, output_size, timeout
            )

    def get_info(self) -> SidebandInfo:
        data = self._ioctl(IOCTL_MIRABOX_CODEX_GET_INFO, output_size=MIRABOX_CODEX_INFO_LENGTH)
        return SidebandInfo.from_bytes(data)

    def reset(self) -> None:
        self._ioctl(IOCTL_MIRABOX_CODEX_RESET)

    def push_input(self, report: Any) -> None:
        valid = validate_report(report)
        self._ioctl(IOCTL_MIRABOX_CODEX_PUSH_INPUT, input_data=valid)

    def read_output(self, *, timeout_ms: Optional[int] = None) -> bytes:
        data = self._ioctl(
            IOCTL_MIRABOX_CODEX_READ_OUTPUT,
            output_size=MIRABOX_CODEX_MICRO_REPORT_LENGTH,
            timeout_ms=timeout_ms,
        )
        return validate_report(data)

    def check(self) -> SidebandInfo:
        info = self.get_info()
        errors = info.compatibility_errors()
        if errors:
            raise SidebandError("sideband identity mismatch: " + ", ".join(errors))
        return info


def _probe_error_status(error: BaseException) -> str:
    """Map a sideband exception to a stable, UI-friendly probe status."""

    code = getattr(error, "error_code", None)
    if code in (ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND):
        return "interface-not-found"
    if code == ERROR_ACCESS_DENIED:
        return "access-denied"
    if isinstance(error, SidebandTimeout):
        return "timeout"
    if isinstance(error, SidebandUnavailable):
        if os.name != "nt":
            return "unsupported-platform"
        return "interface-not-found"
    return "error"


def probe_sideband(path: Optional[str] = None, *, api: Any = None,
                   timeout_ms: int = 5000) -> dict[str, Any]:
    """Perform a read-only sideband probe and return a stable result object.

    The probe only enumerates the private interface, opens it, and issues
    ``GET_INFO``.  It never resets queues, pushes input, reads output, installs
    a driver, or changes test-signing/system state.  Returning a structured
    result (including expected failures) lets the WebUI distinguish an absent
    interface from an ACL problem or an identity mismatch without scraping
    human-oriented stderr text.

    ``api`` exists for unit tests and embedding; normal callers should leave it
    unset so the Windows ctypes implementation is selected lazily.
    """

    if timeout_ms < 0:
        raise ValueError("timeout_ms cannot be negative")
    result: dict[str, Any] = {
        "ok": False,
        "status": "unknown",
        "message": "",
        "platform": sys.platform,
        "driverTouched": False,
        "interfaceFound": False,
        "identityCompatible": False,
        "paths": [],
        "path": path,
        "info": None,
        "compatibilityErrors": [],
        **sideband_capability(),
    }
    client = SidebandClient(path, api=api, timeout_ms=timeout_ms)
    try:
        try:
            paths = client.resolve_paths()
        except Exception as error:  # classify enumeration failures below
            result["status"] = _probe_error_status(error)
            result["message"] = str(error)
            return result
        result["paths"] = list(paths)
        if not paths:
            result["status"] = "unsupported-platform" if os.name != "nt" and api is None else "interface-not-found"
            result["message"] = (
                "Codex Micro sideband requires Windows"
                if result["status"] == "unsupported-platform"
                else "Codex Micro sideband interface was not found"
            )
            return result
        try:
            client.open()
            result["interfaceFound"] = True
            result["path"] = client.path
            info = client.get_info()
        except Exception as error:  # classify open/GET_INFO failures
            result["status"] = _probe_error_status(error)
            result["message"] = str(error)
            return result
        result["info"] = info.as_dict()
        errors = info.compatibility_errors()
        result["compatibilityErrors"] = errors
        result["identityCompatible"] = not errors
        if errors:
            result["status"] = "identity-mismatch"
            result["message"] = "sideband identity mismatch: " + ", ".join(errors)
            return result
        result["ok"] = True
        result["status"] = "compatible"
        result["message"] = (
            "Transport contract matched; this HID-minidriver sideband is "
            "scaffold-only and does not prove Codex HID reachability"
        )
        return result
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Optional N4 -> sideband runtime.  Imports the SDK only when live mode runs.
# ---------------------------------------------------------------------------


def _wire_event(event: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in event.items()
            if key not in {"synthetic", "delayMs", "meta"}}


_LIGHT_ALIASES: dict[str, tuple[str, ...]] = {
    "c": ("c", "color"),
    "b": ("b", "brightness"),
    "e": ("e", "effect"),
    "s": ("s", "speed"),
    "sk": ("sk",),
    "sa": ("sa",),
    "m": ("m", "magic"),
}
_LIGHT_EFFECT_NAMES = {
    "off", "solid", "snake", "rainbow", "breath", "gradient",
    "shallowBreath", "shallow_breath",
}


def _light_patch(value: Any) -> dict[str, Any]:
    """Validate and canonicalise one Micro lighting object.

    The device kit sends both the compact ``c/b/e/s`` spelling and the long
    ``color/brightness/effect/speed`` spelling (the latter is used by
    ``lights.preview``).  Keeping only canonical keys here makes the Python
    endpoint's state match the renderer and the JavaScript WebUI endpoint.
    """

    if not isinstance(value, Mapping):
        raise ValueError("Invalid light")
    out: dict[str, Any] = {}
    for field, names in _LIGHT_ALIASES.items():
        source = next((name for name in names if name in value), None)
        if source is None:
            continue
        item = value[source]
        if field == "c":
            if isinstance(item, str):
                text = item.strip()
                if text.startswith("#"):
                    text = text[1:]
                elif text.lower().startswith("0x"):
                    text = text[2:]
                if len(text) == 3 and all(char in "0123456789abcdefABCDEF" for char in text):
                    text = "".join(char * 2 for char in text)
                if len(text) not in (6, 8) or not all(char in "0123456789abcdefABCDEF" for char in text):
                    raise ValueError("Invalid light field: c")
                out[field] = int(text[:6], 16)
            else:
                if isinstance(item, bool) or not isinstance(item, (int, float)):
                    raise ValueError("Invalid light field: c")
                if int(item) != item or not 0 <= int(item) <= 0xFFFFFF:
                    raise ValueError("Invalid light field: c")
                out[field] = int(item)
        elif field == "e":
            if isinstance(item, str):
                text = item.strip()
                if text not in _LIGHT_EFFECT_NAMES and not text.isdigit():
                    raise ValueError("Invalid light field: e")
                number = int(text) if text.isdigit() else text
                if isinstance(number, int) and not 0 <= number <= 255:
                    raise ValueError("Invalid light field: e")
                out[field] = number
            elif isinstance(item, bool) or not isinstance(item, (int, float)):
                raise ValueError("Invalid light field: e")
            elif int(item) != item or not 0 <= int(item) <= 255:
                raise ValueError("Invalid light field: e")
            else:
                out[field] = int(item)
        else:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise ValueError(f"Invalid light field: {field}")
            if not float(item) == float(item) or not 0 <= float(item) <= 1:
                raise ValueError(f"Invalid light field: {field}")
            out[field] = float(item)
    return out


class _HostRpcError(ValueError):
    """Internal exception carrying the firmware-compatible JSON-RPC code."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = int(code)


class HostRpcEndpoint:
    """Small user-space responder for Codex's initial Micro RPC messages."""

    def __init__(self, on_visual: Optional[Callable[[str, Any], None]] = None,
                 files: Optional[MicroFileStore] = None) -> None:
        self.on_visual = on_visual
        self.files = files if isinstance(files, MicroFileStore) else MicroFileStore()
        self.slots = [{"id": index, "c": 0, "b": 0, "e": 0, "s": 0}
                      for index in range(6)]
        self.lighting: dict[str, Any] = {"keys": {}, "ambient": {}}

    def handle(self, request: Any) -> Optional[dict[str, Any]]:
        if not isinstance(request, Mapping) or not isinstance(request.get("method"), str):
            return self._reply(request, error={"code": -32600, "message": "Invalid request"})
        method = str(request["method"])
        try:
            if method == "sys.version":
                result: Any = {"version": "0.1.0-n4-emulator"}
            elif method == "device.status":
                result = {
                    "version": "0.1.0-n4-emulator", "profile_index": 0,
                    "layer_index": 0, "battery": 100, "is_charging": False,
                }
            elif method == "v.oai.thstatus":
                params = request.get("params")
                if not isinstance(params, list):
                    raise ValueError("Expected light array")
                next_slots = [dict(slot) for slot in self.slots]
                for value in params:
                    if not isinstance(value, Mapping):
                        raise ValueError("Invalid slot")
                    ident = value.get("id")
                    if not isinstance(ident, int) or not 0 <= ident <= 5:
                        raise ValueError("Invalid slot")
                    next_slots[ident].update(_light_patch(value))
                self.slots = next_slots
                if self.on_visual:
                    self.on_visual(method, params)
                result = {"ok": True}
            elif method == "v.oai.rgbcfg":
                params = request.get("params")
                if not isinstance(params, Mapping):
                    raise ValueError("Invalid lighting")
                next_lighting = {
                    "keys": dict(self.lighting.get("keys", {})),
                    "ambient": dict(self.lighting.get("ambient", {})),
                }
                for side in ("keys", "ambient"):
                    if side in params and params[side] is not None:
                        next_lighting[side].update(_light_patch(params[side]))
                self.lighting = next_lighting
                if self.on_visual:
                    self.on_visual(method, params)
                result = {"ok": True}
            elif method == "lights.preview":
                params = request.get("params")
                if not isinstance(params, Mapping):
                    raise ValueError("Invalid lighting")
                next_lighting = {
                    "keys": dict(self.lighting.get("keys", {})),
                    "ambient": dict(self.lighting.get("ambient", {})),
                }
                if "backlight" in params and params["backlight"] is not None:
                    next_lighting["keys"].update(_light_patch(params["backlight"]))
                if "underglow" in params and params["underglow"] is not None:
                    next_lighting["ambient"].update(_light_patch(params["underglow"]))
                self.lighting = next_lighting
                if self.on_visual:
                    self.on_visual(method, params)
                result = {"ok": True}
            elif method == "fs.list":
                result = self.files.list()
            elif method == "fs.readbin":
                params = request.get("params")
                if not isinstance(params, Mapping):
                    raise _HostRpcError("File does not exist", -2)
                offset = self._uint(params.get("offset"), 0)
                length = self._uint(params.get("len"), READ_CHUNK)
                result = self.files.read_binary(params.get("file"), offset, length)
                if result is None:
                    raise _HostRpcError("File does not exist", -2)
            elif method == "fs.read":
                params = request.get("params")
                if not isinstance(params, Mapping):
                    raise _HostRpcError("File does not exist", -2)
                body = self.files.read(params.get("file"))
                if body is None:
                    raise _HostRpcError("File does not exist", -2)
                # A corrupt flash slot must have the same firmware-facing
                # error as malformed JSON (``-2``), rather than escaping to
                # the generic JSON-RPC ``-32602`` handler as a
                # UnicodeDecodeError.
                try:
                    text = body.decode("utf-8", errors="strict").strip()
                except UnicodeDecodeError as exc:
                    raise _HostRpcError("File does not exist", -2) from exc
                if not text.startswith(("{", "[")):
                    raise _HostRpcError("File does not exist", -2)
                try:
                    result = json.loads(text, parse_constant=_reject_json_constant, parse_float=_finite_json_float)
                except ValueError as exc:
                    raise _HostRpcError("File does not exist", -2) from exc
            elif method == "fs.writebin":
                params = request.get("params")
                if not isinstance(params, Mapping) or "file" not in params:
                    self.files.abort_write()
                    raise _HostRpcError("write failed", -3)
                ok, written = self.files.write_binary_chunk(
                    params.get("file"), params.get("data", ""),
                    params.get("completed") is True or (
                        type(params.get("completed")) in (int, float) and params["completed"] == 1
                    ),
                )
                if not ok:
                    raise _HostRpcError("write failed", -3)
                result = {"data_written": written}
            elif method == "fs.write":
                params = request.get("params")
                if not isinstance(params, Mapping) or "file" not in params or "data" not in params:
                    raise _HostRpcError("params are not correct", -1)
                file_name = self.files.normalize_name(params.get("file"))
                if file_name is None or not self.files.begin_write(file_name):
                    raise _HostRpcError("write failed", -3)
                try:
                    if not self.files.write(json_bytes(params.get("data"))) or not self.files.finish_write():
                        raise ValueError("write failed")
                except Exception as exc:
                    self.files.abort_write()
                    raise _HostRpcError(str(exc) or "write failed", -3) from exc
                result = {"ok": True}
            elif method == "fs.delete":
                params = request.get("params")
                file_name = params.get("file") if isinstance(params, Mapping) else None
                if not self.files.delete(file_name):
                    raise _HostRpcError("File does not exist", -2)
                result = {"ok": True}
            elif method in {"fs.rmdir", "fs.txcommit"}:
                result = {"ok": True}
            elif method == "fs.txbegin":
                result = {"tx": 1}
            elif method == "ui.active_screen":
                result = {"screen_name": "home"}
            elif method in {"appmgr.list_active", "appmgr.list_installed"}:
                result = []
            elif method == "host.focused_app":
                result = {"ok": True}
            elif method in {"ui.home_accent_color", "mp.write_info",
                            "mp.write_artwork", "sys.selftest"}:
                result = {"ok": True}
            else:
                return self._reply(request, error={"code": -32601, "message": "Method not found"})
            return self._reply(request, result=result)
        except Exception as exc:
            return self._reply(request, error={"code": getattr(exc, "code", -32602), "message": str(exc)})

    @staticmethod
    def _uint(value: Any, fallback: int) -> int:
        if type(value) not in (int, float):
            return fallback
        if isinstance(value, float) and not math.isfinite(value):
            return fallback
        if value >= 0 and int(value) <= 0xffffffff:
            return int(value)
        return fallback

    @staticmethod
    def _reply(request: Any, *, result: Any = None, error: Any = None) -> Optional[dict[str, Any]]:
        if not isinstance(request, Mapping) or "id" not in request:
            return None
        response: dict[str, Any] = {"id": request["id"]}
        if error is not None:
            response["error"] = error
        else:
            response["result"] = result
        return response


class SidebandRuntime:
    """Connect an N4 dispatcher to either sideband or companion transport.

    The transport is intentionally duck-typed: the legacy ``SidebandClient``
    exposes ``push_input``/``read_output``, and
    ``codexmicro_companion.CompanionHidTransport`` provides the same aliases
    while translating report IDs at the HID collection boundary.
    """

    def __init__(self, client: Any, *, release_ms: int = 100,
                 on_host_message: Optional[Callable[[Mapping[str, Any]], None]] = None,
                 on_visual: Optional[Callable[[str, Any], None]] = None,
                 output_poll_ms: int = 250,
                 status_stream: Any = sys.stderr) -> None:
        self.client = client
        self.release_ms = max(0, int(release_ms))
        self.on_host_message = on_host_message
        self.endpoint = HostRpcEndpoint(on_visual=on_visual)
        self.decoder = MicroMessageDecoder()
        self.output_poll_ms = max(1, int(output_poll_ms))
        self.status_stream = status_stream
        self.stop_event = threading.Event()
        self._write_lock = threading.RLock()
        self._timers: dict[Any, threading.Timer] = {}
        self._output_thread: Optional[threading.Thread] = None

    def _status(self, message: str) -> None:
        if self.status_stream is not None:
            print(message, file=self.status_stream, flush=True)

    def push_message(self, message: Mapping[str, Any]) -> None:
        for report in encode_micro_message(message):
            with self._write_lock:
                self.client.push_input(report)

    def handle_record(self, record: Mapping[str, Any]) -> None:
        decoded = record.get("decoded") if isinstance(record, Mapping) else None
        events = record.get("microEvents") if isinstance(record, Mapping) else None
        if not isinstance(events, list):
            return
        source_key = (decoded or {}).get("hardwareCode") if isinstance(decoded, Mapping) else None
        for event in events:
            if not isinstance(event, Mapping):
                continue
            wire = _wire_event(event)
            if event.get("synthetic"):
                delay_ms = int(event.get("delayMs", self.release_ms))
                old = self._timers.pop(source_key, None)
                if old is not None:
                    old.cancel()
                timer = threading.Timer(max(0, delay_ms) / 1000.0,
                                         self._push_synthetic, args=(source_key, wire))
                timer.daemon = True
                self._timers[source_key] = timer
                timer.start()
            else:
                self.push_message(wire)
                if (isinstance(decoded, Mapping)
                        and decoded.get("kind") == "knob_press"
                        and decoded.get("act") == 0):
                    pending = self._timers.pop(source_key, None)
                    if pending is not None:
                        pending.cancel()

    def _push_synthetic(self, source_key: Any, wire: Mapping[str, Any]) -> None:
        self._timers.pop(source_key, None)
        if not self.stop_event.is_set():
            self.push_message(wire)

    def _output_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                report = self.client.read_output(timeout_ms=self.output_poll_ms)
            except SidebandTimeout:
                continue
            except Exception as exc:
                if not self.stop_event.is_set():
                    self._status(f"[sideband] output read stopped: {exc}")
                return
            # hidapi returns an empty list on a bounded timeout; the companion
            # adapter maps that to None so it can share this polling loop with
            # SidebandClient, whose legacy DeviceIoControl path raises
            # SidebandTimeout instead.
            if report is None:
                continue
            try:
                messages = self.decoder.feed(report)
            except Exception as exc:
                self._status(f"[sideband] invalid Codex output report: {exc}")
                continue
            for message in messages:
                if self.on_host_message:
                    self.on_host_message(message)
                reply = self.endpoint.handle(message)
                if reply is not None:
                    try:
                        self.push_message(reply)
                    except Exception as exc:
                        self._status(f"[sideband] failed to send RPC reply: {exc}")

    def start_output_reader(self) -> None:
        if self._output_thread and self._output_thread.is_alive():
            return
        self.stop_event.clear()
        self._output_thread = threading.Thread(target=self._output_loop,
                                                name="codexmicro-output",
                                                daemon=True)
        self._output_thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        for timer in list(self._timers.values()):
            timer.cancel()
        self._timers.clear()
        # Closing the handle cancels a pending READ_OUTPUT in the Windows
        # wrapper.  The caller owns the client lifecycle, so do not close it
        # here; this method only asks the reader to stop.


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _json_line(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _dry_run(args: argparse.Namespace) -> int:
    reports: list[dict[str, Any]] = []
    for text in args.report_hex or []:
        try:
            value = validate_report(parse_hex_bytes(text))
        except (TypeError, ValueError) as exc:
            raise SidebandError(f"--report-hex: {exc}") from exc
        reports.append({"length": len(value), "reportId": value[0], "hex": value.hex(" ")})
    messages: list[dict[str, Any]] = []
    if args.message_json:
        try:
            parsed = json.loads(args.message_json)
        except json.JSONDecodeError as exc:
            raise SidebandError(f"--message-json is invalid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise SidebandError("--message-json must contain a JSON object")
        for value in encode_micro_message(parsed):
            messages.append({"length": len(value), "reportId": value[0], "hex": value.hex(" ")})
    if not reports and not messages:
        default = {"method": "v.oai.hid", "params": {"k": "ENC_CW", "act": 2}}
        for value in encode_micro_message(default):
            messages.append({"length": len(value), "reportId": value[0], "hex": value.hex(" ")})
    _json_line({
        "mode": "dry-run",
        "platform": sys.platform,
        "ioctl": {name: f"0x{code:08x}" for code, name in IOCTL_NAMES.items()},
        "reportLength": MIRABOX_CODEX_MICRO_REPORT_LENGTH,
        "reports": reports,
        "encodedMessages": messages,
        "driverTouched": False,
        **sideband_capability(),
    })
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codexmicro-bridge.py",
        description=(
            "Mirabox N4 -> Codex Micro bridge. The preferred runtime transport "
            "is the FF70/report-ID-7 companion HID collection; the legacy "
            "custom-IOCTL sideband remains a scaffold-only diagnostic path. "
            "This command never installs a driver."
        ),
        epilog=(
            "Without a built/installed virtual device, use --dry-run. Use "
            "--companion for the FF70 HID transport after installation. "
            "Legacy custom-IOCTL mutations still require the explicit "
            "--allow-scaffold-sideband acknowledgement."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="validate/encode offline; never loads Windows APIs or N4 SDK")
    mode.add_argument("--check-only", action="store_true",
                      help="open sideband and GET_INFO, then exit without changing queues")
    mode.add_argument("--reset", action="store_true",
                      help="send IOCTL_MIRABOX_CODEX_RESET and exit")
    mode.add_argument("--push-report", metavar="HEX",
                      help="send one exact 64-byte report (hex) and exit")
    mode.add_argument("--read-output", action="store_true",
                      help="read one 64-byte Codex output report and exit")
    parser.add_argument(
        "--allow-scaffold-sideband", action="store_true",
        help=(
            "explicitly opt in to the unverified HID-minidriver custom-IOCTL "
            "scaffold; this does not make the path supported"
        ),
    )
    parser.add_argument(
        "--companion", action="store_true",
        help=(
            "use the FF70/report-ID-7 companion HID collection instead of "
            "the legacy custom-IOCTL sideband"
        ),
    )
    parser.add_argument(
        "--companion-path", metavar="PATH",
        help="open this exact hidapi companion path (requires --companion)",
    )
    parser.add_argument("--sideband-path", metavar="PATH",
                        help="explicit custom interface path; otherwise enumerate the bridge GUID")
    parser.add_argument("--timeout-ms", type=int, default=5000,
                        help="IOCTL timeout in milliseconds (default: 5000; read-output may override)")
    parser.add_argument("--report-hex", action="append", default=[],
                        help="offline 64-byte report for --dry-run; may be repeated")
    parser.add_argument("--message-json",
                        help="offline JSON object to encode for --dry-run")
    parser.add_argument("--read-timeout-ms", type=int, default=1000,
                        help="bounded timeout for --read-output (default: 1000)")
    # Live N4 bridge options.  They are intentionally inert in dry-run/check.
    parser.add_argument("--sdk-path", help="StreamDock Python SDK src directory")
    parser.add_argument("--n4-path", help="exact N4 SDK device path")
    parser.add_argument("--n4-index", type=int, default=0,
                        help="matching N4 index (default: 0)")
    parser.add_argument("--input-profile", choices=("cpp", "python"), default="cpp",
                        help="N4 input-code profile (default: cpp)")
    parser.add_argument("--no-init", action="store_true",
                        help="skip N4 SDK initialization commands")
    parser.add_argument("--duration", type=float,
                        help="stop live bridge after this many seconds")
    parser.add_argument("--release-ms", type=int, default=100,
                        help="synthetic encoder release delay (default: 100)")
    parser.add_argument("--no-output-reader", action="store_true",
                        help="do not drain Codex output reports in live mode")
    parser.add_argument("--no-visuals", action="store_true",
                        help="do not render Codex thstatus/rgbcfg messages onto the N4")
    parser.add_argument("--visual-refresh-ms", type=int, default=250,
                        help="N4 visual upload/animation interval (default: 250 ms)")
    parser.add_argument("--visual-target", choices=("both", "screen", "keys"), default="both",
                        help="visual output destination (default: both)")
    parser.add_argument("--quiet", action="store_true", help="suppress status messages")
    return parser


def _run_n4_transport(args: argparse.Namespace, transport: Any) -> int:
    """Run the physical N4 bridge against an already-open report transport."""

    # Lazy import: --dry-run/check/probe modes never need Pillow or the N4 SDK.
    from n4_native_adapter import N4NativeAdapter, ReportDispatcher

    visual_output = None
    if not args.no_visuals:
        try:
            from n4_visual_output import N4VisualOutput

            visual_output = N4VisualOutput(
                adapter=None,  # attached to the adapter constructed below
                refresh_ms=args.visual_refresh_ms,
                target=args.visual_target,
                status_stream=None if args.quiet else sys.stderr,
            )
        except Exception:
            # Keep input forwarding usable when Pillow/the renderer is absent.
            if not args.quiet:
                print(
                    "[n4-visual] disabled (Pillow/renderer unavailable)",
                    file=sys.stderr,
                    flush=True,
                )
            visual_output = None

    dispatcher = ReportDispatcher(
        on_record=None,
        input_profile=args.input_profile,
        synthesize_release=True,
        release_ms=args.release_ms,
    )
    adapter = N4NativeAdapter(
        dispatcher,
        sdk_path=args.sdk_path,
        device_path=args.n4_path,
        device_index=args.n4_index,
        initialize=not args.no_init,
        status_stream=None if args.quiet else sys.stderr,
    )
    if visual_output is not None:
        visual_output.adapter = adapter
    runtime = SidebandRuntime(
        transport,
        release_ms=args.release_ms,
        on_visual=(visual_output.handle if visual_output is not None else None),
        status_stream=None if args.quiet else sys.stderr,
    )
    dispatcher.on_record = runtime.handle_record
    if visual_output is not None:
        visual_output.start()
    if not args.no_output_reader:
        runtime.start_output_reader()
    try:
        return adapter.run(duration=args.duration)
    except KeyboardInterrupt:
        adapter.stop()
        return 0
    finally:
        if visual_output is not None:
            visual_output.stop()
        runtime.stop()
        dispatcher.close()


def _run_companion(args: argparse.Namespace) -> int:
    """Use the normal HIDClass companion TLC instead of custom IOCTLs."""

    if args.sideband_path:
        raise SidebandError(
            "--sideband-path belongs to the legacy custom-IOCTL transport; "
            "use --companion-path with --companion"
        )
    if args.reset:
        raise SidebandError(
            "--reset is defined only for the legacy custom-IOCTL queue; "
            "disconnect/reopen the companion HID transport instead"
        )

    from codexmicro_companion import (
        MIRABOX_CODEX_COMPANION_REPORT_ID,
        MIRABOX_CODEX_COMPANION_REPORT_LENGTH,
        MIRABOX_CODEX_COMPANION_USAGE,
        MIRABOX_CODEX_COMPANION_USAGE_PAGE,
        MIRABOX_CODEX_MICRO_PID as companion_product_id,
        MIRABOX_CODEX_MICRO_VID as companion_vendor_id,
        CompanionHidError,
        CompanionHidTransport,
        companion_path_from_info,
    )

    try:
        transport = (
            CompanionHidTransport.open_path(args.companion_path)
            if args.companion_path
            else CompanionHidTransport.open_first()
        )
    except CompanionHidError as exc:
        # Keep the CLI's error contract machine-friendly and avoid exposing a
        # traceback when hidapi is absent or the collection is not installed.
        raise SidebandUnavailable(str(exc)) from exc
    try:
        selected_path = companion_path_from_info(transport.info)
        if selected_path is None:
            selected_path = args.companion_path
        if isinstance(selected_path, bytes):
            selected_path = selected_path.decode("utf-8", errors="replace")
        connection = {
            "transport": "companion-hid",
            "path": selected_path,
            "vendorId": companion_vendor_id,
            "productId": companion_product_id,
            "usagePage": MIRABOX_CODEX_COMPANION_USAGE_PAGE,
            "usage": MIRABOX_CODEX_COMPANION_USAGE,
            "reportId": MIRABOX_CODEX_COMPANION_REPORT_ID,
            "reportLength": MIRABOX_CODEX_COMPANION_REPORT_LENGTH,
            "driverTouched": False,
        }

        # Opening the explicitly selected HID collection is read-only until a
        # push/read/live mode below is requested. It never opens the N4 here.
        if args.check_only:
            _json_line({"mode": "companion-check", "ok": True, **connection})
            return 0

        _json_line({"mode": "connected-companion", "ok": True, **connection})
        if args.push_report is not None:
            report = validate_report(parse_hex_bytes(args.push_report))
            transport.push_input(report)
            _json_line({
                "ok": True,
                "operation": "push-input",
                "length": len(report),
                **connection,
            })
            return 0
        if args.read_output:
            value = transport.read_output(timeout_ms=args.read_timeout_ms)
            if value is None:
                raise SidebandTimeout(
                    f"companion HID read timed out after {args.read_timeout_ms} ms"
                )
            _json_line({
                "ok": True,
                "operation": "read-output",
                "length": len(value),
                "hex": value.hex(" "),
                **connection,
            })
            return 0

        return _run_n4_transport(args, transport)
    except CompanionHidError as exc:
        raise SidebandError(str(exc)) from exc
    finally:
        transport.close()


def _run_live(args: argparse.Namespace) -> int:
    if os.name != "nt":
        raise SidebandUnavailable(
            "live sideband mode requires Windows; this host is not Windows. "
            "Use --dry-run to validate reports without hardware."
        )
    if args.timeout_ms < 0 or args.read_timeout_ms < 0:
        raise SidebandError("timeouts cannot be negative")
    if args.companion_path and not args.companion:
        raise SidebandError("--companion-path requires --companion")
    if args.companion:
        return _run_companion(args)
    client = SidebandClient(args.sideband_path, timeout_ms=args.timeout_ms)
    client.open()
    try:
        info = client.check()
        connection = {
            "path": client.path,
            "info": info.as_dict(),
            **sideband_capability(),
        }

        # `--check-only` must remain a genuinely read-only operation.  In the
        # old flow the flag was parsed but then fell through into the live N4
        # adapter, which could open hardware on a supposedly safe probe.
        if args.check_only:
            _json_line({"mode": "check-only", **connection})
            return 0

        if not args.allow_scaffold_sideband:
            raise SidebandUnavailable(
                "The HID-minidriver custom-IOCTL sideband is scaffold-only and "
                "not proven reachable. Use --allow-scaffold-sideband only for "
                "explicit low-level experiments; prefer the FF70 companion HID "
                "transport instead. An independent companion control device is "
                "not required for that route."
            )

        _json_line({"mode": "connected-experimental", **connection})
        if args.reset:
            client.reset()
            _json_line({"ok": True, "operation": "reset", **sideband_capability()})
            return 0
        if args.push_report is not None:
            client.push_input(validate_report(parse_hex_bytes(args.push_report)))
            _json_line({"ok": True, "operation": "push-input", "length": 64, **sideband_capability()})
            return 0
        if args.read_output:
            value = client.read_output(timeout_ms=args.read_timeout_ms)
            _json_line({
                "ok": True,
                "operation": "read-output",
                "length": 64,
                "hex": value.hex(" "),
                **sideband_capability(),
            })
            return 0

        return _run_n4_transport(args, client)
    finally:
        client.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.dry_run:
            return _dry_run(args)
        return _run_live(args)
    except (SidebandError, ValueError, OSError, ImportError) as exc:
        print(f"codexmicro-bridge: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - CLI wrapper exercises this
    raise SystemExit(main())


__all__ = [
    "BRIDGE_INTERFACE_GUID",
    "FILE_DEVICE_UNKNOWN",
    "IOCTL_MIRABOX_CODEX_GET_INFO",
    "IOCTL_MIRABOX_CODEX_PUSH_INPUT",
    "IOCTL_MIRABOX_CODEX_READ_OUTPUT",
    "IOCTL_MIRABOX_CODEX_RESET",
    "MIRABOX_CODEX_MICRO_REPORT_ID",
    "MIRABOX_CODEX_MICRO_REPORT_LENGTH",
    "MicroFileStore",
    "MicroMessageDecoder",
    "ReportValidationError",
    "SidebandClient",
    "SIDEBAND_ARCHITECTURE",
    "SIDEBAND_ARCHITECTURE_WARNING",
    "SIDEBAND_OPERATIONAL",
    "SidebandError",
    "SidebandInfo",
    "SidebandIoError",
    "SidebandRuntime",
    "SidebandTimeout",
    "SidebandUnavailable",
    "ctl_code",
    "encode_micro_message",
    "main",
    "parse_hex_bytes",
    "probe_sideband",
    "sideband_capability",
    "validate_report",
]
