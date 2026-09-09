#!/usr/bin/env python3
"""CLI wrapper for the explicit WebUI ↔ physical N4 test bridge."""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from n4_webui_bridge import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())

