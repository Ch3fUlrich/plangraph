#!/usr/bin/env python3
"""Derive a review seat's evidence + review text from a spawner record.json.

A spawner seat run dir (``~/.local/state/plangraph/runs/<run>/``) holds only
``record.json`` and ``live.log``.  ``evidence_from_record`` turns that into the
``evidence.json``/``review.txt`` pair tools/seat_verdict.py consumes, failing
closed (ValueError) whenever the record cannot witness a completed review.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_HEAD_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_TEST_GAMING = re.compile(r"^TEST_GAMING:\s*(\S+)", re.I | re.M)


def evidence_from_record(run_dir: Path) -> tuple[dict, str]:
    """Return (evidence, review_text) for a record.json-only seat run dir.

    Raises ValueError whenever the record cannot witness a completed review.
    """
    run_dir = Path(run_dir)
    try:
        record = json.loads((run_dir / "record.json").read_text("utf-8"))
    except OSError as exc:
        raise ValueError(f"record.json unreadable: {type(exc).__name__}") from exc
    except ValueError as exc:
        raise ValueError(f"record.json is not valid JSON: {exc}") from exc
    if not isinstance(record, dict):
        raise ValueError("record.json is not an object")
    if record.get("role") != "review":
        raise ValueError(f"record role {record.get('role')!r} is not 'review'")
    if record.get("status") != "completed":
        raise ValueError(f"record status {record.get('status')!r} is not 'completed'")
    if record.get("exit_code") != 0:
        raise ValueError(f"record exit_code {record.get('exit_code')!r} is not 0")
    completion = record.get("completion")
    head_sha = completion.get("head_sha") if isinstance(completion, dict) else None
    if not isinstance(head_sha, str) or not _HEAD_SHA.match(head_sha):
        raise ValueError("record completion.head_sha missing or not 40 hex chars")
    try:
        live_log = (run_dir / "live.log").read_text("utf-8")
    except OSError:
        live_log = ""
    review_text = live_log if live_log.strip() else str(record.get("stdout_tail") or "")
    if not review_text.strip():
        raise ValueError("record has no review text (live.log and stdout_tail empty)")
    served = record.get("served_model")
    evidence = {"finish_reason": "stop", "head_sha": head_sha,
                "run_id": record.get("run_id"), "source": "record.json"}
    if isinstance(served, str) and served and served != "unwitnessed":
        evidence["served_model"] = served
    else:
        leg = record.get("chosen_leg")
        chosen = leg.get("model") if isinstance(leg, dict) else None
        if not isinstance(chosen, str) or not chosen:
            raise ValueError("record chosen_leg.model missing; seat model unwitnessed")
        evidence.update(served_model=chosen, witness_kind="record-chosen-leg",
                        request_side_model=chosen)
    match = _TEST_GAMING.search(review_text)
    if match:
        evidence["test_gaming"] = match.group(1)
    return evidence, review_text
