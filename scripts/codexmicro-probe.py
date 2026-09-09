#!/usr/bin/env python3
"""Read-only Codex Micro sideband preflight probe.

This command is intentionally separate from the live bridge.  It enumerates
the private UMDF interface and asks for ``GET_INFO`` only; it never resets a
queue, sends an input report, starts the N4 SDK, installs a driver, or changes
Windows signing policy.
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

from codexmicro_sideband import probe_sideband  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="codexmicro-probe.py",
        description="Read-only Codex Micro sideband interface probe (never installs a driver).",
    )
    parser.add_argument(
        "--sideband-path",
        metavar="PATH",
        help="explicit custom interface path; otherwise enumerate the bridge GUID",
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=5000,
        help="GET_INFO timeout in milliseconds (default: 5000)",
    )
    args = parser.parse_args(argv)
    try:
        value = probe_sideband(args.sideband_path, timeout_ms=args.timeout_ms)
    except (TypeError, ValueError) as error:
        value = {
            "ok": False,
            "status": "error",
            "message": str(error),
            "driverTouched": False,
        }
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    return 0 if value.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
