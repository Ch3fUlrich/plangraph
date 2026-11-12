#!/usr/bin/env python3
"""Shadow merge decision: read a local facts JSON; no merge or GO-row authority."""
import json
from pathlib import Path
import sys

from merge_gate_core import decide


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    try:
        if len(args) != 1:
            raise ValueError
        facts = json.loads(Path(args[0]).read_text(encoding="utf-8"),
                           object_pairs_hook=_unique_object)
    except (OSError, ValueError, UnicodeError, RecursionError):
        print("REFUSE ci_pending facts unavailable or invalid")
        return 1
    result = decide(facts)
    print(f"GO {result.sha}" if result.outcome == "GO" else
          f"REFUSE {result.code} {result.detail}")
    return int(result.outcome != "GO")


if __name__ == "__main__":
    sys.exit(main())
