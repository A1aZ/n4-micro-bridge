"""Companion-HID protocol for the Mirabox Codex Micro bridge.

The main virtual Codex collection is usage page ``0xFF00``/report ID ``6``.
This module describes a second, deliberately independent top-level collection
on usage page ``0xFF70``/report ID ``7``.  The native bridge can select that
collection by usage page without
confusing it with Codex's collection or OpenMicro's ``0xFF60`` interface.

The checked-in UMDF source currently advertises a 58-byte dual-TLC descriptor
and routes both report IDs through one shared HID input stream.  That is a
useful bridge scaffold, but it is not byte-for-byte the independent fifth USB
HID interface used by the ``openmicrokbd`` reference firmware.  HIDClass may
still expose separate collection paths for the two TLCs; only an installed,
WDK-built driver can establish whether the FF00 primary path is discovered by
Codex Desktop and whether the FF70 path is safe for the native bridge.  This
module remains an offline/lazy transport helper until that Windows validation
is complete.

Importing this module never imports hidapi, opens USB, installs a driver, or
changes system state.  ``CompanionHidTransport.open_first`` imports hidapi
only when explicitly called.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Mapping, Sequence
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Values mirrored from driver/codexmicro-umdf/protocol/codexmicro_protocol.h
# ---------------------------------------------------------------------------

MIRABOX_CODEX_MICRO_VID = 0x303A
MIRABOX_CODEX_MICRO_PID = 0x8360
MIRABOX_CODEX_MICRO_USAGE_PAGE = 0xFF00
MIRABOX_CODEX_MICRO_USAGE = 0x0001
MIRABOX_CODEX_MICRO_REPORT_ID = 0x06
MIRABOX_CODEX_MICRO_REPORT_LENGTH = 64
MIRABOX_CODEX_MICRO_BODY_LENGTH = 63

MIRABOX_CODEX_COMPANION_USAGE_PAGE = 0xFF70
MIRABOX_CODEX_COMPANION_USAGE = 0x0001
MIRABOX_CODEX_COMPANION_REPORT_ID = 0x07
MIRABOX_CODEX_COMPANION_REPORT_LENGTH = 64
MIRABOX_CODEX_COMPANION_BODY_LENGTH = 63


# The individual descriptors are intentionally byte strings rather than a
# USB/HID parser dependency.  This keeps contract tests runnable offline.
MIRABOX_CODEX_MICRO_REPORT_DESCRIPTOR = bytes((
    0x06, 0x00, 0xFF, 0x09, 0x01, 0xA1, 0x01, 0x85, 0x06,
    0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08, 0x95, 0x3F,
    0x09, 0x01, 0x81, 0x02, 0x95, 0x3F, 0x09, 0x02, 0x91, 0x02, 0xC0,
))

MIRABOX_CODEX_COMPANION_REPORT_DESCRIPTOR = bytes((
    0x06, 0x70, 0xFF, 0x09, 0x01, 0xA1, 0x01, 0x85, 0x07,
    0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08, 0x95, 0x3F,
    0x09, 0x01, 0x81, 0x02, 0x95, 0x3F, 0x09, 0x02, 0x91, 0x02, 0xC0,
))

MIRABOX_CODEX_MICRO_DUAL_REPORT_DESCRIPTOR = (
    MIRABOX_CODEX_MICRO_REPORT_DESCRIPTOR
    + MIRABOX_CODEX_COMPANION_REPORT_DESCRIPTOR
)

MIRABOX_CODEX_MICRO_REPORT_DESCRIPTOR_LENGTH = len(
    MIRABOX_CODEX_MICRO_REPORT_DESCRIPTOR
)
MIRABOX_CODEX_COMPANION_REPORT_DESCRIPTOR_LENGTH = len(
    MIRABOX_CODEX_COMPANION_REPORT_DESCRIPTOR
)
MIRABOX_CODEX_MICRO_DUAL_REPORT_DESCRIPTOR_LENGTH = len(
    MIRABOX_CODEX_MICRO_DUAL_REPORT_DESCRIPTOR
)

# Short aliases make the protocol convenient to consume without weakening
# the explicit MIRABOX_* names used by the C header and contract checker.
COMPANION_USAGE_PAGE = MIRABOX_CODEX_COMPANION_USAGE_PAGE
COMPANION_USAGE = MIRABOX_CODEX_COMPANION_USAGE
COMPANION_REPORT_ID = MIRABOX_CODEX_COMPANION_REPORT_ID
COMPANION_REPORT_LENGTH = MIRABOX_CODEX_COMPANION_REPORT_LENGTH
COMPANION_BODY_LENGTH = MIRABOX_CODEX_COMPANION_BODY_LENGTH
COMPANION_REPORT_DESCRIPTOR = MIRABOX_CODEX_COMPANION_REPORT_DESCRIPTOR
DUAL_REPORT_DESCRIPTOR = MIRABOX_CODEX_MICRO_DUAL_REPORT_DESCRIPTOR


class CompanionReportError(ValueError):
    """A report does not match the fixed companion/Codex frame shape."""


class CompanionHidError(RuntimeError):
    """Base class for explicit hidapi companion transport failures."""


class CompanionHidUnavailable(CompanionHidError):
    """hidapi is unavailable or no matching collection was found."""


def _coerce_report(value: Any, *, expected_id: int, label: str) -> bytes:
    """Coerce and validate one exact 64-byte HID report.

    hidapi returns a list of integers on some platforms and ``bytes`` on
    others, so both forms are accepted.  A string is accepted only as a
    conventional hexadecimal diagnostic value; malformed text is rejected.
    """

    if isinstance(value, str):
        text = value.strip()
        for separator in (" ", "\t", "\r", "\n", ",", ":", "-"):
            text = text.replace(separator, "")
        if text.lower().startswith("0x"):
            text = text[2:]
        try:
            value = bytes.fromhex(text)
        except ValueError as exc:
            raise CompanionReportError(f"{label} is not valid hexadecimal") from exc
    elif isinstance(value, memoryview):
        value = value.tobytes()
    elif isinstance(value, bytearray):
        value = bytes(value)
    elif not isinstance(value, bytes):
        try:
            value = bytes(value)
        except (TypeError, ValueError) as exc:
            raise CompanionReportError(
                f"{label} must be a bytes-like 64-byte report"
            ) from exc

    if len(value) != MIRABOX_CODEX_MICRO_REPORT_LENGTH:
        raise CompanionReportError(
            f"{label} must be exactly {MIRABOX_CODEX_MICRO_REPORT_LENGTH} bytes; "
            f"got {len(value)}"
        )
    if value[0] != (int(expected_id) & 0xFF):
        raise CompanionReportError(
            f"{label} report ID must be 0x{int(expected_id) & 0xFF:02x}; "
            f"got 0x{value[0]:02x}"
        )
    return bytes(value)


def pack_companion_report(codex_report: Any) -> bytes:
    """Wrap a Codex ID-6 report as a companion ID-7 report.

    The body is opaque and is copied byte-for-byte.  The dual-TLC driver
    translates the report ID while keeping the existing Codex
    63-byte framing unchanged.
    """

    raw = _coerce_report(
        codex_report,
        expected_id=MIRABOX_CODEX_MICRO_REPORT_ID,
        label="Codex report",
    )
    return bytes((MIRABOX_CODEX_COMPANION_REPORT_ID,)) + raw[1:]


def unpack_companion_report(companion_report: Any) -> bytes:
    """Translate a companion ID-7 report back to a Codex ID-6 report."""

    raw = _coerce_report(
        companion_report,
        expected_id=MIRABOX_CODEX_COMPANION_REPORT_ID,
        label="companion report",
    )
    return bytes((MIRABOX_CODEX_MICRO_REPORT_ID,)) + raw[1:]


def _field(entry: Any, *names: str) -> Any:
    """Read a hidapi info field from either a dict or an object."""

    if isinstance(entry, Mapping):
        for name in names:
            if name in entry:
                return entry[name]
        return None
    for name in names:
        if hasattr(entry, name):
            return getattr(entry, name)
    return None


def _intish(value: Any) -> Optional[int]:
    """Normalize hidapi's integer/string representations for matching."""

    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            value = value.decode("ascii")
        except UnicodeDecodeError:
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text, 0)
        except ValueError:
            try:
                return int(text, 16)
            except ValueError:
                return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def find_companion_hid_info(
    entries: Sequence[Any] | None,
    *,
    vendor_id: int | None = MIRABOX_CODEX_MICRO_VID,
    product_id: int | None = MIRABOX_CODEX_MICRO_PID,
    usage_page: int | None = MIRABOX_CODEX_COMPANION_USAGE_PAGE,
    usage: int | None = MIRABOX_CODEX_COMPANION_USAGE,
) -> Any | None:
    """Return the first hidapi record matching the companion TLC.

    ``hid.enumerate(vid, pid)`` normally supplies VID/PID already, but the
    explicit identity checks make this helper safe when given an unfiltered
    list.  Every non-``None`` expected field must be present and match.  In
    particular, a record without usage-page metadata is rejected instead of
    risking opening Codex's primary ``0xFF00`` collection.
    """

    if entries is None:
        return None
    for entry in entries:
        checks = (
            (_field(entry, "vendor_id", "vendorId"), vendor_id),
            (_field(entry, "product_id", "productId"), product_id),
            (_field(entry, "usage_page", "usagePage"), usage_page),
            (_field(entry, "usage"), usage),
        )
        matched = True
        for actual, expected in checks:
            if expected is None:
                continue
            actual_value = _intish(actual)
            if actual_value is None or actual_value != int(expected):
                matched = False
                break
        if matched:
            return entry
    return None


def companion_path_from_info(info: Any) -> Any | None:
    """Extract a hidapi path from a selected info record."""

    return _field(info, "path", "device_path", "devicePath")


def _textish(value: Any) -> str | None:
    """Return a JSON-safe display value for a hidapi string/path field."""

    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", "replace")
        except Exception:
            return value.hex()
    if isinstance(value, str):
        return value
    return str(value)


def _companion_entry_matches(
    entry: Any,
    *,
    vendor_id: int | None = MIRABOX_CODEX_MICRO_VID,
    product_id: int | None = MIRABOX_CODEX_MICRO_PID,
    usage_page: int | None = MIRABOX_CODEX_COMPANION_USAGE_PAGE,
    usage: int | None = MIRABOX_CODEX_COMPANION_USAGE,
) -> bool:
    """Return whether one hidapi record identifies the companion TLC."""

    checks = (
        (_field(entry, "vendor_id", "vendorId"), vendor_id),
        (_field(entry, "product_id", "productId"), product_id),
        (_field(entry, "usage_page", "usagePage"), usage_page),
        (_field(entry, "usage"), usage),
    )
    for actual, expected in checks:
        if expected is None:
            continue
        actual_value = _intish(actual)
        if actual_value is None or actual_value != int(expected):
            return False
    return True


def _normalise_companion_hid_info(entry: Any, *, matched: bool) -> dict[str, Any]:
    """Project a hidapi record into stable, JSON-serialisable fields.

    hidapi's enumeration dictionaries are backend-dependent.  Keeping this
    projection deliberately small means callers can safely display the result
    without serialising backend objects or accidentally invoking a device.
    ``None`` is retained for metadata that a backend does not expose.
    """

    value = {
        "path": _textish(_field(entry, "path", "device_path", "devicePath")),
        "vendorId": _intish(_field(entry, "vendor_id", "vendorId")),
        "productId": _intish(_field(entry, "product_id", "productId")),
        "usagePage": _intish(_field(entry, "usage_page", "usagePage")),
        "usage": _intish(_field(entry, "usage")),
        "releaseNumber": _intish(_field(entry, "release_number", "releaseNumber")),
        "interfaceNumber": _intish(_field(entry, "interface_number", "interfaceNumber")),
        "manufacturerString": _textish(
            _field(entry, "manufacturer_string", "manufacturerString")
        ),
        "productString": _textish(
            _field(entry, "product_string", "productString")
        ),
        "serialNumber": _textish(
            _field(entry, "serial_number", "serialNumber")
        ),
        "busType": _textish(_field(entry, "bus_type", "busType")),
        "matched": bool(matched),
    }
    return value


def probe_companion_hid(
    *,
    hid_module: Any | None = None,
    vendor_id: int | None = MIRABOX_CODEX_MICRO_VID,
    product_id: int | None = MIRABOX_CODEX_MICRO_PID,
    usage_page: int | None = MIRABOX_CODEX_COMPANION_USAGE_PAGE,
    usage: int | None = MIRABOX_CODEX_COMPANION_USAGE,
) -> dict[str, Any]:
    """Perform a strictly read-only companion HID enumeration probe.

    The probe calls only ``hid.enumerate``.  It never creates a hidapi device,
    opens a path, reads or writes a report, starts the N4 SDK, installs a
    driver, or changes system state.  This makes it suitable for a WebUI
    preflight button and for troubleshooting a descriptor before attempting a
    live transport.

    ``hid_module`` is injectable for offline tests.  In normal use it is
    imported lazily so importing this module remains hardware-free.

    The returned object is JSON-serialisable and uses these stable statuses:

    * ``compatible``: at least one exact VID/PID/usage-page/usage match;
    * ``path-unavailable``: identity matched but hidapi exposed no path;
    * ``interface-not-found``: enumeration succeeded but no exact match;
    * ``hidapi-unavailable``: hidapi is not installed or lacks enumerate;
    * ``enumeration-error``: the backend raised while enumerating.
    """

    for label, value in (
        ("vendor_id", vendor_id),
        ("product_id", product_id),
        ("usage_page", usage_page),
        ("usage", usage),
    ):
        if value is not None and not 0 <= int(value) <= 0xFFFF:
            raise ValueError(f"{label} must be between 0 and 65535")

    expected = {
        "vendorId": None if vendor_id is None else int(vendor_id),
        "productId": None if product_id is None else int(product_id),
        "usagePage": None if usage_page is None else int(usage_page),
        "usage": None if usage is None else int(usage),
        "reportId": MIRABOX_CODEX_COMPANION_REPORT_ID,
        "reportLength": MIRABOX_CODEX_COMPANION_REPORT_LENGTH,
    }
    result: dict[str, Any] = {
        "ok": False,
        "status": "unknown",
        "message": "",
        "platform": sys.platform,
        "driverTouched": False,
        "opened": False,
        "enumerated": False,
        "hidapiAvailable": False,
        "interfaceFound": False,
        "identityCompatible": False,
        "pathReady": False,
        "expected": expected,
        "candidates": [],
        "matches": [],
        "paths": [],
        "path": None,
        "candidateCount": 0,
        "matchCount": 0,
    }

    if hid_module is None:
        try:
            hid_module = importlib.import_module("hid")
        except Exception as exc:
            result["status"] = "hidapi-unavailable"
            result["message"] = (
                "hidapi is not installed; install a compatible hidapi package "
                "to enumerate the companion collection"
            )
            result["error"] = str(exc)
            return result

    enumerate_fn = getattr(hid_module, "enumerate", None)
    if not callable(enumerate_fn):
        result["status"] = "hidapi-unavailable"
        result["message"] = "hidapi module does not expose enumerate()"
        return result
    result["hidapiAvailable"] = True

    # hidapi uses 0 as the wildcard for VID/PID.  Keep the public API's None
    # wildcard explicit while passing the conventional integer to backends.
    enumerate_vid = 0 if vendor_id is None else int(vendor_id)
    enumerate_pid = 0 if product_id is None else int(product_id)
    try:
        entries = enumerate_fn(enumerate_vid, enumerate_pid)
        entries = [] if entries is None else list(entries)
    except Exception as exc:
        result["status"] = "enumeration-error"
        result["message"] = f"hidapi enumeration failed: {exc}"
        result["error"] = str(exc)
        return result

    result["enumerated"] = True
    candidates: list[dict[str, Any]] = []
    matches: list[dict[str, Any]] = []
    for entry in entries:
        is_match = _companion_entry_matches(
            entry,
            vendor_id=vendor_id,
            product_id=product_id,
            usage_page=usage_page,
            usage=usage,
        )
        normalised = _normalise_companion_hid_info(entry, matched=is_match)
        candidates.append(normalised)
        if is_match:
            matches.append(normalised)

    paths = [item["path"] for item in matches if item.get("path")]
    result["candidates"] = candidates
    result["matches"] = matches
    result["paths"] = paths
    result["path"] = paths[0] if paths else None
    result["candidateCount"] = len(candidates)
    result["matchCount"] = len(matches)
    result["interfaceFound"] = bool(matches)
    result["identityCompatible"] = bool(matches)
    result["pathReady"] = bool(paths)
    if matches and paths:
        result["ok"] = True
        result["status"] = "compatible"
        result["message"] = (
            "Companion HID collection found; probe was read-only and did not "
            "open the device"
        )
    elif matches:
        result["status"] = "path-unavailable"
        result["message"] = (
            "Companion HID identity matched, but hidapi did not expose an "
            "openable path"
        )
    else:
        result["status"] = "interface-not-found"
        result["message"] = (
            "No HID collection matched the expected companion usage page/usage"
        )
    return result


class CompanionHidTransport:
    """Small, lazy hidapi adapter for the experimental companion TLC.

    The class deliberately exposes only report-level I/O.  It does not try to
    implement Codex JSON framing, N4 mapping, or visual output; those remain
    higher-level bridge responsibilities.
    """

    def __init__(self, device: Any, *, info: Any | None = None,
                 owns_device: bool = False) -> None:
        if device is None:
            raise TypeError("device is required")
        self.device = device
        self.info = info
        self._owns_device = bool(owns_device)
        self._closed = False

    @staticmethod
    def _load_hid_module(hid_module: Any | None) -> Any:
        """Load hidapi only for an explicit transport operation."""

        if hid_module is not None:
            return hid_module
        try:
            return importlib.import_module("hid")
        except Exception as exc:
            raise CompanionHidUnavailable(
                "hidapi is not installed; use the offline helpers or install "
                "a compatible hidapi package explicitly"
            ) from exc

    @classmethod
    def open_path(
        cls,
        path: Any,
        *,
        hid_module: Any | None = None,
        info: Any | None = None,
    ) -> "CompanionHidTransport":
        """Open an explicitly selected companion HID path.

        This is the intentional escape hatch for hidapi backends that omit
        usage-page metadata during enumeration.  The caller owns the safety
        decision: ``path`` should come from a trusted, manually verified
        companion collection.  No VID/PID or usage assumptions are invented
        here, and the path is still opened only after this explicit call.
        """

        if path is None or path == "" or path == b"":
            raise CompanionHidUnavailable("an explicit HID path is required")
        hid_module = cls._load_hid_module(hid_module)
        device_factory = getattr(hid_module, "device", None)
        if not callable(device_factory):
            raise CompanionHidUnavailable("hidapi module lacks device()")

        device = None
        try:
            device = device_factory()
            open_fn = getattr(device, "open_path", None)
            if not callable(open_fn):
                raise CompanionHidUnavailable("hidapi device lacks open_path()")
            open_fn(path)
        except CompanionHidUnavailable:
            if device is not None:
                close_fn = getattr(device, "close", None)
                if callable(close_fn):
                    try:
                        close_fn()
                    except Exception:
                        pass
            raise
        except Exception as exc:
            if device is not None:
                close_fn = getattr(device, "close", None)
                if callable(close_fn):
                    try:
                        close_fn()
                    except Exception:
                        pass
            raise CompanionHidError(f"hidapi open_path failed: {exc}") from exc
        return cls(device, info=info, owns_device=True)

    @classmethod
    def open_first(
        cls,
        *,
        hid_module: Any | None = None,
        vendor_id: int = MIRABOX_CODEX_MICRO_VID,
        product_id: int = MIRABOX_CODEX_MICRO_PID,
    ) -> "CompanionHidTransport":
        """Enumerate and open the first matching companion collection.

        ``hid_module`` is injectable for offline tests.  In production it is
        imported as ``hid`` only at this explicit call site.
        """

        hid_module = cls._load_hid_module(hid_module)
        enumerate_fn = getattr(hid_module, "enumerate", None)
        device_factory = getattr(hid_module, "device", None)
        if not callable(enumerate_fn) or not callable(device_factory):
            raise CompanionHidUnavailable("hidapi module lacks enumerate/device")
        try:
            entries = enumerate_fn(int(vendor_id), int(product_id))
        except Exception as exc:
            raise CompanionHidError(f"hidapi enumeration failed: {exc}") from exc
        info = find_companion_hid_info(
            entries,
            vendor_id=vendor_id,
            product_id=product_id,
        )
        if info is None:
            raise CompanionHidUnavailable(
                "no HID collection with usage page "
                f"0x{MIRABOX_CODEX_COMPANION_USAGE_PAGE:04x} / usage "
                f"0x{MIRABOX_CODEX_COMPANION_USAGE:04x} was found"
            )
        path = companion_path_from_info(info)
        if path is None:
            raise CompanionHidUnavailable("matching hidapi record has no path")
        return cls.open_path(path, hid_module=hid_module, info=info)

    def write_codex_report(self, codex_report: Any) -> int:
        """Send one ID-6 Codex report through the ID-7 companion output."""

        if self._closed:
            raise CompanionHidError("companion transport is closed")
        packet = pack_companion_report(codex_report)
        try:
            result = self.device.write(packet)
        except Exception as exc:
            raise CompanionHidError(f"hidapi write failed: {exc}") from exc
        if not isinstance(result, int) or result != len(packet):
            raise CompanionHidError(
                f"hidapi write returned {result!r}; expected {len(packet)} bytes"
            )
        return int(result)

    # SidebandRuntime intentionally talks to a small transport-shaped object
    # (`push_input`/`read_output`) rather than importing a concrete USB
    # implementation.  These aliases let an already-open companion transport
    # plug into that runtime without changing the legacy SidebandClient API or
    # importing hidapi at module load time.
    def push_input(self, codex_report: Any) -> int:
        """SidebandRuntime-compatible alias for :meth:`write_codex_report`."""

        return self.write_codex_report(codex_report)

    def read_codex_report(self, timeout_ms: int = 100) -> bytes | None:
        """Read one ID-7 report and translate it to an ID-6 Codex report.

        hidapi conventionally returns an empty list on a bounded timeout; that
        maps to ``None`` so callers can poll without conflating timeout with a
        malformed report.
        """

        if self._closed:
            raise CompanionHidError("companion transport is closed")
        if int(timeout_ms) < 0:
            raise ValueError("timeout_ms cannot be negative")
        try:
            raw = self.device.read(
                MIRABOX_CODEX_COMPANION_REPORT_LENGTH,
                int(timeout_ms),
            )
        except Exception as exc:
            raise CompanionHidError(f"hidapi read failed: {exc}") from exc
        if raw is None or len(raw) == 0:
            return None
        return unpack_companion_report(raw)

    def read_output(self, *, timeout_ms: int | None = None) -> bytes | None:
        """SidebandRuntime-compatible alias for companion input reads.

        hidapi uses an empty list for a bounded timeout, represented here as
        ``None``.  The runtime treats that as a poll miss; callers that use the
        legacy :class:`SidebandClient` continue to receive its timeout
        exception semantics.
        """

        return self.read_codex_report(
            timeout_ms=100 if timeout_ms is None else int(timeout_ms)
        )

    def close(self) -> None:
        """Close an owned hidapi handle; safe to call more than once."""

        if self._closed:
            return
        self._closed = True
        if self._owns_device:
            close_fn = getattr(self.device, "close", None)
            if callable(close_fn):
                close_fn()

    def __enter__(self) -> "CompanionHidTransport":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


__all__ = [
    "COMPANION_BODY_LENGTH",
    "COMPANION_REPORT_DESCRIPTOR",
    "COMPANION_REPORT_ID",
    "COMPANION_REPORT_LENGTH",
    "COMPANION_USAGE",
    "COMPANION_USAGE_PAGE",
    "CompanionHidError",
    "CompanionHidTransport",
    "CompanionHidUnavailable",
    "CompanionReportError",
    "DUAL_REPORT_DESCRIPTOR",
    "MIRABOX_CODEX_COMPANION_BODY_LENGTH",
    "MIRABOX_CODEX_COMPANION_REPORT_DESCRIPTOR",
    "MIRABOX_CODEX_COMPANION_REPORT_DESCRIPTOR_LENGTH",
    "MIRABOX_CODEX_COMPANION_REPORT_ID",
    "MIRABOX_CODEX_COMPANION_REPORT_LENGTH",
    "MIRABOX_CODEX_COMPANION_USAGE",
    "MIRABOX_CODEX_COMPANION_USAGE_PAGE",
    "MIRABOX_CODEX_MICRO_DUAL_REPORT_DESCRIPTOR",
    "MIRABOX_CODEX_MICRO_DUAL_REPORT_DESCRIPTOR_LENGTH",
    "MIRABOX_CODEX_MICRO_BODY_LENGTH",
    "MIRABOX_CODEX_MICRO_REPORT_DESCRIPTOR",
    "MIRABOX_CODEX_MICRO_REPORT_DESCRIPTOR_LENGTH",
    "MIRABOX_CODEX_MICRO_REPORT_ID",
    "MIRABOX_CODEX_MICRO_REPORT_LENGTH",
    "MIRABOX_CODEX_MICRO_PID",
    "MIRABOX_CODEX_MICRO_USAGE",
    "MIRABOX_CODEX_MICRO_USAGE_PAGE",
    "MIRABOX_CODEX_MICRO_VID",
    "companion_path_from_info",
    "find_companion_hid_info",
    "pack_companion_report",
    "probe_companion_hid",
    "unpack_companion_report",
]
