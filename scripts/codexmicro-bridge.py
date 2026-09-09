#!/usr/bin/env python3
"""CLI entry point for the Codex Micro sideband companion."""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from codexmicro_sideband import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())

