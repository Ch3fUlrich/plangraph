#!/usr/bin/env python3
"""Source-checkout and installed-package wrapper, portable across hosts."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from plangraph.spawner.cli import main

if __name__ == '__main__':
    raise SystemExit(main())
