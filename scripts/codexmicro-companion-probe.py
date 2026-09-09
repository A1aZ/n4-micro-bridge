#!/usr/bin/env python3
"""Read-only Codex Micro companion-HID enumeration probe.

This command intentionally performs *enumeration only*.  It does not create a
hidapi device object, open a path, read/write reports, start the N4 SDK, install
or reset a driver, or change test-signing/system state.  Use it to verify that
the dual-TLC descriptor exposes the expected ``0xFF70`` collection before
trying a live bridge.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from codexmicro_companion import (  # noqa: E402
    MIRABOX_CODEX_COMPANION_USAGE,
    MIRABOX_CODEX_COMPANION_USAGE_PAGE,
    MIRABOX_CODEX_MICRO_PID,
    MIRABOX_CODEX_MICRO_VID,
    probe_companion_hid,
)


def _parse_int(value: str) -> int:
    """Parse decimal or ``0x``/bare-hex command-line integers."""

    text = str(value).strip()
    try:
        return int(text, 0)
    except ValueError:
        try:
            return int(text, 16)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(
                f"expected decimal or hexadecimal integer, got {value!r}"
            ) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="codexmicro-companion-probe.py",
        description=(
            "Read-only Codex Micro companion HID enumeration "
            "(never opens a device or installs a driver)."
        ),
    )
    parser.add_argument(
        "--vendor-id",
        type=_parse_int,
        default=MIRABOX_CODEX_MICRO_VID,
        help=f"VID to enumerate (default: 0x{MIRABOX_CODEX_MICRO_VID:04x}; use 0 for wildcard)",
    )
    parser.add_argument(
        "--product-id",
        type=_parse_int,
        default=MIRABOX_CODEX_MICRO_PID,
        help=f"PID to enumerate (default: 0x{MIRABOX_CODEX_MICRO_PID:04x}; use 0 for wildcard)",
    )
    parser.add_argument(
        "--usage-page",
        type=_parse_int,
        default=MIRABOX_CODEX_COMPANION_USAGE_PAGE,
        help=(
            "companion usage page to match "
            f"(default: 0x{MIRABOX_CODEX_COMPANION_USAGE_PAGE:04x})"
        ),
    )
    parser.add_argument(
        "--usage",
        type=_parse_int,
        default=MIRABOX_CODEX_COMPANION_USAGE,
        help=f"companion usage to match (default: 0x{MIRABOX_CODEX_COMPANION_USAGE:04x})",
    )
    args = parser.parse_args(argv)

    try:
        value = probe_companion_hid(
            vendor_id=None if args.vendor_id == 0 else args.vendor_id,
            product_id=None if args.product_id == 0 else args.product_id,
            usage_page=args.usage_page,
            usage=args.usage,
        )
    except (TypeError, ValueError) as error:
        value = {
            "ok": False,
            "status": "invalid-arguments",
            "message": str(error),
            "platform": sys.platform,
            "driverTouched": False,
            "opened": False,
        }
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    return 0 if value.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
