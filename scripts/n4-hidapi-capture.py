#!/usr/bin/env python3
"""Read raw reports from a Mirabox N4 with Python hidapi only.

This is a diagnostic transport probe, not the production bridge.  It never
loads the StreamDock SDK, never sends an output report, and never changes the
device.  That makes it useful for separating a firmware/Windows HID delivery
problem from the SDK transport wrapper.

The N4's vendor collection is normally VID 0x6602/PID 0x1001, usage page
0xFFA0, usage 1 (MI_00).  hidapi returns a leading report-ID byte on some
backends; ``decode_n4_report`` accepts both the 512-byte body and the 513-byte
ID-prefixed form.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from n4_native_adapter import (  # noqa: E402
    N4_PRODUCT_ID,
    N4_USAGE,
    N4_USAGE_PAGE,
    N4_VENDOR_ID,
    decode_n4_report,
    normalize_n4_report,
)


def _parse_int(value: str) -> int:
    return int(str(value).strip(), 0)


def _path_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _int_or(value: Any, fallback: int) -> int:
    """Convert an optional hidapi integer without treating zero as missing."""

    if value is None:
        return int(fallback)
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(fallback)


def _candidate_score(info: Mapping[str, Any]) -> tuple[int, int, int]:
    """Prefer the observed N4 vendor collection over keyboard interfaces."""

    usage_page = _int_or(info.get("usage_page"), 0)
    usage = _int_or(info.get("usage"), 0)
    interface = _int_or(info.get("interface_number"), 999)
    return (
        0 if usage_page == N4_USAGE_PAGE else 1,
        0 if usage == N4_USAGE else 1,
        interface,
    )


def _enumerate(hid: Any, vendor_id: int, product_id: int) -> list[dict[str, Any]]:
    values = hid.enumerate(vendor_id, product_id)
    if values is None:
        return []
    return [dict(item) for item in values if isinstance(item, Mapping)]


def _select(
    candidates: Sequence[Mapping[str, Any]],
    *,
    path: Optional[str],
    interface: Optional[int],
) -> Mapping[str, Any]:
    filtered = list(candidates)
    if path is not None:
        wanted = path.encode("utf-8")
        filtered = [
            item for item in filtered
            if item.get("path") == path or item.get("path") == wanted
        ]
    if interface is not None:
        filtered = [
            item for item in filtered
            if _int_or(item.get("interface_number"), -1) == interface
        ]
    if not filtered:
        raise RuntimeError("no matching N4 HID collection")
    return sorted(filtered, key=_candidate_score)[0]


def _record(raw: Any, *, report_id: int, input_profile: str) -> dict[str, Any]:
    body = bytes(raw or [])
    normalized, effective_id = normalize_n4_report(body, report_id)
    decoded = decode_n4_report(normalized, require_ack=True, input_profile=input_profile)
    return {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "reportId": effective_id,
        "length": len(body),
        "bytes": body.hex(" "),
        "normalizedBytes": normalized.hex(" ") if normalized != body else None,
        "decoded": decoded,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read N4 input reports using Python hidapi without the StreamDock SDK."
    )
    parser.add_argument("--vid", default=f"0x{N4_VENDOR_ID:04x}")
    parser.add_argument("--pid", default=f"0x{N4_PRODUCT_ID:04x}")
    parser.add_argument("--path", help="exact hidapi path returned by --list")
    parser.add_argument("--interface", type=int, help="hidapi interface number (normally 0)")
    parser.add_argument("--list", action="store_true", help="list matching collections and exit")
    parser.add_argument("--timeout-ms", type=int, default=500,
                        help="bounded hidapi read timeout (default: 500 ms)")
    parser.add_argument("--size", type=int, default=1024,
                        help="read buffer size passed to hidapi (default: 1024)")
    parser.add_argument("--duration", type=float,
                        help="stop after this many seconds; otherwise Ctrl+C")
    parser.add_argument("--input-profile", choices=("cpp", "python"), default="cpp")
    parser.add_argument("--jsonl", action="store_true", help="emit one JSON object per report")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.timeout_ms < 1:
        raise ValueError("--timeout-ms must be positive")
    if args.size < 11:
        raise ValueError("--size must be at least 11")
    vendor_id = _parse_int(args.vid)
    product_id = _parse_int(args.pid)
    try:
        import hid  # type: ignore
    except Exception as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("Python hidapi is unavailable; install the 'hidapi' package") from exc

    candidates = _enumerate(hid, vendor_id, product_id)
    if args.list:
        print(json.dumps(candidates, ensure_ascii=False, indent=2, default=_path_text))
        return 0
    selected = _select(candidates, path=args.path, interface=args.interface)
    path_value = selected.get("path")
    if path_value in (None, "", b""):
        raise RuntimeError("selected hidapi collection has no path")

    device = hid.device()
    opened = False
    try:
        device.open_path(path_value)
        opened = True
        print(json.dumps({
            "mode": "hidapi-capture",
            "opened": True,
            "vendorId": vendor_id,
            "productId": product_id,
            "usagePage": selected.get("usage_page"),
            "usage": selected.get("usage"),
            "interface": selected.get("interface_number"),
            "path": _path_text(path_value),
            "timeoutMs": args.timeout_ms,
        }, ensure_ascii=False, default=_path_text), flush=True)
        deadline = None if args.duration is None else time.monotonic() + float(args.duration)
        reports = 0
        while deadline is None or time.monotonic() < deadline:
            raw = device.read(args.size, args.timeout_ms)
            if not raw:
                continue
            reports += 1
            value = _record(raw, report_id=0, input_profile=args.input_profile)
            value["sequence"] = reports
            if args.jsonl:
                print(json.dumps(value, ensure_ascii=False, separators=(",", ":")), flush=True)
            else:
                decoded = value.get("decoded") or {}
                print(
                    f"RAW #{reports} len={value['length']} "
                    f"code={decoded.get('hardwareCodeHex', '?')} "
                    f"state={decoded.get('stateHex', '?')} "
                    f"kind={decoded.get('kind', 'unmatched')} "
                    f"head={value['bytes'][:95]}",
                    flush=True,
                )
    except KeyboardInterrupt:
        return 0
    finally:
        if opened:
            try:
                device.close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as exc:
        print(f"n4-hidapi-capture: {exc}", file=sys.stderr)
        raise SystemExit(2)
