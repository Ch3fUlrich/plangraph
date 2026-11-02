"""mark.py - write ttl.json marker for a session (SESSION-AUTO-CLOSE, D-2442)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional, Sequence


def resolve_session_dir(explicit_dir: Optional[Path] = None) -> Path:
    """Resolve the session's own job directory.

    An explicit dir must itself hold state.json. Otherwise CLAUDE_JOB_DIR is walked
    upward to the nearest directory that holds state.json (CLAUDE_JOB_DIR may be the
    job dir or a child of it). Refuses if the variable is unset or no directory has a
    state.json, so a marker is never written outside a session's job dir.
    """
    if explicit_dir is not None:
        explicit = Path(explicit_dir)
        if not (explicit / "state.json").is_file():
            raise ValueError(f"no state.json in --dir ({explicit}); refusing to write ttl.json")
        return explicit

    raw = os.environ.get("CLAUDE_JOB_DIR")
    if not raw or not raw.strip():
        raise ValueError("CLAUDE_JOB_DIR environment variable is unset")

    start = Path(raw)
    for candidate in (start, *start.parents):
        if (candidate / "state.json").is_file():
            return candidate
    raise ValueError(f"no state.json in CLAUDE_JOB_DIR ({start}) or any parent; refusing to write ttl.json")


def mark_session(
    ttl_min: int,
    allow_stop: bool = True,
    job_dir: Optional[Path] = None,
) -> Path:
    """Write ttl.json to the session directory."""
    if ttl_min < 1:
        raise ValueError(f"--ttl-min must be >= 1, got {ttl_min}")
    target_dir = resolve_session_dir(job_dir)

    ttl_path = target_dir / "ttl.json"
    data = {
        "ttl_min": int(ttl_min),
        "allow_stop": bool(allow_stop),
    }
    with open(ttl_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")

    return ttl_path

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="plangraph.session_reap.mark",
        description="Write ttl.json for the current Claude Code session.",
    )
    parser.add_argument(
        "--ttl-min",
        type=int,
        required=True,
        help="Minutes a finished session is kept before it may be stopped (>= 1); replaces the default grace/idle for this session.",
    )
    parser.add_argument(
        "--no-stop",
        action="store_true",
        default=False,
        help="Mark session as allow_stop: false (never reap automatically).",
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=None,
        help="Explicit session directory; must contain state.json (defaults to the job dir found from CLAUDE_JOB_DIR).",
    )

    args = parser.parse_args(argv)

    try:
        ttl_path = mark_session(
            ttl_min=args.ttl_min,
            allow_stop=not args.no_stop,
            job_dir=args.dir,
        )
        print(f"Wrote {ttl_path}")
        return 0
    except Exception as exc:
        sys.stderr.write(f"mark error: {exc}\n")
        sys.exit(1)


if __name__ == "__main__":
    sys.exit(main())
