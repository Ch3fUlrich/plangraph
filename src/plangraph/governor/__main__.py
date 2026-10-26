"""Run the governor CLI as ``python3 -m plangraph.governor``."""
from __future__ import annotations

import sys

from plangraph.governor import cli

__all__ = []

if __name__ == "__main__":
    sys.exit(cli.main())
