"""Optional direct-hidapi input path for the physical Mirabox N4.

The normal bridge uses the StreamDock SDK because it also owns N4 screen/key
output and the SDK's device lifecycle.  This module is an explicit input-only
fallback for machines where the SDK transport DLL cannot open the N4.  It is
deliberately lazy: importing the module never imports ``hid`` or opens a USB
handle.  Callers must opt in with ``--hidapi``/``--hidapi-input``.

Direct hidapi mode does not implement N4 visual writes.  It is therefore safe
for report capture and WebUI input forwarding only, and callers should use
``--no-visuals``.  The existing WebUI bridge remains the source of editable
Micro mappings and synthetic encoder releases.
"""

from __future__ import annotations

import importlib
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from typing import Any, Optional

from n4_native_adapter import (
    N4_INPUT_REPORT_SIZE,
    N4_PRODUCT_ID,
    N4_REPORT_ID,
    N4_USAGE,
    N4_USAGE_PAGE,
    N4_VENDOR_ID,
    detect_external_owner_hints,
    utc_now,
)


DIRECT_HIDAPI_TRANSPORT = "hidapi-direct"
# HIDAPI's ``read(size, ...)`` argument is a receive-buffer capacity, not a
# promise about the device's descriptor length.  The N4 firmware/SDK has been
# observed with both a 512-byte body and a 513-byte report-ID-prefixed form;
# keep the body length above for protocol metadata but read into a larger
# buffer so neither form can be truncated before normalization.
N4_READ_BUFFER_SIZE = 1024


class N4HidapiError(RuntimeError):
    """Base error for the explicit direct-hidapi N4 path."""


class N4HidapiUnavailable(N4HidapiError):
    """hidapi is missing or did not expose a usable N4 interface."""


def _field(entry: Any, *names: str) -> Any:
    """Read a hidapi field from either its dict or object representation."""

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
    """Normalize hidapi integer fields without raising during diagnostics."""

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


def _textish(value: Any) -> Optional[str]:
    """Make a hidapi path/string safe for JSON and path comparisons."""

    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _path_value(entry: Any) -> Any:
    """Return the original path object accepted by ``hid.open_path``."""

    return _field(entry, "path", "device_path", "devicePath")


def _path_text(value: Any) -> str:
    return _textish(value) or ""


def _normalise_info(entry: Any, *, matched: bool = False) -> dict[str, Any]:
    """Project one backend-specific hidapi record into stable JSON fields."""

    return {
        "path": _textish(_path_value(entry)),
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
        "serialNumber": _textish(_field(entry, "serial_number", "serialNumber")),
        "busType": _textish(_field(entry, "bus_type", "busType")),
        "matched": bool(matched),
    }


def _matches(
    entry: Any,
    *,
    vendor_id: int,
    product_id: int,
    usage_page: int,
    usage: int,
    allow_missing_usage: bool = False,
) -> bool:
    """Match the N4 TLC, refusing a keyboard interface by default."""

    for actual, expected in (
        (_intish(_field(entry, "vendor_id", "vendorId")), vendor_id),
        (_intish(_field(entry, "product_id", "productId")), product_id),
    ):
        if actual is None or actual != int(expected):
            return False
    for actual, expected in (
        (_intish(_field(entry, "usage_page", "usagePage")), usage_page),
        (_intish(_field(entry, "usage")), usage),
    ):
        if actual is None:
            if allow_missing_usage:
                continue
            return False
        if actual != int(expected):
            return False
    return True


def _load_hid_module(hid_module: Any | None = None) -> Any:
    if hid_module is not None:
        return hid_module
    try:
        return importlib.import_module("hid")
    except Exception as exc:
        raise N4HidapiUnavailable(
            "hidapi is not installed; install a compatible hidapi package "
            "or use the default StreamDock SDK path"
        ) from exc


def _enumerate(
    *,
    hid_module: Any | None,
    vendor_id: int,
    product_id: int,
) -> tuple[Any, list[Any]]:
    module = _load_hid_module(hid_module)
    enumerate_fn = getattr(module, "enumerate", None)
    if not callable(enumerate_fn):
        raise N4HidapiUnavailable("hidapi module does not expose enumerate()")
    try:
        entries = enumerate_fn(int(vendor_id), int(product_id))
        return module, [] if entries is None else list(entries)
    except Exception as exc:
        raise N4HidapiError(f"hidapi enumeration failed: {exc}") from exc


def probe_n4_hidapi(
    *,
    hid_module: Any | None = None,
    vendor_id: int = N4_VENDOR_ID,
    product_id: int = N4_PRODUCT_ID,
    usage_page: int = N4_USAGE_PAGE,
    usage: int = N4_USAGE,
    path: Any | None = None,
) -> dict[str, Any]:
    """Enumerate the N4 HID TLC without creating/opening a device handle."""

    expected = {
        "vendorId": int(vendor_id),
        "productId": int(product_id),
        "usagePage": int(usage_page),
        "usage": int(usage),
        "reportId": int(N4_REPORT_ID),
        "reportLength": int(N4_INPUT_REPORT_SIZE),
        "readBufferSize": int(N4_READ_BUFFER_SIZE),
    }
    result: dict[str, Any] = {
        "ok": False,
        "status": "unknown",
        "message": "",
        "transport": DIRECT_HIDAPI_TRANSPORT,
        "driverTouched": False,
        "opened": False,
        "enumerated": False,
        "hidapiAvailable": False,
        "interfaceFound": False,
        "identityCompatible": False,
        "pathReady": False,
        "expected": expected,
        "requestedPath": _textish(path),
        "candidates": [],
        "matches": [],
        "paths": [],
        "path": None,
        "candidateCount": 0,
        "matchCount": 0,
        "usageMetadataMissing": [],
    }
    try:
        module, entries = _enumerate(
            hid_module=hid_module,
            vendor_id=vendor_id,
            product_id=product_id,
        )
    except N4HidapiUnavailable as exc:
        result["status"] = "hidapi-unavailable"
        result["message"] = str(exc)
        result["error"] = str(exc)
        return result
    except N4HidapiError as exc:
        result["status"] = "enumeration-error"
        result["message"] = str(exc)
        result["error"] = str(exc)
        return result

    result["hidapiAvailable"] = True
    result["enumerated"] = True
    candidates: list[dict[str, Any]] = []
    matches: list[dict[str, Any]] = []
    requested_text = _path_text(path)
    for entry in entries:
        normalised = _normalise_info(entry)
        candidates.append(normalised)
        same_path = bool(requested_text) and _path_text(_path_value(entry)) == requested_text
        has_usage = (
            _intish(_field(entry, "usage_page", "usagePage")) is not None
            and _intish(_field(entry, "usage")) is not None
        )
        if (
            _intish(_field(entry, "vendor_id", "vendorId")) == int(vendor_id)
            and _intish(_field(entry, "product_id", "productId")) == int(product_id)
            and not has_usage
        ):
            result["usageMetadataMissing"].append(normalised)
        matched = _matches(
            entry,
            vendor_id=vendor_id,
            product_id=product_id,
            usage_page=usage_page,
            usage=usage,
            allow_missing_usage=bool(requested_text and same_path),
        )
        if requested_text and not same_path:
            matched = False
        normalised["matched"] = bool(matched)
        if matched:
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
            "N4 0xFFA0 HID collection found; enumeration was read-only and "
            "did not open the device"
        )
    elif result["usageMetadataMissing"] and not requested_text:
        result["status"] = "usage-metadata-missing"
        result["message"] = (
            "hidapi returned VID/PID candidates without usage metadata; "
            "pass a manually verified --hidapi-path to open one safely"
        )
    else:
        result["status"] = "interface-not-found"
        result["message"] = (
            "No N4 HID collection matched VID/PID 0x%04x:0x%04x, usage page "
            "0x%04x / usage 0x%04x" % (vendor_id, product_id, usage_page, usage)
        )
    return result


class DirectHidapiN4Reader:
    """Input-only N4 reader backed directly by Python hidapi."""

    def __init__(
        self,
        dispatcher: Any,
        *,
        vendor_id: int = N4_VENDOR_ID,
        product_id: int = N4_PRODUCT_ID,
        usage_page: int = N4_USAGE_PAGE,
        usage: int = N4_USAGE,
        report_id: int = N4_REPORT_ID,
        device_path: Any | None = None,
        device_index: int = 0,
        read_timeout_ms: int = 100,
        read_buffer_size: int = N4_READ_BUFFER_SIZE,
        hid_module: Any | None = None,
        status_stream: Any = sys.stderr,
    ) -> None:
        if dispatcher is None:
            raise ValueError("dispatcher is required")
        if int(device_index) < 0:
            raise ValueError("device index must be non-negative")
        if int(read_timeout_ms) < 1 or int(read_timeout_ms) > 5000:
            raise ValueError("hidapi read timeout must be between 1 and 5000 ms")
        if int(read_buffer_size) < 11 or int(read_buffer_size) > 65536:
            raise ValueError("hidapi read buffer size must be between 11 and 65536 bytes")
        self.dispatcher = dispatcher
        self.vendor_id = int(vendor_id)
        self.product_id = int(product_id)
        self.usage_page = int(usage_page)
        self.usage = int(usage)
        self.report_id = int(report_id)
        self.device_path = device_path
        self.device_index = int(device_index)
        self.read_timeout_ms = int(read_timeout_ms)
        self.read_buffer_size = int(read_buffer_size)
        self.hid_module = hid_module
        self.status_stream = status_stream
        self.device: Any = None
        self.info: Any = None
        self.stop_event = threading.Event()
        self._ready = threading.Event()
        self._lock = threading.RLock()
        self._opened = False
        self._run_flag = False
        self.reads = 0
        self.timeouts = 0
        self.last_read_at: Optional[str] = None
        self.last_error: Optional[str] = None

    def _status(self, message: str) -> None:
        if self.status_stream is not None:
            print(message, file=self.status_stream, flush=True)

    def enumerate(self) -> list[Any]:
        _module, entries = _enumerate(
            hid_module=self.hid_module,
            vendor_id=self.vendor_id,
            product_id=self.product_id,
        )
        return entries

    def enumerate_json(self) -> list[dict[str, Any]]:
        return probe_n4_hidapi(
            hid_module=self.hid_module,
            vendor_id=self.vendor_id,
            product_id=self.product_id,
            usage_page=self.usage_page,
            usage=self.usage,
            path=self.device_path,
        )["candidates"]

    def _select_info(self) -> Any:
        entries = self.enumerate()
        requested_text = _path_text(self.device_path)
        matches = []
        for entry in entries:
            same_path = bool(requested_text) and _path_text(_path_value(entry)) == requested_text
            if requested_text and not same_path:
                continue
            if _matches(
                entry,
                vendor_id=self.vendor_id,
                product_id=self.product_id,
                usage_page=self.usage_page,
                usage=self.usage,
                allow_missing_usage=bool(requested_text and same_path),
            ):
                matches.append(entry)
        if not matches:
            if requested_text:
                raise N4HidapiUnavailable(
                    "requested hidapi path was not an enumerated N4 0xFFA0 interface; "
                    "verify --hidapi-path before opening"
                )
            if any(
                _intish(_field(entry, "vendor_id", "vendorId")) == self.vendor_id
                and _intish(_field(entry, "product_id", "productId")) == self.product_id
                for entry in entries
            ):
                raise N4HidapiUnavailable(
                    "hidapi exposed N4 VID/PID candidates but no usage metadata; "
                    "pass a manually verified --hidapi-path"
                )
            raise N4HidapiUnavailable(
                "no N4 0xFFA0/usage-1 HID collection was found"
            )
        if self.device_path is not None:
            return matches[0]
        if self.device_index >= len(matches):
            raise N4HidapiUnavailable(
                f"hidapi N4 device index {self.device_index} is out of range; "
                f"found {len(matches)} matching interface(s)"
            )
        return matches[self.device_index]

    def open(self) -> bool:
        with self._lock:
            if self._opened:
                return True
            module = _load_hid_module(self.hid_module)
            device_factory = getattr(module, "device", None)
            if not callable(device_factory):
                raise N4HidapiUnavailable("hidapi module does not expose device()")
            info = self._select_info()
            raw_path = _path_value(info)
            if raw_path in (None, "", b""):
                raise N4HidapiUnavailable("matching N4 hidapi record has no path")
            device = None
            try:
                device = device_factory()
                open_fn = getattr(device, "open_path", None)
                if not callable(open_fn):
                    raise N4HidapiUnavailable("hidapi device lacks open_path()")
                open_fn(raw_path)
            except N4HidapiUnavailable:
                if device is not None:
                    self._close_device(device)
                raise
            except Exception as exc:
                if device is not None:
                    self._close_device(device)
                raise N4HidapiError(f"hidapi open_path failed: {exc}") from exc
            self.device = device
            self.info = info
            self._opened = True
            self._ready.set()
            self.last_error = None
            self._status(
                f"[n4/hidapi] opened {_textish(raw_path) or '?'} "
                f"VID:PID={self.vendor_id:04x}:{self.product_id:04x}"
            )
            return True

    @staticmethod
    def _close_device(device: Any) -> None:
        close_fn = getattr(device, "close", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:
                pass

    @property
    def is_open(self) -> bool:
        with self._lock:
            return bool(self._opened and self.device is not None)

    def wait_until_ready(self, timeout: Optional[float] = None) -> bool:
        return self._ready.wait(timeout)

    def run(self, *, duration: Optional[float] = None) -> int:
        self.stop_event.clear()
        self.open()
        with self._lock:
            self._run_flag = True
        deadline = None if duration is None else time.monotonic() + float(duration)
        try:
            while not self.stop_event.is_set():
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                with self._lock:
                    device = self.device
                if device is None:
                    raise N4HidapiError("hidapi device disappeared while reading")
                try:
                    # Use a capacity larger than the descriptor body.  Some
                    # HIDAPI backends include a leading report-ID byte even
                    # when the report ID is zero; normalization handles that
                    # optional prefix after the read completes.
                    raw = device.read(self.read_buffer_size, self.read_timeout_ms)
                except Exception as exc:
                    self.last_error = f"hidapi read failed: {exc}"
                    raise N4HidapiError(self.last_error) from exc
                if raw is None or len(raw) == 0:
                    self.timeouts += 1
                    continue
                self.reads += 1
                self.last_read_at = utc_now()
                self.dispatcher.handle(raw, report_id=self.report_id)
            return 0
        finally:
            with self._lock:
                self._run_flag = False
            self.close()

    def stop(self) -> None:
        self.stop_event.set()

    def close(self) -> None:
        with self._lock:
            device = self.device
            self.device = None
            self._opened = False
            self._ready.clear()
        if device is not None:
            self._close_device(device)
            self._status("[n4/hidapi] closed")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            info = self.info
            path = _textish(_path_value(info)) if info is not None else _textish(self.device_path)
            opened = bool(self._opened and self.device is not None)
            return {
                "transport": DIRECT_HIDAPI_TRANSPORT,
                "opened": opened,
                "ready": bool(self._ready.is_set()),
                "path": path,
                "vendorId": self.vendor_id,
                "productId": self.product_id,
                "usagePage": self.usage_page,
                "usage": self.usage,
                "reportId": self.report_id,
                "reportLength": N4_INPUT_REPORT_SIZE,
                "readBufferSize": self.read_buffer_size,
                "deviceIndex": self.device_index,
                "devicePathRequested": _textish(self.device_path),
                "initialized": False,
                "openMode": DIRECT_HIDAPI_TRANSPORT,
                "readTimeoutMs": self.read_timeout_ms,
                "reads": self.reads,
                "timeouts": self.timeouts,
                "lastReadAt": self.last_read_at,
                "lastError": self.last_error,
                "transportDetails": {
                    "present": bool(info is not None or self.device is not None),
                    "handlePresent": opened,
                    "isOpen": opened,
                    "inputReportSize": N4_INPUT_REPORT_SIZE,
                    "outputReportSize": None,
                    "featureReportSize": None,
                    "reportId": self.report_id,
                    "lastError": self.last_error,
                },
                # Keep the same read-only host-process hint in both native
                # transport snapshots so the WebUI can explain an opened but
                # silent handle regardless of which input path is selected.
                "externalOwnerHints": detect_external_owner_hints(),
                "reader": {
                    "threadAlive": bool(self._run_flag),
                    "runFlag": bool(self._run_flag),
                    "rawCallbackRegistered": False,
                },
                "heartbeat": {
                    "threadAlive": False,
                    "runFlag": False,
                },
            }

    def lighting_capabilities(self) -> dict[str, Any]:
        return {
            "open": self.is_open,
            "hasRGBLed": False,
            "ledCounts": 0,
            "methods": [],
        }

    def __enter__(self) -> "DirectHidapiN4Reader":
        self.open()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


__all__ = [
    "DIRECT_HIDAPI_TRANSPORT",
    "N4_READ_BUFFER_SIZE",
    "DirectHidapiN4Reader",
    "N4HidapiError",
    "N4HidapiUnavailable",
    "probe_n4_hidapi",
]
