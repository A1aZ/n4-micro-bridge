"""Native Mirabox N4 input/output adapter.

This module deliberately keeps USB/HID access small and replaceable:

* device discovery/open/read is delegated to the upstream StreamDock Python
  SDK shipped under ``upstream/StreamDock-Device-SDK`` by default;
* an explicit, input-only ``--hidapi`` fallback can read the N4 TLC directly
  when the SDK transport DLL is unavailable;
* the SDK's ordinary ``StreamDockN4`` class is used unchanged for transport,
  while this module decodes the four encoder codes that the class currently
  omits;
* reports can be written as JSONL and/or POSTed to the local bridge endpoint;
* display, key-image, brightness, refresh, and optional RGB calls are
  serialized on the same opened SDK device used for input.

It does not install a driver, replace the Windows HID driver, or create a
virtual HID device.  The latter is a separate (kernel/UMDF) concern.  This
adapter is therefore useful as a safe native input source for the WebUI and
for testing the Micro wire mapping before a virtual device is available.
"""

from __future__ import annotations

import argparse
import csv
import copy
import io
import json
import os
import platform
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence, TextIO


# The identity is the one observed on the user's ordinary N4 (HOTSPOTEKUSB
# HID DEMO).  Keep these values configurable on the CLI for regional variants.
N4_VENDOR_ID = 0x6602
N4_PRODUCT_ID = 0x1001
N4_USAGE_PAGE = 0xFFA0
N4_USAGE = 1
N4_REPORT_ID = 0
N4_INPUT_REPORT_SIZE = 512
DEFAULT_BRIDGE_URL = "http://127.0.0.1:8787/api/bridge/report"
# The upstream SDK heartbeat worker sleeps for up to ten seconds between
# checks.  Ordered-open startup failures must wait a little longer than that
# interval before tearing down the underlying HID transport, otherwise a
# sleeping heartbeat can wake up after ``transport.close()`` and use a freed
# handle.
SDK_WORKER_JOIN_TIMEOUT_SECONDS = 11.0

# A normal StreamDock installation opens the N4 vendor-defined HID collection
# itself.  When the desktop application is left running, a second reader can
# appear healthy (the HID path enumerates and ``open_path`` returns) while no
# reports are delivered.  We cannot reliably attribute a kernel HID handle
# without a privileged handle-inspection tool, so this diagnostic deliberately
# reports only *process-presence hints*.  Callers must treat it as a prompt to
# close the known host, never as proof that a specific handle is owned.
KNOWN_EXTERNAL_OWNER_PROCESS_NAMES: tuple[str, ...] = (
    "StreamDock.exe",
    "CefViewWing.exe",
)
EXTERNAL_OWNER_HINT_CACHE_TTL_SECONDS = 2.0
_external_owner_hint_lock = threading.Lock()
_external_owner_hint_cache_at = 0.0
_external_owner_hint_cache: Optional[dict[str, Any]] = None


# The C++ SDK's StreamDockN4 dispatcher (and the current WebUI default) uses
# hardware input codes 0x06..0x0A, 0x01..0x05, then 0x40..0x43.  The Python
# SDK's image API exposes a different logical numbering (11..15, 6..10,
# 1..4), so keep that legacy profile available explicitly instead of guessing
# based on overlapping byte values.
N4_BUTTON_PROFILES: Mapping[str, tuple[int, ...]] = {
    "cpp": (0x06, 0x07, 0x08, 0x09, 0x0A, 0x01, 0x02, 0x03, 0x04, 0x05, 0x40, 0x41, 0x42, 0x43),
    "python": (11, 12, 13, 14, 15, 6, 7, 8, 9, 10, 1, 2, 3, 4),
}
DEFAULT_BUTTON_PROFILE = "cpp"


def _button_mapping(profile: str = DEFAULT_BUTTON_PROFILE) -> dict[int, int]:
    """Build hardware-code -> logical-key mapping for a named input profile."""

    try:
        codes = N4_BUTTON_PROFILES[profile]
    except KeyError as error:
        raise ValueError(
            f"unknown N4 button profile {profile!r}; "
            f"choose one of {', '.join(N4_BUTTON_PROFILES)}"
        ) from error
    return {hardware_code: index + 1 for index, hardware_code in enumerate(codes)}


# Convenient aliases for callers that want a concrete table without invoking
# `_button_mapping()` themselves.  New listeners should select a profile
# explicitly; the default alias follows the C++/WebUI input profile.
N4_BUTTON_HARDWARE_TO_LOGICAL: Mapping[int, int] = _button_mapping()
N4_BUTTON_HARDWARE_TO_LOGICAL_PYTHON: Mapping[int, int] = _button_mapping("python")

# These mappings are confirmed by real N4 reports and are also present in the
# upstream N4Pro decoder.  Direction names are intentionally human-oriented;
# the Micro wire key is supplied alongside them.
N4_KNOB_ROTATE: Mapping[int, tuple[int, str, str]] = {
    0xA0: (1, "ccw", "ENC_CC"),
    0xA1: (1, "cw", "ENC_CW"),
    0x50: (2, "ccw", "ENC_CC"),
    0x51: (2, "cw", "ENC_CW"),
    0x90: (3, "ccw", "ENC_CC"),
    0x91: (3, "cw", "ENC_CW"),
    0x70: (4, "ccw", "ENC_CC"),
    0x71: (4, "cw", "ENC_CW"),
}

N4_KNOB_PRESS: Mapping[int, int] = {
    0x37: 1,
    0x35: 2,
    0x33: 3,
    0x36: 4,
}

# The C++ StreamDockN4 dispatcher exposes two secondary-screen swipe codes.
# Codex Micro's ``v.oai.hid`` wire vocabulary has no unambiguous swipe key,
# so these are decoded for diagnostics only and never previewed as a Micro
# input event.
N4_SWIPE: Mapping[int, str] = {
    0x38: "left",
    0x39: "right",
}

# The default mirabox.codex.micro configuration used by the WebUI.  The
# fourteenth physical N4 button has no Micro key by default (the UI leaves it
# editable/disabled), so it is represented by None.
def _button_micro_mapping(profile: str = DEFAULT_BUTTON_PROFILE) -> dict[int, Optional[str]]:
    codes = N4_BUTTON_PROFILES[profile]
    return {
        hardware_code: (f"AG{index:02d}" if index < 6 else f"ACT{index:02d}")
        if index < 13
        else None
        for index, hardware_code in enumerate(codes)
    }


N4_BUTTON_TO_MICRO: Mapping[int, Optional[str]] = _button_micro_mapping()


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp suitable for JSONL/SSE logs."""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _parse_tasklist_csv(output: Any) -> list[dict[str, Any]]:
    """Parse the stable image-name/PID columns emitted by ``tasklist /FO CSV``.

    ``tasklist`` localizes the remaining columns (session name, status, and
    memory), so this parser intentionally consumes only the first two fields.
    Malformed rows are ignored: process-presence diagnostics must never make
    the USB bridge fail.
    """

    rows: list[dict[str, Any]] = []
    try:
        reader = csv.reader(io.StringIO(str(output or "")))
        for row in reader:
            if len(row) < 2:
                continue
            name = str(row[0] or "").strip()
            if not name or name.lower() in {"image name", "imagename"}:
                continue
            try:
                pid = int(str(row[1]).strip().replace(",", ""), 10)
            except (TypeError, ValueError):
                continue
            if pid < 0:
                continue
            rows.append({"name": name, "pid": pid})
    except (csv.Error, TypeError, ValueError):
        return []
    return rows


def _external_owner_hint_result(
    *,
    system_name: str,
    runner: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    """Perform one uncached, read-only known-owner process check.

    The optional ``runner`` hook is intentionally private and exists for unit
    tests; production callers use :func:`detect_external_owner_hints` without
    supplying it.  No process is opened, signalled, or terminated.
    """

    checked_at = utc_now()
    base: dict[str, Any] = {
        "available": False,
        "status": "unsupported",
        "suspected": False,
        "confidence": "process-presence-only",
        "checkedAt": checked_at,
        "knownProcessNames": list(KNOWN_EXTERNAL_OWNER_PROCESS_NAMES),
        "processes": [],
        "message": "外部占用进程检查仅支持 Windows；未检查句柄归属。",
    }
    if str(system_name).lower() != "windows":
        return base

    execute = runner or subprocess.run
    kwargs = {
        "check": False,
        "capture_output": True,
        "text": True,
        "timeout": 1.5,
    }
    # Avoid flashing a console window on Windows.  ``getattr`` keeps tests and
    # non-Windows imports portable.
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if no_window:
        kwargs["creationflags"] = no_window
    try:
        completed = execute(["tasklist.exe", "/FO", "CSV", "/NH"], **kwargs)
    except (FileNotFoundError, OSError, subprocess.SubprocessError, TimeoutError) as exc:
        base.update(
            status="error",
            message=f"无法读取 Windows 进程列表：{exc}",
        )
        return base
    except Exception as exc:  # pragma: no cover - defensive for unusual runners
        base.update(
            status="error",
            message=f"读取 Windows 进程列表失败：{exc}",
        )
        return base

    return_code = getattr(completed, "returncode", 0)
    if return_code not in (None, 0):
        stderr = str(getattr(completed, "stderr", "") or "").strip()
        suffix = f": {stderr[:300]}" if stderr else ""
        base.update(
            status="error",
            message=f"tasklist 返回错误码 {return_code}{suffix}",
        )
        return base

    processes = _parse_tasklist_csv(getattr(completed, "stdout", ""))
    wanted = {name.lower() for name in KNOWN_EXTERNAL_OWNER_PROCESS_NAMES}
    detected = [
        item for item in processes
        if str(item.get("name", "")).lower() in wanted
    ]
    streamdock_present = any(
        str(item.get("name", "")).lower() == "streamdock.exe"
        for item in detected
    )
    # CefViewWing is a generic Chromium helper.  Treat it as a likely child
    # only when the owning StreamDock process is also present; otherwise expose
    # it as a non-authoritative related-process hint.
    likely = [
        {
            **item,
            "likelyOwner": (
                str(item.get("name", "")).lower() == "streamdock.exe"
                or streamdock_present
            ),
        }
        for item in detected
    ]
    if streamdock_present:
        status = "detected"
        message = (
            "检测到 StreamDock.exe；它可能正在占用 N4 的 0xFFA0/usage-1 HID 接口。"
            "请退出 StreamDock 后再测试；此结果不等于已确认内核句柄归属。"
        )
    elif likely:
        status = "related-process"
        message = (
            "检测到 CefViewWing.exe，但未检测到 StreamDock.exe；仅作相关进程提示，"
            "不能据此判断 N4 HID 接口被占用。"
        )
    else:
        status = "none"
        message = "未检测到已知 StreamDock/CefView 进程；仍未执行句柄级检查。"
    base.update(
        available=True,
        status=status,
        suspected=bool(streamdock_present),
        processes=likely,
        message=message,
    )
    return base


def detect_external_owner_hints(*, force: bool = False) -> dict[str, Any]:
    """Return a cached, read-only hint about known N4 host processes.

    This deliberately checks process names only.  Windows does not expose a
    cheap, unprivileged way to map a HID file handle back to an owning process;
    therefore ``suspected`` means “a known host is running”, not “the process
    definitely owns this exact N4 handle”.
    """

    global _external_owner_hint_cache_at, _external_owner_hint_cache
    now = time.monotonic()
    with _external_owner_hint_lock:
        if (
            not force
            and _external_owner_hint_cache is not None
            and now - _external_owner_hint_cache_at < EXTERNAL_OWNER_HINT_CACHE_TTL_SECONDS
        ):
            return copy.deepcopy(_external_owner_hint_cache)
        result = _external_owner_hint_result(system_name=platform.system())
        _external_owner_hint_cache = copy.deepcopy(result)
        _external_owner_hint_cache_at = now
        return result


def parse_int(value: Any) -> int:
    """Parse decimal or ``0x`` integer CLI/config values."""

    if isinstance(value, bool):
        raise ValueError("boolean is not an integer")
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        raise ValueError("empty integer")
    return int(text, 0)


def coerce_report(value: Any) -> bytes:
    """Convert bytes-like or an integer sequence into immutable report bytes."""

    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, memoryview):
        return value.tobytes()
    # ctypes arrays and similar SDK buffers generally expose ``bytes()``.
    if not isinstance(value, (str, list, tuple)):
        try:
            return bytes(value)
        except (TypeError, ValueError):
            pass
    if isinstance(value, str):
        raise TypeError("report text must be parsed with parse_hex_report()")
    if isinstance(value, Sequence):
        out = bytearray()
        for index, item in enumerate(value):
            try:
                number = parse_int(item)
            except (TypeError, ValueError) as error:
                raise ValueError(f"report byte {index} is invalid: {item!r}") from error
            if number < 0 or number > 0xFF:
                raise ValueError(f"report byte {index} is outside 0..255: {number}")
            out.append(number)
        return bytes(out)
    raise TypeError(f"unsupported report type: {type(value).__name__}")


def parse_hex_report(text: str) -> bytes:
    """Parse a report represented as compact or spaced hexadecimal text."""

    if not isinstance(text, str):
        raise TypeError("hex report must be a string")
    # Accept the same forms users paste from the WebUI: ``41 43 4b`` or
    # ``41434b``.  Separators are restricted to whitespace/comma/colon/dash so
    # accidental non-hex text is not silently discarded.
    cleaned = text.strip().replace(" ", "").replace("\t", "").replace("\r", "").replace("\n", "")
    for separator in (",", ":", "-"):
        cleaned = cleaned.replace(separator, "")
    if cleaned.lower().startswith("0x"):
        cleaned = cleaned[2:]
    if len(cleaned) % 2:
        raise ValueError("hex report must contain complete bytes")
    if not cleaned:
        return b""
    try:
        return bytes.fromhex(cleaned)
    except ValueError as error:
        raise ValueError(f"invalid hex report: {error}") from error


def _ack_offset(report: bytes) -> Optional[int]:
    """Return ACK packet offset (0, or 1 when a report-ID byte is present)."""

    for offset in (0, 1):
        if len(report) >= offset + 11 and report[offset : offset + 3] == b"ACK":
            if report[offset + 5 : offset + 7] == b"OK":
                return offset
    return None


def is_n4_input_packet(report: Any) -> bool:
    """Whether *report* has the StreamDock ACK/OK input-event framing."""

    try:
        return _ack_offset(coerce_report(report)) is not None
    except (TypeError, ValueError):
        return False


def normalize_n4_report(report: Any, report_id: int = N4_REPORT_ID) -> tuple[bytes, int]:
    """Strip an optional leading HID report-ID byte for the WebUI endpoint.

    The StreamDock C transport normally returns the 512-byte body and exposes
    report ID separately.  Some alternate readers include ID 0 as byte 0.  If
    ACK starts at byte 1 we remove that byte before POSTing because the Node
    bridge, like the SDK, expects ``data[9]``/``data[10]`` in the body.
    """

    raw = coerce_report(report)
    offset = _ack_offset(raw)
    try:
        rid = parse_int(report_id)
    except (TypeError, ValueError):
        rid = N4_REPORT_ID
    if offset == 1:
        # Preserve a caller-supplied non-zero ID, otherwise use the prefix.
        if rid == N4_REPORT_ID:
            rid = raw[0]
        return raw[1:], rid
    return raw, rid


def decode_n4_report(
    report: Any,
    *,
    require_ack: bool = False,
    input_profile: str = DEFAULT_BUTTON_PROFILE,
) -> Optional[dict[str, Any]]:
    """Decode one N4 report into a JSON-serializable event description.

    ``state == 0x01`` is the SDK's pressed state; all other states are treated
    as released.  N4 encoder presses observed in hardware are one-shot press
    reports, so the returned event includes ``oneShot: true`` and callers can
    synthesize a release at a higher layer.  Rotation events use Micro action
    ``2`` regardless of state.

    When ``require_ack`` is false (the default), a synthetic 11-byte buffer with
    code/state at offsets 9/10 can be decoded for tests.  The native listener
    uses ``require_ack=True`` to avoid interpreting unrelated output replies as
    input actions.
    """

    try:
        raw = coerce_report(report)
    except (TypeError, ValueError):
        return None
    if len(raw) < 11:
        return None
    offset = _ack_offset(raw)
    if require_ack and offset is None:
        return None
    if offset is None:
        offset = 0
    if len(raw) < offset + 11:
        return None

    code = raw[offset + 9]
    state = raw[offset + 10]
    normalized_state = 1 if state == 0x01 else 0
    base: dict[str, Any] = {
        "hardwareCode": code,
        "hardwareCodeHex": f"0x{code:02x}",
        "state": state,
        "stateHex": f"0x{state:02x}",
        "normalizedState": normalized_state,
        "packetOffset": offset,
        "ack": _ack_offset(raw) is not None,
    }

    if code in N4_KNOB_ROTATE:
        knob, direction, micro_key = N4_KNOB_ROTATE[code]
        base.update(
            {
                "kind": "knob_rotate",
                "knob": knob,
                "direction": direction,
                "microKey": micro_key,
                "act": 2,
            }
        )
        return base

    if code in N4_KNOB_PRESS:
        base.update(
            {
                "kind": "knob_press",
                "knob": N4_KNOB_PRESS[code],
                "microKey": "ENC",
                "act": normalized_state,
                # Real N4 firmware currently emits only state=0x01 on a knob
                # press; there is no matching release report.
                "oneShot": normalized_state == 1,
            }
        )
        return base

    if code in N4_SWIPE:
        base.update(
            {
                "kind": "swipe",
                "direction": N4_SWIPE[code],
                "microKey": None,
                "microSupported": False,
            }
        )
        return base

    try:
        button_mapping = _button_mapping(input_profile)
        micro_mapping = _button_micro_mapping(input_profile)
    except ValueError:
        return None
    if code in button_mapping:
        base.update(
            {
                "kind": "button",
                "key": button_mapping[code],
                "inputProfile": input_profile,
                "microKey": micro_mapping.get(code),
                "act": normalized_state,
            }
        )
        return base

    base.update({"kind": "unknown", "act": normalized_state})
    return base


def micro_events_for_decoded(
    decoded: Optional[Mapping[str, Any]],
    *,
    synthesize_release: bool = False,
    release_ms: int = 100,
) -> list[dict[str, Any]]:
    """Build default Micro wire events from :func:`decode_n4_report` output.

    The WebUI server remains the source of truth for editable mappings and its
    bridge scheduler.  This helper is intentionally only a local preview for
    dry-run/JSONL diagnostics; HTTP mode sends raw reports to the server so its
    current configuration is applied exactly once.
    """

    if not decoded or decoded.get("kind") in {"unknown", "swipe"}:
        return []
    key = decoded.get("microKey")
    if not isinstance(key, str) or not key:
        return []
    action = int(decoded.get("act", 0))
    params: dict[str, Any] = {"k": key, "act": action}
    if key.startswith("AG") and len(key) == 4 and key[2:].isdigit():
        params["ag"] = int(key[2:])
    events = [{"method": "v.oai.hid", "params": params}]
    if (
        synthesize_release
        and decoded.get("kind") == "knob_press"
        and action == 1
    ):
        try:
            delay = parse_int(release_ms)
        except (TypeError, ValueError):
            delay = 100
        release_params = {**params, "act": 0}
        events.append(
            {
                "method": "v.oai.hid",
                "params": release_params,
                "synthetic": True,
                "delayMs": delay,
            }
        )
    return events


def report_record(
    report: Any,
    *,
    report_id: int = N4_REPORT_ID,
    input_profile: str = DEFAULT_BUTTON_PROFILE,
    include_micro_preview: bool = True,
    synthesize_release: bool = False,
    release_ms: int = 100,
    timestamp: Optional[str] = None,
) -> dict[str, Any]:
    """Create the JSONL record used by the adapter sinks."""

    raw = coerce_report(report)
    normalized, effective_report_id = normalize_n4_report(raw, report_id)
    input_packet = is_n4_input_packet(raw)
    decoded = (
        decode_n4_report(
            normalized,
            require_ack=True,
            input_profile=input_profile,
        )
        if input_packet
        else None
    )
    record: dict[str, Any] = {
        "source": "mirabox-n4-native",
        "at": timestamp or utc_now(),
        "reportId": effective_report_id,
        "inputProfile": input_profile,
        "length": len(raw),
        "inputPacket": input_packet,
        "bytes": raw.hex(" "),
        "decoded": decoded,
    }
    if normalized != raw:
        record["normalizedBytes"] = normalized.hex(" ")
    if include_micro_preview:
        record["microEvents"] = micro_events_for_decoded(
            decoded,
            synthesize_release=synthesize_release,
            release_ms=release_ms,
        )
    return record


class JsonlSink:
    """Thread-safe JSON Lines writer."""

    def __init__(self, stream: TextIO, *, close_stream: bool = False) -> None:
        self.stream = stream
        self.close_stream = close_stream
        self._lock = threading.Lock()

    def write(self, record: Mapping[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self.stream.write(line + "\n")
            self.stream.flush()

    def close(self) -> None:
        if self.close_stream:
            self.stream.close()


class HttpReporter:
    """POST raw N4 reports to ``webui/server.cjs`` without third-party deps."""

    def __init__(
        self,
        url: str = "http://127.0.0.1:8787/api/bridge/report",
        *,
        timeout: float = 2.0,
        user_agent: str = "mirabox-n4-native/1.0",
    ) -> None:
        if not url:
            raise ValueError("HTTP endpoint URL cannot be empty")
        if timeout <= 0:
            raise ValueError("HTTP timeout must be positive")
        self.url = url
        self.timeout = float(timeout)
        self.user_agent = user_agent
        self.posts = 0
        self.successes = 0
        self.failures = 0
        self.last_status: Optional[int] = None
        self.last_error: Optional[str] = None
        self._stats_lock = threading.Lock()

    def _record(self, result: Mapping[str, Any]) -> dict[str, Any]:
        """Update local HTTP counters without changing the returned result."""

        ok = bool(result.get("ok"))
        status = result.get("status")
        error = result.get("error")
        with self._stats_lock:
            self.posts += 1
            if ok:
                self.successes += 1
                self.last_error = None
            else:
                self.failures += 1
                self.last_error = str(error) if error else "HTTP reporter failed"
            self.last_status = int(status) if isinstance(status, int) else None
        return dict(result)

    def snapshot(self) -> dict[str, Any]:
        """Return thread-safe counters for native bridge diagnostics."""

        with self._stats_lock:
            return {
                "posts": self.posts,
                "successes": self.successes,
                "failures": self.failures,
                "lastStatus": self.last_status,
                "lastError": self.last_error,
            }

    def post(self, report: Any, *, report_id: int = N4_REPORT_ID) -> dict[str, Any]:
        raw, effective_report_id = normalize_n4_report(report, report_id)
        payload = {
            "bytes": list(raw),
            "reportId": effective_report_id,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "content-type": "application/json",
                "accept": "application/json",
                "user-agent": self.user_agent,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response_body = response.read().decode("utf-8", errors="replace")
                try:
                    parsed: Any = json.loads(response_body) if response_body else None
                except json.JSONDecodeError:
                    parsed = response_body
                return self._record({
                    "ok": True,
                    "status": int(response.status),
                    "response": parsed,
                })
        except urllib.error.HTTPError as error:
            response_body = error.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(response_body) if response_body else None
            except json.JSONDecodeError:
                parsed = response_body
            return self._record({
                "ok": False,
                "status": int(error.code),
                "error": f"HTTP {error.code}: {error.reason}",
                "response": parsed,
            })
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            return self._record({"ok": False, "error": str(error)})


class ReportDispatcher:
    """Fan reports out to JSONL and HTTP sinks.

    By default only ACK/OK input packets are forwarded.  Initialization replies
    and unrelated status reports can be included with ``include_non_input`` for
    low-level diagnostics.
    """

    def __init__(
        self,
        *,
        jsonl: Optional[JsonlSink] = None,
        http: Optional[HttpReporter] = None,
        include_non_input: bool = False,
        input_profile: str = DEFAULT_BUTTON_PROFILE,
        include_micro_preview: bool = True,
        synthesize_release: bool = False,
        release_ms: int = 100,
        on_record: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> None:
        if jsonl is None and http is None and on_record is None:
            raise ValueError("at least one report sink is required")
        self.jsonl = jsonl
        self.http = http
        self.include_non_input = include_non_input
        # Validate once at construction so a typo fails before hardware opens.
        _button_mapping(input_profile)
        self.input_profile = input_profile
        self.include_micro_preview = include_micro_preview
        self.synthesize_release = synthesize_release
        self.release_ms = release_ms
        self.on_record = on_record
        self.reports_seen = 0
        self.reports_forwarded = 0
        self.reports_ignored = 0
        self._lock = threading.Lock()

    def handle(
        self,
        report: Any,
        *,
        report_id: int = N4_REPORT_ID,
        timestamp: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Process one raw SDK report and return its emitted record."""

        raw = coerce_report(report)
        input_packet = is_n4_input_packet(raw)
        with self._lock:
            self.reports_seen += 1
        if not input_packet and not self.include_non_input:
            with self._lock:
                self.reports_ignored += 1
            return None

        record = report_record(
            raw,
            report_id=report_id,
            input_profile=self.input_profile,
            include_micro_preview=self.include_micro_preview,
            synthesize_release=self.synthesize_release,
            release_ms=self.release_ms,
            timestamp=timestamp,
        )
        if self.http is not None:
            # The HTTP bridge owns editable config + synthetic-release timing.
            # Send the normalized body and not a possible leading report ID.
            normalized, effective_report_id = normalize_n4_report(raw, report_id)
            record["post"] = self.http.post(normalized, report_id=effective_report_id)
        if self.jsonl is not None:
            self.jsonl.write(record)
        if self.on_record is not None:
            self.on_record(record)
        with self._lock:
            self.reports_forwarded += 1
        return record

    def close(self) -> None:
        if self.jsonl is not None:
            self.jsonl.close()

    def snapshot(self) -> dict[str, Any]:
        """Return stable counters for CLI/WebUI status reporting."""

        with self._lock:
            value = {
                "reportsSeen": self.reports_seen,
                "reportsForwarded": self.reports_forwarded,
                "reportsIgnored": self.reports_ignored,
                "inputProfile": self.input_profile,
            }
        http_snapshot = getattr(self.http, "snapshot", None)
        if callable(http_snapshot):
            value["http"] = http_snapshot()
        return value


def _add_sdk_path(sdk_path: Optional[os.PathLike[str] | str] = None) -> Path:
    """Add the bundled upstream SDK ``src`` directory to ``sys.path``."""

    module_root = Path(__file__).resolve().parents[1]
    configured = sdk_path or os.environ.get("MIRABOX_STREAMDOCK_SDK")
    if configured:
        candidate = Path(configured)
        # Accept either the SDK src directory or its parent Python-SDK folder.
        if (candidate / "StreamDock").is_dir():
            src = candidate
        elif (candidate / "src" / "StreamDock").is_dir():
            src = candidate / "src"
        else:
            src = candidate
    else:
        src = module_root / "upstream" / "StreamDock-Device-SDK" / "Python-SDK" / "src"
    if not (src / "StreamDock").is_dir():
        raise FileNotFoundError(
            f"StreamDock Python SDK not found at {src}. "
            "Pass --sdk-path or set MIRABOX_STREAMDOCK_SDK."
        )
    text = str(src)
    if text not in sys.path:
        sys.path.insert(0, text)
    return src


def load_device_manager(sdk_path: Optional[os.PathLike[str] | str] = None) -> Any:
    """Import and return the upstream ``DeviceManager`` class lazily."""

    _add_sdk_path(sdk_path)
    try:
        from StreamDock.DeviceManager import DeviceManager
    except Exception as error:  # pragma: no cover - depends on native DLLs
        raise RuntimeError(
            "Unable to import StreamDock Python SDK. Install its Python "
            "dependencies (Pillow; Windows pywin32/wmi are optional for "
            "polling) and ensure the bundled transport DLL is loadable."
        ) from error
    return DeviceManager


def _text(value: Any) -> Any:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="replace")
        except Exception:
            return value.hex()
    return value


def device_info(device: Any) -> dict[str, Any]:
    """Extract stable JSON metadata from an upstream SDK device object."""

    fields = (
        "path",
        "vendor_id",
        "product_id",
        "serial_number",
        "manufacturer_string",
        "product_string",
        "release_number",
        "usage_page",
        "usage",
        "interface_number",
    )
    result: dict[str, Any] = {}
    for field in fields:
        if hasattr(device, field):
            result[field] = _text(getattr(device, field))
    # StreamDock's base object keeps only a subset of enumeration metadata;
    # the per-device transport retains the original hid_device_info structure.
    transport_info = getattr(getattr(device, "transport", None), "_device_info", None)
    if transport_info is not None:
        for field in fields:
            if result.get(field) not in (None, "") or not hasattr(transport_info, field):
                continue
            value = getattr(transport_info, field)
            result[field] = _text(value)
    # Add the canonical names used by the WebUI without dropping SDK names.
    result.setdefault("vendorId", result.get("vendor_id"))
    result.setdefault("productId", result.get("product_id"))
    result["usagePage"] = result.get("usage_page") or N4_USAGE_PAGE
    result["usage"] = result.get("usage") or N4_USAGE
    return result


def _thread_is_alive(value: Any) -> bool:
    """Return a safe boolean for an SDK-owned thread-like object.

    The upstream SDK exposes ``read_thread`` and ``heartbeat_thread`` as
    ``threading.Thread`` instances, but alternate SDK revisions and test fakes
    may omit either attribute or expose a thread-like object whose
    ``is_alive()`` raises while it is being torn down.  Diagnostics must never
    make the bridge fail, so all non-conforming/exceptional values map to
    ``False``.
    """

    method = getattr(value, "is_alive", None)
    if not callable(method):
        return False
    try:
        return bool(method())
    except Exception:
        return False


class N4NativeAdapter:
    """Own one real N4 SDK device for serialized input and output."""

    def __init__(
        self,
        dispatcher: ReportDispatcher,
        *,
        sdk_path: Optional[os.PathLike[str] | str] = None,
        vendor_id: int = N4_VENDOR_ID,
        product_id: int = N4_PRODUCT_ID,
        device_path: Optional[str] = None,
        device_index: int = 0,
        initialize: bool = True,
        report_id: int = N4_REPORT_ID,
        status_stream: TextIO = sys.stderr,
        # ``device`` is intentionally injectable for tests and for hosts that
        # already own an SDK device object.  When omitted, normal VID/PID
        # enumeration is used.  The adapter still owns open/close for an
        # injected device, so output calls share the same lifecycle/lock.
        device: Any = None,
        manager_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.dispatcher = dispatcher
        self.sdk_path = sdk_path
        self.vendor_id = parse_int(vendor_id)
        self.product_id = parse_int(product_id)
        self.device_path = device_path
        self.device_index = int(device_index)
        self.initialize = bool(initialize)
        self.report_id = parse_int(report_id)
        self.status_stream = status_stream
        self._injected_device = device
        self.manager_factory = manager_factory
        self.manager: Any = None
        self.devices: list[Any] = []
        self.device: Any = None
        self.stop_event = threading.Event()
        # StreamDock's reader callback and output methods use the same HID
        # handle.  The upstream SDK does not expose a shared update lock, so
        # serialize open/init/output/close here.  RLock also permits a fake
        # device callback to call back into an adapter during a test.
        self._device_lock = threading.RLock()
        self._device_ready = threading.Event()
        self._opened = False
        # ``StreamDock.open()`` starts its reader before ``StreamDock.init()``
        # configures report sizes.  Real N4 devices use a 513-byte HID input
        # buffer (report ID byte + 512-byte body), so remember whether this
        # adapter used the ordered open path below.  It is also exposed in
        # diagnostics to make stale SDK behavior visible.
        self._open_mode = "sdk-open"

    def _status(self, message: str) -> None:
        if self.status_stream is not None:
            print(message, file=self.status_stream, flush=True)

    def enumerate(self) -> list[Any]:
        if self._injected_device is not None:
            self.manager = None
            self.devices = [self._injected_device]
            return self.devices
        if self.manager_factory is not None:
            self.manager = self.manager_factory()
        else:
            manager_cls = load_device_manager(self.sdk_path)
            self.manager = manager_cls()
        # The upstream manager enumerates all known products.  Filter exact
        # VID/PID so a second StreamDock does not get opened accidentally.
        all_devices = self.manager.enumerate()
        self.devices = [
            device
            for device in all_devices
            if int(getattr(device, "vendor_id", -1)) == self.vendor_id
            and int(getattr(device, "product_id", -1)) == self.product_id
        ]
        return self.devices

    def enumerate_json(self) -> list[dict[str, Any]]:
        return [device_info(device) for device in self.enumerate()]

    def select(self) -> Any:
        devices = self.enumerate()
        if self.device_path is not None:
            for device in devices:
                if str(getattr(device, "path", "")) == self.device_path:
                    return device
            raise RuntimeError(f"N4 path not found: {self.device_path}")
        if self.device_index < 0 or self.device_index >= len(devices):
            raise RuntimeError(
                f"N4 device index {self.device_index} is out of range; "
                f"found {len(devices)} matching device(s)"
            )
        return devices[self.device_index]

    @staticmethod
    def _device_path_bytes(device: Any) -> bytes:
        """Return an SDK transport path in the byte form expected by hidapi."""

        value = getattr(device, "path", None)
        if isinstance(value, bytes):
            return value
        if value is None:
            raise RuntimeError("N4 SDK device has no HID path")
        return os.fspath(value).encode("utf-8")

    @staticmethod
    def _stop_sdk_workers(
        device: Any,
        *,
        join_timeout: float = SDK_WORKER_JOIN_TIMEOUT_SECONDS,
    ) -> None:
        """Stop SDK reader/heartbeat workers before tearing down transport.

        The ordered-open path starts the SDK workers manually.  If one of the
        later startup steps fails (for example ``_start_heartbeat``), calling
        ``transport.close()`` directly while ``_read`` is still blocked can
        leave a worker using a freed HID handle.  Keep this cleanup helper
        deliberately duck-typed so it also works with older SDK revisions and
        test fakes.
        """

        for flag in ("run_heartbeat_thread", "run_read_thread"):
            try:
                if hasattr(device, flag):
                    setattr(device, flag, False)
            except Exception:
                # Cleanup must continue even if an alternate SDK exposes a
                # read-only/raising attribute.
                pass

        current = threading.current_thread()
        for name in ("heartbeat_thread", "read_thread"):
            try:
                worker = getattr(device, name, None)
                if worker is None or worker is current:
                    continue
                is_alive = getattr(worker, "is_alive", None)
                if callable(is_alive) and not is_alive():
                    continue
                join = getattr(worker, "join", None)
                if callable(join):
                    join(timeout=max(0.0, float(join_timeout)))
            except Exception:
                # A best-effort stop is preferable to closing the transport
                # from an exception path without attempting to quiesce the
                # workers at all.
                pass

    def _open_ordered_sdk_device(self, device: Any) -> bool:
        """Open an upstream SDK device without its report-size race.

        The stock ``StreamDock.open`` starts ``_read`` immediately and the
        N4-specific ``set_device`` call (which sets 513/1025/0 report sizes)
        only happens later from ``init``.  On Windows this can leave the first
        reader calls using an unconfigured transport and silently collapse
        timeout/communication failures into ``None``.  The upstream classes
        expose the needed lifecycle pieces, so configure the transport before
        starting their worker threads.  Injected test fakes and alternate SDK
        revisions fall back to the public ``open`` method.
        """

        transport = getattr(device, "transport", None)
        open_transport = getattr(transport, "open", None)
        set_device = getattr(device, "set_device", None)
        setup_reader = getattr(device, "_setup_reader", None)
        start_heartbeat = getattr(device, "_start_heartbeat", None)
        read_worker = getattr(device, "_read", None)
        if not all(callable(value) for value in (
            open_transport, set_device, setup_reader, start_heartbeat, read_worker,
        )):
            self._open_mode = "sdk-open"
            return bool(device.open())

        # Do not rely on LibUSBHIDAPI.open()'s boolean alone: the wrapper has
        # historically returned True after allocating a C transport wrapper
        # even when the underlying hid_open_path failed.  A non-null handle is
        # the strongest portable signal exposed by that wrapper; later
        # transport diagnostics report the raw HID error if reads still fail.
        opened = bool(open_transport(self._device_path_bytes(device)))
        handle = getattr(transport, "_handle", None)
        if not opened or not handle:
            try:
                self._stop_sdk_workers(device)
                close_transport = getattr(transport, "close", None)
                if callable(close_transport):
                    close_transport()
            finally:
                self._open_mode = "ordered-sdk-open-failed"
            return False

        try:
            # ``set_device`` is intentionally before _setup_reader.  It is
            # idempotent when ``init`` calls it again and now no longer races
            # the first transport_read call.
            set_device()
            if hasattr(device, "_notify_on_close"):
                device._notify_on_close = True
            setup_reader(read_worker)
            # Match StreamDock.open's short stabilization delay before the
            # heartbeat worker starts; this avoids output/read overlap during
            # the initial HID setup.
            time.sleep(0.1)
            start_heartbeat()
            self._open_mode = "ordered-sdk-open"
            return True
        except Exception:
            # Quiesce any worker that may have been started before closing the
            # raw transport.  Without this ordering a blocked SDK reader can
            # race a freed HID handle and survive into the next retry.
            try:
                self._stop_sdk_workers(device)
                close_transport = getattr(transport, "close", None)
                if callable(close_transport):
                    close_transport()
            finally:
                self._open_mode = "ordered-sdk-open-failed"
            raise

    def run(self, *, duration: Optional[float] = None) -> int:
        """Run until Ctrl+C/stop or an optional duration elapses."""

        # A completed run leaves the event set.  Clear it here so an adapter
        # instance can be deliberately reused after a clean stop.
        self.stop_event.clear()
        device = self.select()
        self.device = device

        def on_raw(_device: Any, data: Any) -> None:
            try:
                self.dispatcher.handle(data, report_id=self.report_id)
            except Exception as error:
                # Never let a malformed report terminate the SDK read thread.
                self._status(f"[n4] report dispatch error: {error}")

        try:
            # Register before open: StreamDock.open starts its reader thread.
            # Hold the same lock used by output wrappers so no image/light
            # write races the open/init sequence.
            with self._device_lock:
                device.set_raw_read_callback(on_raw)
                opened = self._open_ordered_sdk_device(device)
                if not opened:
                    raise RuntimeError("StreamDock SDK failed to open the N4")
                self._opened = True
                self._status(
                    f"[n4] opened {getattr(device, 'path', '?')} "
                    f"VID:PID={self.vendor_id:04x}:{self.product_id:04x}"
                )
                if self.initialize:
                    device.init()
                    self._status("[n4] SDK initialization complete")
                self._device_ready.set()
            deadline = None if duration is None else time.monotonic() + float(duration)
            while not self.stop_event.is_set():
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self.stop_event.wait(min(0.2, remaining))
                else:
                    self.stop_event.wait(0.2)
        finally:
            with self._device_lock:
                self._device_ready.clear()
                if self._opened:
                    self._opened = False
                    try:
                        device.close()
                    finally:
                        self._status("[n4] closed")
        return 0

    def stop(self) -> None:
        self.stop_event.set()

    # ------------------------------------------------------------------
    # Thread-safe output/control wrappers
    # ------------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        """Whether the selected SDK device is open and ready for output."""

        with self._device_lock:
            return bool(self._opened and self.device is not None)

    def wait_until_ready(self, timeout: Optional[float] = None) -> bool:
        """Wait until :meth:`run` has opened (and optionally initialized) N4."""

        return self._device_ready.wait(timeout)

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-safe lifecycle snapshot without performing I/O."""

        with self._device_lock:
            device = self.device
            device_path = getattr(device, "path", None) if device is not None else None
            if device_path is not None:
                try:
                    device_path = os.fspath(device_path)
                except TypeError:
                    device_path = str(device_path)
            # StreamDock starts these workers from ``open()``.  Keep the
            # fields deliberately boolean and object-free so they can be
            # forwarded through the WebUI heartbeat without leaking callback
            # objects or SDK internals.  They are useful for distinguishing a
            # genuinely active HID reader from an opened-but-stalled handle.
            reader_thread = getattr(device, "read_thread", None) if device is not None else None
            heartbeat_thread = getattr(device, "heartbeat_thread", None) if device is not None else None
            raw_callback = getattr(device, "raw_read_callback", None) if device is not None else None
            return {
                "transport": "streamdock-sdk",
                "opened": bool(self._opened and device is not None),
                "ready": bool(self._device_ready.is_set()),
                "path": device_path,
                "vendorId": int(self.vendor_id),
                "productId": int(self.product_id),
                "deviceIndex": int(self.device_index),
                "devicePathRequested": self.device_path,
                "initialized": bool(self.initialize),
                "openMode": self._open_mode,
                "reader": {
                    "threadAlive": _thread_is_alive(reader_thread),
                    "runFlag": bool(getattr(device, "run_read_thread", False)) if device is not None else False,
                    # A callback may remain stored by an SDK object after
                    # close(); report it as registered only while this adapter
                    # still owns an opened handle.
                    "rawCallbackRegistered": bool(self._opened and callable(raw_callback)),
                },
                "heartbeat": {
                    "threadAlive": _thread_is_alive(heartbeat_thread),
                    "runFlag": bool(getattr(device, "run_heartbeat_thread", False)) if device is not None else False,
                },
                # Keep ``transport`` a stable discriminator shared with the
                # direct-hidapi fallback.  Detailed SDK internals live under
                # a separate field so the WebUI/API contract never receives
                # an object where callers expect the string ``streamdock-sdk``.
                "transportDetails": self._transport_snapshot(device),
                # Process-presence hint only; this does not prove ownership of
                # a particular kernel HID handle.
                "externalOwnerHints": detect_external_owner_hints(),
            }

    @staticmethod
    def _transport_snapshot(device: Any) -> dict[str, Any]:
        """Return safe, best-effort transport diagnostics for WebUI status."""

        transport = getattr(device, "transport", None) if device is not None else None
        if transport is None:
            return {
                "present": False,
                "handlePresent": False,
                "isOpen": False,
                "inputReportSize": None,
                "outputReportSize": None,
                "featureReportSize": None,
                "reportId": None,
                "lastError": None,
            }

        def integer(name: str) -> Optional[int]:
            try:
                value = getattr(transport, name)
                value = value() if callable(value) else value
                return int(value) if value is not None else None
            except Exception:
                return None

        last_error: Optional[str] = None
        getter = getattr(transport, "get_last_error", None)
        if callable(getter):
            try:
                value = getter()
                if value not in (None, ""):
                    last_error = str(value)
            except Exception:
                last_error = None
        return {
            "present": True,
            "handlePresent": bool(getattr(transport, "_handle", None)),
            "isOpen": bool(getattr(transport, "_is_open", False)),
            "inputReportSize": integer("input_report_size"),
            "outputReportSize": integer("output_report_size"),
            "featureReportSize": integer("feature_report_size"),
            "reportId": integer("get_report_id"),
            "lastError": last_error,
        }

    def _call_device(self, method_names: str | Sequence[str], *args: Any, **kwargs: Any) -> Any:
        """Call one SDK method while holding the adapter's device lock."""

        names = (method_names,) if isinstance(method_names, str) else tuple(method_names)
        with self._device_lock:
            device = self.device
            if not self._opened or device is None:
                raise RuntimeError("N4 device is not open")
            for name in names:
                method = getattr(device, name, None)
                if callable(method):
                    return method(*args, **kwargs)
        joined = ", ".join(names)
        raise NotImplementedError(f"N4 SDK device does not expose {joined}")

    @staticmethod
    def _logical_key(value: Any, *, label: str = "logical key") -> int:
        """Normalize a Python SDK ``ButtonKey`` or integer key number."""

        candidate = getattr(value, "value", value)
        try:
            number = parse_int(candidate)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{label} must be an integer") from error
        if number < 1 or number > 14:
            raise ValueError(f"{label} must be between 1 and 14")
        return number

    @staticmethod
    def _path_value(path: Any) -> str:
        try:
            return os.fspath(path)
        except TypeError as error:
            raise TypeError("image path must be path-like") from error

    def set_touchscreen_image(self, path: Any) -> Any:
        """Set the 800x480 N4 touchscreen/background image via the SDK."""

        return self._call_device("set_touchscreen_image", self._path_value(path))

    def set_screen_image(self, path: Any) -> Any:
        """Alias for :meth:`set_touchscreen_image` used by renderers."""

        return self.set_touchscreen_image(path)

    def set_key_image(self, logical_key: Any, path: Any) -> Any:
        """Set one logical N4 key image (1..10 main, 11..14 secondary)."""

        key = self._logical_key(logical_key)
        return self._call_device("set_key_image", key, self._path_value(path))

    def supports_native_frames(self) -> bool:
        with self._device_lock:
            return (self.device is not None and type(self.device).__name__=='StreamDockN4'
                    and callable(getattr(getattr(self.device,'transport',None),'set_key_image_stream',None)))

    def set_key_frame(self,logical_key: Any,data: bytes) -> Any:
        """Send already oriented/encoded N4 bytes through the official stream API."""
        key=self._logical_key(logical_key)
        if not isinstance(data,bytes) or not data.startswith(b'\xff\xd8'):raise ValueError('Native JPEG bytes required')
        with self._device_lock:
            if not self._opened or not self._device_ready.is_set():raise RuntimeError('N4 is not ready')
            if not self.supports_native_frames():raise NotImplementedError('Native frames unavailable')
            hardware=(11,12,13,14,15,6,7,8,9,10,1,2,3,4)[key-1]
            result=self.device.transport.set_key_image_stream(data,hardware)
            # transport_c.h defines unsigned error categories, not negative errno.
            if result is None or int(result)!=0:raise RuntimeError(f'N4 frame transport failed: {result}')
            return result

    def set_main_key_image(self, key: Any, path: Any) -> Any:
        """Set a 112x112 main-screen key image (logical key 1..10)."""

        number = self._logical_key(key, label="main key")
        if number > 10:
            raise ValueError("main key must be between 1 and 10")
        return self.set_key_image(number, path)

    def set_second_screen_image(self, key: Any, path: Any) -> Any:
        """Set a 176x112 secondary-screen image.

        ``key`` may be a secondary slot number (1..4) or the SDK logical key
        number (11..14).  The upstream N4 Python SDK spells its dedicated
        method ``set_seondscreen_image``; both that spelling and a corrected
        spelling are accepted so fakes/alternate SDK revisions work.
        """

        candidate = getattr(key, "value", key)
        try:
            number = parse_int(candidate)
        except (TypeError, ValueError) as error:
            raise ValueError("secondary key must be an integer") from error
        if 1 <= number <= 4:
            logical_key = number + 10
        elif 11 <= number <= 14:
            logical_key = number
        else:
            raise ValueError("secondary key must be a slot 1..4 or logical key 11..14")
        image_path = self._path_value(path)
        return self._call_device(
            ("set_seondscreen_image", "set_second_screen_image"),
            logical_key,
            image_path,
        )

    # British/SDK spelling aliases retained for callers that mirror upstream.
    set_seondscreen_image = set_second_screen_image
    set_secondary_screen_image = set_second_screen_image

    def refresh(self) -> Any:
        """Commit pending image updates through the SDK transport."""

        return self._call_device("refresh")

    def set_brightness(self, percent: Any) -> Any:
        """Set N4 screen brightness (SDK range: integer 0..100 percent)."""

        value = parse_int(percent)
        if value < 0 or value > 100:
            raise ValueError("brightness percent must be between 0 and 100")
        return self._call_device("set_brightness", value)

    def lighting_capabilities(self) -> dict[str, Any]:
        """Return SDK-reported RGB capability and available control methods."""

        with self._device_lock:
            device = self.device
            if device is None:
                return {"open": False, "hasRGBLed": False, "ledCounts": 0, "methods": []}
            feature = getattr(device, "feature_option", None)
            has_rgb = bool(getattr(feature, "hasRGBLed", False))
            # The base Python SDK defines all four RGB methods on every
            # StreamDock object, but they are intentional no-ops when
            # ``feature_option.hasRGBLed`` is false (ordinary N4).  Do not
            # advertise those inherited methods as usable capabilities.
            try:
                count = int(getattr(feature, "ledCounts", 0) or 0)
            except (TypeError, ValueError):
                count = 0
            if not has_rgb:
                count = 0
            support_single = bool(getattr(feature, "support_single_led_color", False))
            methods: list[str] = []
            if has_rgb:
                # Preserve the SDK method order used by the previous
                # capability report while filtering unsupported entries.
                for name in (
                    "set_led_brightness",
                    "set_led_color",
                    "set_single_led_color",
                    "reset_led_effect",
                ):
                    if name == "set_single_led_color" and not support_single:
                        continue
                    if callable(getattr(device, name, None)):
                        methods.append(name)
            return {
                "open": bool(self._opened),
                "hasRGBLed": has_rgb,
                "ledCounts": count,
                "methods": methods,
            }

    def _call_rgb_device(
        self,
        method_names: str | Sequence[str],
        *args: Any,
        require_single: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Call an RGB SDK method only when the device advertises RGB.

        The upstream Python SDK exposes RGB methods on the common base class
        even for devices without physical LEDs.  Those methods silently
        return ``None`` when ``feature_option.hasRGBLed`` is false, which is
        easy for a caller to mistake for a successful write.  Keep the same
        lifecycle/serialization guarantees as :meth:`_call_device`, but make
        unsupported hardware explicit with ``NotImplementedError``.
        """

        names = (method_names,) if isinstance(method_names, str) else tuple(method_names)
        with self._device_lock:
            device = self.device
            if not self._opened or device is None:
                raise RuntimeError("N4 device is not open")
            feature = getattr(device, "feature_option", None)
            if not bool(getattr(feature, "hasRGBLed", False)):
                joined = ", ".join(names)
                raise NotImplementedError(
                    f"N4 device does not advertise RGB LED support; {joined} is unavailable"
                )
            if require_single and not bool(
                getattr(feature, "support_single_led_color", False)
            ):
                raise NotImplementedError(
                    "N4 device does not advertise per-LED RGB color support"
                )
            for name in names:
                method = getattr(device, name, None)
                if callable(method):
                    return method(*args, **kwargs)
        joined = ", ".join(names)
        raise NotImplementedError(f"N4 SDK device does not expose {joined}")

    def set_led_brightness(self, value: Any) -> Any:
        """Set RGB/under-glow LED brightness when the SDK/device supports it."""

        brightness = parse_int(value)
        if brightness < 0 or brightness > 100:
            raise ValueError("LED brightness must be between 0 and 100")
        return self._call_rgb_device("set_led_brightness", brightness)

    @staticmethod
    def _rgb(value: Any, *, label: str = "color") -> tuple[int, int, int]:
        if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
            raise ValueError(f"{label} must contain exactly three channels")
        if len(value) != 3:
            raise ValueError(f"{label} must contain exactly three channels")
        channels = tuple(parse_int(channel) for channel in value)
        if any(channel < 0 or channel > 255 for channel in channels):
            raise ValueError(f"{label} channels must be between 0 and 255")
        return channels  # type: ignore[return-value]

    def set_led_color(self, *args: Any) -> Any:
        """Set one RGB color; accepts ``(r,g,b)`` or ``((r,g,b),)``."""

        if len(args) == 1:
            color = self._rgb(args[0])
        elif len(args) == 3:
            color = self._rgb(args)
        else:
            raise TypeError("set_led_color expects (r, g, b) or one RGB sequence")
        return self._call_rgb_device("set_led_color", *color)

    def set_single_led_color(self, colors: Sequence[Sequence[int]]) -> Any:
        """Set individual RGB colors when the SDK exposes per-LED control."""

        if isinstance(colors, (str, bytes, bytearray)) or not isinstance(colors, Sequence):
            raise ValueError("colors must be a sequence of RGB triples")
        normalized = [self._rgb(color, label=f"colors[{index}]") for index, color in enumerate(colors)]
        if not normalized:
            raise ValueError("colors must not be empty")
        return self._call_rgb_device(
            "set_single_led_color", normalized, require_single=True
        )

    def reset_led_effect(self) -> Any:
        """Reset RGB/under-glow LED effects when supported by the SDK."""

        return self._call_rgb_device("reset_led_effect")


def _default_fixture_reports() -> list[bytes]:
    """Return deterministic ACK reports covering all N4 knob directions."""

    reports: list[bytes] = []
    for code in (0xA0, 0xA1, 0x50, 0x51, 0x90, 0x91, 0x70, 0x71):
        report = bytearray(N4_INPUT_REPORT_SIZE)
        report[:7] = b"ACK\x00\x00OK"
        report[9] = code
        report[10] = 0
        reports.append(bytes(report))
    for code in N4_KNOB_PRESS:
        report = bytearray(N4_INPUT_REPORT_SIZE)
        report[:7] = b"ACK\x00\x00OK"
        report[9] = code
        report[10] = 1
        reports.append(bytes(report))
    return reports


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Native Mirabox N4 listener/bridge (no driver installation)."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--enumerate", "--list", action="store_true", help="list matching N4 HID devices and exit")
    mode.add_argument("--dry-run", action="store_true", help="decode fixture/hex reports without opening hardware")
    mode.add_argument(
        "--hidapi-probe",
        action="store_true",
        help="read-only direct-hidapi N4 enumeration (never opens a device)",
    )
    parser.add_argument("--hex", dest="hex_reports", action="append", default=[], metavar="HEX", help="dry-run report bytes; repeat for multiple reports")
    parser.add_argument(
        "--post",
        nargs="?",
        const=DEFAULT_BRIDGE_URL,
        metavar="URL",
        help="POST reports to WebUI endpoint (default when omitted: %(const)s)",
    )
    parser.add_argument("--post-timeout", type=float, default=2.0, metavar="SEC", help="HTTP POST timeout (default: 2)")
    parser.add_argument("--jsonl", action="store_true", help="write JSONL records to stdout")
    parser.add_argument("--jsonl-file", metavar="PATH", help="write JSONL records to a file")
    parser.add_argument("--all-reports", action="store_true", help="include non-ACK/status reports (default forwards input packets only)")
    parser.add_argument(
        "--input-profile",
        choices=tuple(N4_BUTTON_PROFILES),
        default=DEFAULT_BUTTON_PROFILE,
        help="N4 button input-code profile (default: cpp)",
    )
    parser.add_argument("--no-preview", action="store_true", help="omit local Micro event preview from JSONL")
    parser.add_argument("--preview-release", action="store_true", help="include a local synthetic encoder release in preview")
    parser.add_argument("--release-ms", type=int, default=100, help="preview synthetic release delay (default: 100)")
    parser.add_argument("--sdk-path", help="StreamDock Python-SDK/src directory or its parent")
    parser.add_argument(
        "--hidapi", "--hidapi-input",
        dest="hidapi",
        action="store_true",
        help=(
            "explicit input-only fallback using Python hidapi; default remains "
            "the StreamDock SDK"
        ),
    )
    parser.add_argument(
        "--hidapi-path",
        metavar="PATH",
        help="exact verified N4 hidapi path (requires --hidapi or --hidapi-probe)",
    )
    parser.add_argument(
        "--hidapi-read-timeout-ms",
        type=int,
        default=100,
        metavar="MS",
        help="direct hidapi read timeout (default: 100 ms)",
    )
    parser.add_argument("--vid", default=f"0x{N4_VENDOR_ID:04x}", help="N4 vendor ID (default: 0x6602)")
    parser.add_argument("--pid", default=f"0x{N4_PRODUCT_ID:04x}", help="N4 product ID (default: 0x1001)")
    parser.add_argument("--report-id", default="0", help="HID report ID sent to WebUI (default: 0)")
    parser.add_argument("--device-path", help="open this exact SDK device path")
    parser.add_argument("--device-index", type=int, default=0, help="matching N4 index (default: 0)")
    parser.add_argument("--no-init", action="store_true", help="open/read without SDK wake/brightness/clear/refresh init")
    parser.add_argument("--duration", type=float, help="stop after this many seconds")
    parser.add_argument("--quiet", action="store_true", help="suppress status messages on stderr")
    return parser


def _make_dispatcher(args: argparse.Namespace) -> tuple[ReportDispatcher, list[Any]]:
    opened_streams: list[Any] = []
    jsonl_sink: Optional[JsonlSink] = None
    if args.jsonl_file:
        stream = open(args.jsonl_file, "a", encoding="utf-8")
        opened_streams.append(stream)
        jsonl_sink = JsonlSink(stream, close_stream=False)
    elif args.jsonl or not args.post:
        jsonl_sink = JsonlSink(sys.stdout)

    http_sink = HttpReporter(args.post, timeout=args.post_timeout) if args.post else None
    dispatcher = ReportDispatcher(
        jsonl=jsonl_sink,
        http=http_sink,
        include_non_input=args.all_reports,
        input_profile=args.input_profile,
        include_micro_preview=not args.no_preview,
        synthesize_release=args.preview_release,
        release_ms=args.release_ms,
    )
    return dispatcher, opened_streams


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        args.vid = parse_int(args.vid)
        args.pid = parse_int(args.pid)
        args.report_id = parse_int(args.report_id)
    except ValueError as error:
        parser.error(str(error))

    if args.hidapi_probe:
        args.hidapi = True
    if args.hidapi_path and not args.hidapi:
        parser.error("--hidapi-path requires --hidapi or --hidapi-probe")
    if args.hidapi and (args.hidapi_read_timeout_ms < 1 or args.hidapi_read_timeout_ms > 5000):
        parser.error("--hidapi-read-timeout-ms must be between 1 and 5000")
    if args.hidapi and args.device_path and args.hidapi_path:
        parser.error("use only one of --device-path and --hidapi-path")

    if args.hidapi_probe:
        from n4_hidapi import probe_n4_hidapi

        result = probe_n4_hidapi(
            vendor_id=args.vid,
            product_id=args.pid,
            path=args.hidapi_path or args.device_path,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 2

    if args.enumerate:
        if args.hidapi:
            from n4_hidapi import probe_n4_hidapi

            result = probe_n4_hidapi(
                vendor_id=args.vid,
                product_id=args.pid,
                path=args.hidapi_path or args.device_path,
            )
            print(json.dumps(result.get("candidates", []), ensure_ascii=False, indent=2))
            return 0
        adapter = N4NativeAdapter(
            ReportDispatcher(on_record=lambda _record: None),
            sdk_path=args.sdk_path,
            vendor_id=args.vid,
            product_id=args.pid,
            status_stream=None if args.quiet else sys.stderr,
        )
        print(json.dumps(adapter.enumerate_json(), ensure_ascii=False, indent=2))
        return 0

    dispatcher, opened_streams = _make_dispatcher(args)
    try:
        if args.dry_run:
            reports = [parse_hex_report(text) for text in args.hex_reports]
            if not reports:
                reports = _default_fixture_reports()
            for report in reports:
                dispatcher.handle(report, report_id=args.report_id)
            return 0

        status_stream = None if args.quiet else sys.stderr
        if args.hidapi:
            from n4_hidapi import DirectHidapiN4Reader

            adapter = DirectHidapiN4Reader(
                dispatcher,
                vendor_id=args.vid,
                product_id=args.pid,
                device_path=args.hidapi_path or args.device_path,
                device_index=args.device_index,
                report_id=args.report_id,
                read_timeout_ms=args.hidapi_read_timeout_ms,
                status_stream=status_stream,
            )
            if not args.quiet and not args.no_init:
                print(
                    "[n4/hidapi] input-only fallback selected; SDK initialization/output are disabled",
                    file=sys.stderr,
                    flush=True,
                )
        else:
            adapter = N4NativeAdapter(
                dispatcher,
                sdk_path=args.sdk_path,
                vendor_id=args.vid,
                product_id=args.pid,
                device_path=args.device_path,
                device_index=args.device_index,
                initialize=not args.no_init,
                report_id=args.report_id,
                status_stream=status_stream,
            )
        try:
            return adapter.run(duration=args.duration)
        except KeyboardInterrupt:
            adapter.stop()
            return 0
    finally:
        dispatcher.close()
        for stream in opened_streams:
            try:
                stream.close()
            except OSError:
                pass


if __name__ == "__main__":  # pragma: no cover - exercised via CLI smoke tests
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"n4-native-adapter: {error}", file=sys.stderr)
        raise SystemExit(1)
