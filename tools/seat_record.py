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
_TEST_GAMING = re.compile(r"^TEST_GAMING:[ \t]*(.*)$", re.I | re.M)
_VERDICT_LINE = re.compile(r"^[\s>#*_`-]*verdict[\s*_`]*:[\s*_`]*(.*)$", re.I)


def _canon_family(name):
    """Canonical family of a model or family name via seat_verdict's alias table."""
    from seat_verdict import model_family  # local: seat_verdict imports this module
    return model_family(name) or str(name or "").strip().lower()


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
    exit_code = record.get("exit_code")
    if type(exit_code) is not int or exit_code != 0:
        raise ValueError(f"record exit_code {exit_code!r} is not 0")
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
    leg = record.get("chosen_leg")
    chosen = leg.get("model") if isinstance(leg, dict) else None
    leg_family = leg.get("family") if isinstance(leg, dict) else None
    if leg_family is not None and _canon_family(leg_family) != _canon_family(chosen):
        raise ValueError(f"chosen_leg.family {leg_family!r} does not match "
                         f"family {_canon_family(chosen)!r} of chosen_leg.model {chosen!r}")
    evidence = {"finish_reason": "stop", "head_sha": head_sha,
                "run_id": record.get("run_id"), "source": "record.json"}
    if isinstance(served, str) and served and served != "unwitnessed":
        evidence["served_model"] = served
    else:
        if not isinstance(chosen, str) or not chosen:
            raise ValueError("record chosen_leg.model missing; seat model unwitnessed")
        evidence.update(served_model=chosen, witness_kind="record-chosen-leg",
                        request_side_model=chosen)
    writer_family = record.get("writer_family")
    if writer_family is not None and \
            _canon_family(writer_family) == _canon_family(evidence["served_model"]):
        raise ValueError(f"seat family equals record writer_family {writer_family!r}"
                         f"; seat covers no listed writer")
    verdicts = {" ".join(m.group(1).split()).lower()
                for line in review_text.splitlines()
                for m in [_VERDICT_LINE.match(line)] if m}
    if len(verdicts) > 1:
        raise ValueError("conflicting VERDICT lines")
    gaming = None
    for match in _TEST_GAMING.finditer(review_text):
        gaming = match.group(1).strip()
    if gaming and "|" not in gaming:
        evidence["test_gaming"] = gaming.split()[0]
    return evidence, review_text
