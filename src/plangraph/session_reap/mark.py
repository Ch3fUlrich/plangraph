"""mark.py - write ttl.json marker for a session (SESSION-AUTO-CLOSE, D-2442)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional, Sequence


def resolve_session_dir(explicit_dir: Optional[Path] = None) -> Path:
    """Resolve session directory from explicit_dir or CLAUDE_JOB_DIR parent.

    Refuses if CLAUDE_JOB_DIR is unset.
    """
    if explicit_dir is not None:
        return Path(explicit_dir)

    raw = os.environ.get("CLAUDE_JOB_DIR")
    if not raw or not raw.strip():
        raise ValueError("CLAUDE_JOB_DIR environment variable is unset")

    p = Path(raw)
    # If p already has state.json, p is the session directory.
    # Otherwise, per brief, dir is from env CLAUDE_JOB_DIR parent.
    if (p / "state.json").is_file() and not (p.parent / "state.json").is_file():
        return p
    return p.parent


def mark_session(
    ttl_min: int,
    allow_stop: bool = True,
    job_dir: Optional[Path] = None,
) -> Path:
    """Write ttl.json to the session directory."""
    target_dir = resolve_session_dir(job_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

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
        help="Session idle TTL in minutes.",
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
        help="Explicit session directory (defaults to CLAUDE_JOB_DIR parent).",
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
