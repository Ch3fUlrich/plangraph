#!/usr/bin/env python3
"""Shadow merge decision: read a local facts JSON; no merge or GO-row authority."""
import json
from pathlib import Path
import re
import sys

from merge_gate_core import Decision, REASON_CODES, decide


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
        result = decide(facts)
        if not isinstance(result, Decision):
            raise ValueError
        if result.outcome == "GO":
            if (result.code is not None or result.detail or not isinstance(result.sha, str)
                    or not re.fullmatch(r"[0-9a-f]{40}", result.sha)
                    or result.sha != facts["head_sha_now"]
                    or result.sha != facts["head_sha_at_check"]):
                raise ValueError
            line, status = f"GO {result.sha}", 0
        else:
            if (result.outcome != "REFUSE" or result.code not in REASON_CODES
                    or not isinstance(result.detail, str) or not result.detail
                    or not result.detail.isprintable() or result.sha):
                raise ValueError
            line, status = f"REFUSE {result.code} {result.detail}", 1
    except Exception:
        print("REFUSE ci_pending facts unavailable or invalid")
        return 1
    print(line)
    return status


if __name__ == "__main__":
    sys.exit(main())
