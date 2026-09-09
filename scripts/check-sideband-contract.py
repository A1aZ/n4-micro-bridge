#!/usr/bin/env python3
"""Offline consistency checks for the Codex Micro sideband scaffold.

The checker deliberately does not invoke MSBuild, SetupAPI, CreateFile, or any
driver installation command.  It parses the reviewable C contract and project
metadata, imports the pure-Python constants, and reports drift before a WDK is
available.  It is useful in CI and on a development machine where installing a
driver is not yet safe or desirable.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable


EXPECTED = {
    "MIRABOX_CODEX_MICRO_VID": 0x303A,
    "MIRABOX_CODEX_MICRO_PID": 0x8360,
    "MIRABOX_CODEX_MICRO_RELEASE": 0x0100,
    "MIRABOX_CODEX_MICRO_USAGE_PAGE": 0xFF00,
    "MIRABOX_CODEX_MICRO_USAGE": 0x0001,
    "MIRABOX_CODEX_MICRO_REPORT_ID": 0x06,
    "MIRABOX_CODEX_MICRO_REPORT_LENGTH": 64,
    "MIRABOX_CODEX_MICRO_BODY_LENGTH": 63,
    "MIRABOX_CODEX_MICRO_MESSAGE_TYPE": 0x02,
    "MIRABOX_CODEX_MICRO_PAYLOAD_MAX": 61,
}

# The companion collection is deliberately checked separately from the active
# single-TLC Codex descriptor.  Keeping these values in a second map makes it
# obvious that adding a descriptor scaffold must not silently change the
# currently installed/advertised collection.
EXPECTED_COMPANION = {
    "MIRABOX_CODEX_COMPANION_USAGE_PAGE": 0xFF70,
    "MIRABOX_CODEX_COMPANION_USAGE": 0x0001,
    "MIRABOX_CODEX_COMPANION_REPORT_ID": 0x07,
    "MIRABOX_CODEX_COMPANION_REPORT_LENGTH": 64,
    "MIRABOX_CODEX_COMPANION_BODY_LENGTH": 63,
}

EXPECTED_GUID = "{7F5A2E91-1C0E-4D1E-9C2A-1A6B8E537044}"
EXPECTED_DESCRIPTOR = [
    0x06, 0x00, 0xFF, 0x09, 0x01, 0xA1, 0x01, 0x85, 0x06,
    0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08, 0x95, 0x3F,
    0x09, 0x01, 0x81, 0x02, 0x95, 0x3F, 0x09, 0x02, 0x91, 0x02, 0xC0,
]
EXPECTED_COMPANION_DESCRIPTOR = [
    0x06, 0x70, 0xFF, 0x09, 0x01, 0xA1, 0x01, 0x85, 0x07,
    0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08, 0x95, 0x3F,
    0x09, 0x01, 0x81, 0x02, 0x95, 0x3F, 0x09, 0x02, 0x91, 0x02, 0xC0,
]
EXPECTED_DUAL_DESCRIPTOR = EXPECTED_DESCRIPTOR + EXPECTED_COMPANION_DESCRIPTOR

IOCTL_NAMES = (
    "IOCTL_MIRABOX_CODEX_PUSH_INPUT",
    "IOCTL_MIRABOX_CODEX_READ_OUTPUT",
    "IOCTL_MIRABOX_CODEX_GET_INFO",
    "IOCTL_MIRABOX_CODEX_RESET",
)

_MACROS = {
    "FILE_DEVICE_UNKNOWN": 0x22,
    "METHOD_BUFFERED": 0,
    "FILE_READ_ACCESS": 0x1,
    "FILE_WRITE_ACCESS": 0x2,
    "FILE_ANY_ACCESS": 0,
}


def _strip_comments(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"//[^\r\n]*", "", source)


def _parse_int(token: str, values: dict[str, int] | None = None) -> int:
    """Parse the small integer expressions used by the protocol header."""

    token = token.strip()
    # Remove balanced casts/parentheses used by the descriptor-length macro.
    token = re.sub(r"\b(?:UCHAR|USHORT|ULONG|UINT|unsigned|long|int)\b", "", token)
    token = token.replace("(", "").replace(")", "").strip()
    token = re.sub(r"[uUlL]+$", "", token)
    if values and token in values:
        return int(values[token])
    if token in _MACROS:
        return _MACROS[token]
    if token.startswith("sizeof"):
        raise ValueError(f"sizeof expression is not a scalar integer: {token}")
    return int(token, 0)


def _parse_defines(source: str, names: Iterable[str]) -> dict[str, int]:
    source = _strip_comments(source)
    # Join preprocessor continuation lines before looking for scalar values.
    source = re.sub(r"\\\s*\r?\n", " ", source)
    values: dict[str, int] = {}
    for name in names:
        match = re.search(rf"^\s*#define\s+{re.escape(name)}\s+([^\r\n]+)", source, re.M)
        if not match:
            raise ValueError(f"missing #define {name}")
        values[name] = _parse_int(match.group(1), {**_MACROS, **values})
    return values


def _parse_ioctls(source: str) -> dict[str, dict[str, int]]:
    source = _strip_comments(source)
    source = re.sub(r"\\\s*\r?\n", " ", source)
    result: dict[str, dict[str, int]] = {}
    for name in IOCTL_NAMES:
        match = re.search(
            rf"^\s*#define\s+{re.escape(name)}\s+CTL_CODE\s*\(([^)]*)\)",
            source,
            re.M,
        )
        if not match:
            raise ValueError(f"missing CTL_CODE for {name}")
        args = [part.strip() for part in match.group(1).split(",")]
        if len(args) != 4:
            raise ValueError(f"{name} CTL_CODE has {len(args)} arguments")
        device_type, function, method, access = (
            _parse_int(part) for part in args
        )
        value = (
            ((device_type & 0xFFFF) << 16)
            | ((access & 0x3) << 14)
            | ((function & 0xFFF) << 2)
            | (method & 0x3)
        )
        result[name] = {
            "deviceType": device_type,
            "function": function,
            "method": method,
            "access": access,
            "value": value,
        }
    return result


def _parse_guid(source: str) -> str:
    source = _strip_comments(source)
    match = re.search(
        r"DEFINE_GUID\s*\(\s*GUID_DEVINTERFACE_MIRABOX_CODEX_MICRO_BRIDGE\s*,"
        r"(.*?)\)",
        source,
        re.S,
    )
    if not match:
        raise ValueError("missing bridge DEFINE_GUID")
    parts = [part.strip() for part in match.group(1).split(",")]
    if len(parts) != 11:
        raise ValueError(f"bridge GUID has {len(parts)} fields")
    numbers = [_parse_int(part) for part in parts]
    data1, data2, data3 = numbers[:3]
    data4 = numbers[3:]
    if not all(0 <= value <= 0xFF for value in data4):
        raise ValueError("bridge GUID Data4 contains a value wider than one byte")
    return (
        f"{{{data1:08X}-{data2:04X}-{data3:04X}-"
        f"{data4[0]:02X}{data4[1]:02X}-"
        f"{''.join(f'{value:02X}' for value in data4[2:])}}}"
    )


def _parse_hex_array(source: str, symbol: str) -> list[int]:
    source = _strip_comments(source)
    match = re.search(
        rf"\b{re.escape(symbol)}\s*\[\s*\]\s*=\s*\{{(.*?)\}}\s*;",
        source,
        re.S,
    )
    if not match:
        raise ValueError(f"missing byte array {symbol}")
    values = [int(item, 16) for item in re.findall(r"0x([0-9A-Fa-f]{1,2})", match.group(1))]
    if not values:
        raise ValueError(f"byte array {symbol} is empty")
    return values


def _extract_function_body(source: str, name: str) -> str | None:
    """Return one C function body using a small brace matcher.

    This is intentionally not a C parser.  The sideband scaffold uses plain
    function definitions, so matching the opening declaration and balancing
    braces is enough for a useful offline race/dispatch check without a WDK.
    Strings and comments are skipped while balancing to avoid being fooled by
    diagnostic text containing braces.
    """

    # Mask comments without changing offsets, then match the declaration.
    # Keeping offsets stable lets the brace scan return the original body.
    declaration_source = re.sub(
        r"/\*.*?\*/|//[^\r\n]*",
        lambda match: "".join("\n" if char in "\r\n" else " "
                               for char in match.group(0)),
        source,
        flags=re.S,
    )
    match = re.search(
        rf"(?:^|\n)\s*(?:static\s+)?(?:NTSTATUS|BOOLEAN|VOID)\s+"
        rf"{re.escape(name)}\s*\([^;{{]*?\)\s*\{{",
        declaration_source,
        re.S,
    )
    if not match:
        return None
    start = match.end() - 1
    depth = 0
    index = start
    in_line_comment = False
    in_block_comment = False
    in_string = False
    in_char = False
    escaped = False
    while index < len(source):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""
        if in_line_comment:
            if char in "\r\n":
                in_line_comment = False
        elif in_block_comment:
            if char == "*" and next_char == "/":
                in_block_comment = False
                index += 1
        elif in_string or in_char:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif (in_string and char == '"') or (in_char and char == "'"):
                in_string = False
                in_char = False
        else:
            if char == "/" and next_char == "/":
                in_line_comment = True
                index += 1
            elif char == "/" and next_char == "*":
                in_block_comment = True
                index += 1
            elif char == '"':
                in_string = True
            elif char == "'":
                in_char = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return source[start + 1:index]
        index += 1
    return None


def _load_python_module(path: Path, module_name: str = "_codexmicro_sideband_contract") -> Any:
    source_dir = str(path.parent)
    if source_dir not in sys.path:
        # The sideband module has small pure-Python siblings (for example the
        # in-memory fs.* store). Import it with the same package search path as
        # the normal CLI wrapper instead of relying on the caller's cwd.
        sys.path.insert(0, source_dir)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load Python module from {path}")
    module = importlib.util.module_from_spec(spec)
    # dataclasses (and a few other standard-library helpers) look the module
    # up in sys.modules while executing class decorators.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _add_issue(issues: list[str], message: str) -> None:
    issues.append(message)


def check_contract(root: Path) -> dict[str, Any]:
    """Return a JSON-serialisable contract report for ``root``."""

    root = Path(root).resolve()
    protocol_path = root / "driver" / "codexmicro-umdf" / "protocol" / "codexmicro_protocol.h"
    driver_path = root / "driver" / "codexmicro-umdf" / "driver" / "codexmicro.c"
    bridge_path = root / "driver" / "codexmicro-umdf" / "driver" / "bridge.c"
    header_path = root / "driver" / "codexmicro-umdf" / "driver" / "codexmicro.h"
    common_path = root / "driver" / "codexmicro-umdf" / "inc" / "common.h"
    project_path = root / "driver" / "codexmicro-umdf" / "driver" / "umdf2" / "CodexMicroUm.vcxproj"
    inf_path = root / "driver" / "codexmicro-umdf" / "driver" / "umdf2" / "CodexMicroUm.inx"
    python_path = root / "src" / "codexmicro_sideband.py"
    companion_python_path = root / "src" / "codexmicro_companion.py"

    issues: list[str] = []
    active_mode = "unknown"
    report: dict[str, Any] = {
        "root": str(root),
        "files": {},
        "constants": {},
        "ioctl": {},
        "descriptor": {},
        "guid": None,
        "issues": issues,
    }

    paths = {
        "protocol": protocol_path,
        "driver": driver_path,
        "bridge": bridge_path,
        "header": header_path,
        "common": common_path,
        "project": project_path,
        "inf": inf_path,
        "python": python_path,
        "companionPython": companion_python_path,
    }
    for label, path in paths.items():
        if not path.is_file():
            _add_issue(issues, f"missing {label} file: {path}")
            report["files"][label] = {"path": str(path), "exists": False}
        else:
            report["files"][label] = {"path": str(path), "exists": True}

    if issues:
        report["ok"] = False
        return report

    protocol = protocol_path.read_text(encoding="utf-8")
    driver = driver_path.read_text(encoding="utf-8")
    bridge = bridge_path.read_text(encoding="utf-8")
    header = header_path.read_text(encoding="utf-8")
    common = common_path.read_text(encoding="utf-8")
    project = project_path.read_text(encoding="utf-8")
    inf = inf_path.read_text(encoding="utf-8")
    companion_python = companion_python_path.read_text(encoding="utf-8")

    try:
        c_values = _parse_defines(protocol, EXPECTED)
        report["constants"]["c"] = c_values
    except (ValueError, TypeError) as exc:
        _add_issue(issues, f"protocol constants: {exc}")
        c_values = {}

    try:
        c_companion_values = _parse_defines(protocol, EXPECTED_COMPANION)
        report["constants"]["companionC"] = c_companion_values
    except (ValueError, TypeError) as exc:
        _add_issue(issues, f"companion protocol constants: {exc}")
        c_companion_values = {}

    try:
        ioctl_values = _parse_ioctls(protocol)
        report["ioctl"]["c"] = ioctl_values
    except (ValueError, TypeError) as exc:
        _add_issue(issues, f"protocol IOCTLs: {exc}")
        ioctl_values = {}

    try:
        guid = _parse_guid(protocol)
        report["guid"] = {"c": guid}
        if guid != EXPECTED_GUID:
            _add_issue(issues, f"bridge GUID is {guid}; expected {EXPECTED_GUID}")
    except (ValueError, TypeError) as exc:
        _add_issue(issues, f"protocol GUID: {exc}")

    try:
        protocol_descriptor = _parse_hex_array(protocol, "MIRABOX_CODEX_MICRO_REPORT_DESCRIPTOR")
        driver_descriptor = _parse_hex_array(driver, "G_DefaultReportDescriptor")
        companion_descriptor = _parse_hex_array(
            protocol, "MIRABOX_CODEX_COMPANION_REPORT_DESCRIPTOR"
        )
        dual_descriptor = _parse_hex_array(
            protocol, "MIRABOX_CODEX_MICRO_DUAL_REPORT_DESCRIPTOR"
        )
        report["descriptor"] = {
            "protocol": protocol_descriptor,
            "driver": driver_descriptor,
            "expected": EXPECTED_DESCRIPTOR,
            "length": len(protocol_descriptor),
            "activeLength": len(driver_descriptor),
        }
        report["companionDescriptor"] = {
            "protocol": companion_descriptor,
            "expected": EXPECTED_COMPANION_DESCRIPTOR,
            "length": len(companion_descriptor),
            "active": False,
        }
        report["dualDescriptor"] = {
            "protocol": dual_descriptor,
            "expected": EXPECTED_DUAL_DESCRIPTOR,
            "length": len(dual_descriptor),
            "active": False,
        }
        if protocol_descriptor != EXPECTED_DESCRIPTOR:
            _add_issue(issues, "protocol report descriptor differs from Codex Micro descriptor")
        if driver_descriptor == EXPECTED_DESCRIPTOR:
            active_mode = "single-tlc"
            report["descriptor"]["activeMode"] = active_mode
        elif driver_descriptor == EXPECTED_DUAL_DESCRIPTOR:
            active_mode = "dual-tlc"
            report["descriptor"]["activeMode"] = active_mode
            report["companionDescriptor"]["active"] = True
            report["dualDescriptor"]["active"] = True
        else:
            report["descriptor"]["activeMode"] = "unknown"
            _add_issue(
                issues,
                "active driver report descriptor is neither the primary nor dual contract",
            )
        if companion_descriptor != EXPECTED_COMPANION_DESCRIPTOR:
            _add_issue(issues, "companion report descriptor differs from scaffold")
        if dual_descriptor != EXPECTED_DUAL_DESCRIPTOR:
            _add_issue(issues, "dual report descriptor differs from scaffold")
        if dual_descriptor != protocol_descriptor + companion_descriptor:
            _add_issue(issues, "dual descriptor is not primary + companion")
        if len(companion_descriptor) != 29:
            _add_issue(issues, "companion report descriptor must be 29 bytes")
        if len(dual_descriptor) != len(protocol_descriptor) + len(companion_descriptor):
            _add_issue(issues, "dual descriptor length does not equal both TLC lengths")
    except (ValueError, TypeError) as exc:
        _add_issue(issues, f"report descriptor: {exc}")

    # Compile-time shape checks that do not require a WDK.  The structs are
    # under #pragma pack(push, 1), so these sums are deterministic.
    if c_values:
        expected_sizes = {
            "MIRABOX_CODEX_REPORT": c_values.get("MIRABOX_CODEX_MICRO_REPORT_LENGTH"),
            "MIRABOX_CODEX_INFO": 12,
        }
        report["structSizes"] = expected_sizes
        if c_values.get("MIRABOX_CODEX_MICRO_BODY_LENGTH") != c_values.get("MIRABOX_CODEX_MICRO_REPORT_LENGTH", 0) - 1:
            _add_issue(issues, "body length is not report length minus report ID")
        if c_values.get("MIRABOX_CODEX_MICRO_PAYLOAD_MAX") != c_values.get("MIRABOX_CODEX_MICRO_BODY_LENGTH", 0) - 2:
            _add_issue(issues, "payload max is not body length minus message type/length bytes")
        if not re.search(r"typedef\s+struct\s+_MIRABOX_CODEX_REPORT\s*\{[^}]*Bytes\s*\[\s*MIRABOX_CODEX_MICRO_REPORT_LENGTH\s*\]", protocol, re.S):
            _add_issue(issues, "MIRABOX_CODEX_REPORT does not carry the full report length")
        if not re.search(r"typedef\s+struct\s+_MIRABOX_CODEX_INFO\s*\{[^}]*Reserved\s*\[\s*2\s*\]", protocol, re.S):
            _add_issue(issues, "MIRABOX_CODEX_INFO does not have the packed two-byte reserved tail")

    # Compare the actual Python module values with the C contract.  Loading
    # this module only defines pure helpers and ctypes declarations; it does
    # not enumerate devices or open a handle.
    try:
        py = _load_python_module(python_path, "_codexmicro_sideband_contract")
        py_values = {name: int(getattr(py, name)) for name in EXPECTED}
        report["constants"]["python"] = py_values
        for name, expected in EXPECTED.items():
            actual_c = c_values.get(name)
            actual_py = py_values.get(name)
            if actual_c != expected:
                _add_issue(issues, f"C {name}=0x{(actual_c or 0):x}; expected 0x{expected:x}")
            if actual_py != expected:
                _add_issue(issues, f"Python {name}=0x{(actual_py or 0):x}; expected 0x{expected:x}")
            if actual_c is not None and actual_py != actual_c:
                _add_issue(issues, f"C/Python drift for {name}: 0x{actual_c:x} vs 0x{actual_py:x}")
        py_guid = str(getattr(py, "BRIDGE_INTERFACE_GUID"))
        report["guid"]["python"] = py_guid
        if py_guid.upper() != EXPECTED_GUID:
            _add_issue(issues, f"Python bridge GUID is {py_guid}; expected {EXPECTED_GUID}")
        for name, fields in ioctl_values.items():
            py_value = int(getattr(py, name))
            report["ioctl"].setdefault("python", {})[name] = py_value
            if py_value != fields["value"]:
                _add_issue(issues, f"C/Python IOCTL drift for {name}: 0x{fields['value']:08x} vs 0x{py_value:08x}")
    except Exception as exc:  # import errors should be visible in the report
        _add_issue(issues, f"Python sideband constants: {type(exc).__name__}: {exc}")

    # The companion helper is intentionally a separate, hidapi-lazy module.
    # Importing it here proves that offline checks do not require hidapi or a
    # Windows device, while comparing its constants catches C/Python drift.
    try:
        companion_py = _load_python_module(
            companion_python_path, "_codexmicro_companion_contract"
        )
        companion_py_values = {
            name: int(getattr(companion_py, name))
            for name in EXPECTED_COMPANION
        }
        report["constants"]["companionPython"] = companion_py_values
        for name, expected in EXPECTED_COMPANION.items():
            actual_c = c_companion_values.get(name)
            actual_py = companion_py_values.get(name)
            if actual_c != expected:
                _add_issue(
                    issues,
                    f"C {name}=0x{(actual_c or 0):x}; expected 0x{expected:x}",
                )
            if actual_py != expected:
                _add_issue(
                    issues,
                    f"Python {name}=0x{(actual_py or 0):x}; expected 0x{expected:x}",
                )
            if actual_c is not None and actual_py != actual_c:
                _add_issue(
                    issues,
                    f"C/Python drift for {name}: 0x{actual_c:x} vs 0x{actual_py:x}",
                )
        companion_py_descriptor = list(
            getattr(companion_py, "MIRABOX_CODEX_COMPANION_REPORT_DESCRIPTOR")
        )
        companion_py_dual = list(
            getattr(companion_py, "MIRABOX_CODEX_MICRO_DUAL_REPORT_DESCRIPTOR")
        )
        report.setdefault("companionDescriptor", {})["python"] = companion_py_descriptor
        report.setdefault("dualDescriptor", {})["python"] = companion_py_dual
        if companion_py_descriptor != EXPECTED_COMPANION_DESCRIPTOR:
            _add_issue(issues, "Python companion report descriptor differs from scaffold")
        if companion_py_dual != EXPECTED_DUAL_DESCRIPTOR:
            _add_issue(issues, "Python dual report descriptor differs from scaffold")
        if companion_py_dual != EXPECTED_DESCRIPTOR + companion_py_descriptor:
            _add_issue(issues, "Python dual descriptor is not primary + companion")
    except Exception as exc:
        _add_issue(issues, f"Python companion constants: {type(exc).__name__}: {exc}")

    # Verify that the driver actually dispatches every sideband operation and
    # that the bridge source is part of the UMDF project.
    for name in IOCTL_NAMES:
        if not re.search(rf"case\s+{re.escape(name)}\s*:", driver):
            _add_issue(issues, f"driver dispatch is missing {name}")
    for symbol in ("BridgePushInput", "BridgeReadOutput", "BridgeGetInfo", "BridgeReset"):
        if not re.search(rf"\b{re.escape(symbol)}\s*\(", bridge):
            _add_issue(issues, f"bridge.c is missing {symbol}")
    for symbol in ("BridgePublishOutput", "BridgeCopyHidPacket"):
        if not re.search(rf"\b{re.escape(symbol)}\s*\(", bridge):
            _add_issue(issues, f"bridge.c is missing {symbol}")
    if "WdfDeviceCreateDeviceInterface" not in driver:
        _add_issue(issues, "driver does not publish the sideband device interface")
    if "WdfRequestForwardToIoQueue(Request, DeviceContext->BridgeOutputQueue)" not in bridge:
        _add_issue(issues, "READ_OUTPUT is not forwarded to BridgeOutputQueue")
    if "WdfIoQueueRetrieveNextRequest" not in bridge or "STATUS_CANCELLED" not in bridge:
        _add_issue(issues, "RESET does not visibly drain/cancel pending sideband reads")

    # The input path has two producers/consumers (the native sideband writer
    # and hidclass's pending read queue).  A check-then-forward sequence split
    # across separate lock regions has a lost-wakeup race: PUSH_INPUT can land
    # in the ring just before READ_REPORT parks its request.  Keep this
    # invariant reviewable even when the WDK is unavailable.
    push_body = _extract_function_body(bridge, "BridgePushInputReport")
    push_name = "BridgePushInputReport"
    if push_body is None:
        push_body = _extract_function_body(bridge, "BridgePushInput")
        push_name = "BridgePushInput"
    read_input_body = _extract_function_body(bridge, "BridgeReadInput")
    read_report_body = _extract_function_body(driver, "ReadReport")
    if push_body is None:
        _add_issue(issues, f"{push_name} body could not be inspected")
    else:
        if "WdfWaitLockAcquire" not in push_body:
            _add_issue(issues, f"{push_name} does not acquire BridgeLock")
        if "WdfIoQueueRetrieveNextRequest" not in push_body:
            _add_issue(issues, f"{push_name} does not service ManualQueue")
        if "BridgeStoreInputLocked" not in push_body:
            _add_issue(issues, f"{push_name} does not store input under BridgeLock")
        if "MIRABOX_CODEX_MICRO_REPORT_ID" not in push_body:
            _add_issue(issues, f"{push_name} does not validate the primary report ID")
        if active_mode == "dual-tlc" and "COMPANION_COLLECTION_REPORT_ID" not in push_body:
            _add_issue(issues, f"{push_name} does not accept the companion report ID")
    if read_input_body is None:
        _add_issue(issues, "BridgeReadInput body is missing")
    else:
        if "WdfWaitLockAcquire" not in read_input_body:
            _add_issue(issues, "BridgeReadInput does not acquire BridgeLock")
        if "BridgeTryPopInputLocked" not in read_input_body:
            _add_issue(issues, "BridgeReadInput does not pop input under BridgeLock")
        if "WdfRequestForwardToIoQueue" not in read_input_body:
            _add_issue(issues, "BridgeReadInput does not forward empty reads")
    if read_report_body is None:
        _add_issue(issues, "ReadReport body could not be inspected")
    else:
        if "BridgeReadInput" not in read_report_body:
            _add_issue(issues, "ReadReport does not use the atomic BridgeReadInput path")
        if "BridgeTryPopInput(" in read_report_body:
            _add_issue(issues, "ReadReport still has a non-atomic ring check")

    if active_mode == "dual-tlc":
        # IOCTL_HID_READ_REPORT has no requested report-ID input.  The
        # minidriver returns reports from one shared stream and HIDClass routes
        # each complete report to a top-level collection by its ID.
        if read_report_body is not None:
            if "RequestGetHidXferPacket_ToReadFromDevice" in read_report_body:
                _add_issue(
                    issues,
                    "dual ReadReport incorrectly tries to read a requested report ID",
                )
            if "BridgeReadCompanionOutput" in read_report_body:
                _add_issue(
                    issues,
                    "dual ReadReport uses a per-companion queue instead of shared HID input",
                )
        for forbidden in ("CompanionReadQueue", "CompanionOutputRing"):
            if forbidden in header or forbidden in bridge or forbidden in driver:
                _add_issue(
                    issues,
                    f"dual HID input path still carries obsolete per-TLC state {forbidden}",
                )

        write_body = _extract_function_body(driver, "WriteReport")
        set_output_body = _extract_function_body(driver, "SetOutputReport")
        publish_body = _extract_function_body(bridge, "BridgePublishOutput")
        for name, body in (
            ("WriteReport", write_body),
            ("SetOutputReport", set_output_body),
        ):
            if body is None:
                _add_issue(issues, f"{name} body could not be inspected for dual routing")
                continue
            if "COMPANION_COLLECTION_REPORT_ID" not in body:
                _add_issue(issues, f"{name} does not dispatch companion report ID 7")
            if "BridgePushInputReport" not in body:
                _add_issue(issues, f"{name} does not translate companion output into HID input")
        if publish_body is None:
            _add_issue(issues, "BridgePublishOutput body could not be inspected for dual routing")
        else:
            if "COMPANION_COLLECTION_REPORT_ID" not in publish_body:
                _add_issue(issues, "BridgePublishOutput does not translate ID 6 to ID 7")
            if "BridgePushInputReport" not in publish_body:
                _add_issue(
                    issues,
                    "BridgePublishOutput does not publish companion reports to shared HID input",
                )

    copy_body = _extract_function_body(bridge, "BridgeCopyHidPacketForId")
    copy_name = "BridgeCopyHidPacketForId"
    if copy_body is None:
        copy_body = _extract_function_body(bridge, "BridgeCopyHidPacket")
        copy_name = "BridgeCopyHidPacket"
    if copy_body is None:
        _add_issue(issues, f"{copy_name} body could not be inspected")
    else:
        if not re.search(
            r"Packet->reportBufferLen\s*!=\s*MIRABOX_CODEX_MICRO_BODY_LENGTH",
            copy_body,
        ):
            _add_issue(issues, f"{copy_name} does not constrain bare reports to 63 bytes")
        if not re.search(
            r"Packet->reportBufferLen\s*!=\s*MIRABOX_CODEX_MICRO_REPORT_LENGTH",
            copy_body,
        ):
            _add_issue(issues, f"{copy_name} does not constrain full reports to 64 bytes")

    get_input_body = _extract_function_body(driver, "GetInputReport")
    if get_input_body is None:
        _add_issue(issues, "GetInputReport body could not be inspected")
    elif not re.search(
        r"report\s*\[\s*1\s*\]\s*=\s*MIRABOX_CODEX_MICRO_MESSAGE_TYPE",
        get_input_body,
    ):
        _add_issue(issues, "empty GET_INPUT_REPORT does not identify Micro RPC channel 2")

    for required in (r"..\codexmicro.c", r"..\bridge.c", "util.c"):
        if required not in project:
            _add_issue(issues, f"UMDF project does not include {required}")
    for required in (r"root\MiraboxCodexMicro", "MsHidUmdf.inf", "UmdfService=", "ServiceBinary="):
        if required.lower() not in inf.lower():
            _add_issue(issues, f"INF is missing {required}")
    if "Class=HIDClass" not in inf:
        _add_issue(issues, "INF is not marked HIDClass")
    if "L\"Codex Micro\"" not in common or "L\"Work Louder\"" not in common:
        _add_issue(issues, "HID manufacturer/product strings do not identify Codex Micro")

    report["ok"] = not issues
    return report


def _format_human(report: dict[str, Any]) -> str:
    lines = ["sideband contract: PASS" if report.get("ok") else "sideband contract: FAIL"]
    descriptor = report.get("descriptor") or {}
    if descriptor:
        lines.append(f"descriptor bytes: {descriptor.get('length', '?')}")
    ioctl = (report.get("ioctl") or {}).get("c", {})
    for name in IOCTL_NAMES:
        if name in ioctl:
            lines.append(f"{name}: 0x{ioctl[name]['value']:08x}")
    if report.get("guid"):
        lines.append(f"bridge GUID: {report['guid'].get('c', '?')}")
    for issue in report.get("issues", []):
        lines.append(f"- {issue}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root (default: parent of scripts/)",
    )
    parser.add_argument("--json", action="store_true", help="emit a JSON report")
    args = parser.parse_args(argv)
    report = check_contract(args.root)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(_format_human(report))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
