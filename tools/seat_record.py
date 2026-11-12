#!/usr/bin/env python3
"""Derive a review seat's evidence + review text from a spawner record.json.

A spawner seat run dir (``~/.local/state/plangraph/runs/<run>/``) holds only
``record.json`` and ``live.log``.  ``evidence_from_record`` turns that into the
``evidence.json``/``review.txt`` pair tools/seat_verdict.py consumes, failing
closed (ValueError) whenever the record cannot witness a completed review.
The record is bound to its run dir (run_id must equal the dir name) and must be
uid-owned, not group/world-writable.  ``--head`` binds the completion-time HEAD
of the run cwd (record completion.head_sha), not the launch commit: the spawner
records no launch-time sha, so a launch binding cannot be checked here.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

_HEAD_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
# F8: both spellings count (review/tail.md uses TEST-GAMING).
_TEST_GAMING = re.compile(r"^TEST[_-]GAMING:[ \t]*(.*)$", re.I | re.M)


def _verdict_line():
    """seat_verdict's authoritative VERDICT-line grammar (lazy: circular import)."""
    from seat_verdict import _VERDICT_LINE
    return _VERDICT_LINE


def _canon_family(name):
    """Canonical family of a model or family name via seat_verdict's alias table."""
    from seat_verdict import model_family, KNOWN_FAMILIES  # local: seat_verdict imports this module
    family = model_family(name)
    if family is not None:
        return family
    candidate = str(name or "").strip().lower()
    if candidate not in KNOWN_FAMILIES:
        raise ValueError(f"unknown model family {name!r}")
    return candidate


def evidence_from_record(run_dir: Path, cli_writer_families=None) -> tuple[dict, str]:
    """Return (evidence, review_text) for a record.json-only seat run dir.

    Writer coverage comes only from the CLI --writer-family list (enforced in
    seat_verdict.main); ``cli_writer_families`` is accepted for call compat but
    ignored here, because record.writer_family is the seat's own leg family and
    never names a reviewed writer.  Raises ValueError whenever the record
    cannot witness a completed review.
    """
    run_dir = Path(run_dir)
    record_path = run_dir / "record.json"
    try:
        record = json.loads(record_path.read_text("utf-8"))
    except OSError as exc:
        raise ValueError(f"record.json unreadable: {type(exc).__name__}") from exc
    except ValueError as exc:
        raise ValueError(f"record.json is not valid JSON: {exc}") from exc
    if not isinstance(record, dict):
        raise ValueError("record.json is not an object")
    try:
        stat = record_path.stat()
    except OSError as exc:
        raise ValueError(f"record.json unreadable: {type(exc).__name__}") from exc
    # F6: bind the record to its directory and to this uid's write access.
    if stat.st_uid != os.getuid() or stat.st_mode & 0o022:
        raise ValueError("record.json must be owned by the current uid and "
                         "not group/world-writable")
    run_id = record.get("run_id")
    if not isinstance(run_id, str) or run_id != run_dir.name:
        raise ValueError(f"record run_id {run_id!r} does not match "
                         f"run dir name {run_dir.name!r}")
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
    if live_log.strip():
        review_text = live_log
    else:
        # F4: stdout_tail accumulates every leg (service.py/runner.py append to
        # it), so it may stand in for live.log only on a single-attempt record.
        attempts = record.get("attempts")
        if not isinstance(attempts, list) or len(attempts) > 1:
            raise ValueError("stdout_tail accumulates every leg; live.log required")
        review_text = str(record.get("stdout_tail") or "")
    if not review_text.strip():
        raise ValueError("record has no review text (live.log and stdout_tail empty)")
    served = record.get("served_model")
    leg = record.get("chosen_leg")
    chosen = leg.get("model") if isinstance(leg, dict) else None
    leg_family = leg.get("family") if isinstance(leg, dict) else None
    if leg_family is not None and _canon_family(leg_family) != _canon_family(chosen):
        raise ValueError(f"chosen_leg.family {leg_family!r} does not match "
                         f"family {_canon_family(chosen)!r} of chosen_leg.model {chosen!r}")
    evidence = {"head_sha": head_sha, "source": "record.json", "run_id": run_id}
    if "item_key" in record:
        evidence["item_key"] = record["item_key"]
    # F3: finish_reason is only ever recorded; never hard-coded.  With no
    # record finish field the key is omitted, so a PASS is refused downstream.
    finish = record.get("finish_reason")
    if not (isinstance(finish, str) and finish) and isinstance(completion, dict):
        finish = completion.get("finish_reason")
    if isinstance(finish, str) and finish:
        evidence["finish_reason"] = finish
    if isinstance(served, str) and served and served != "unwitnessed":
        evidence["served_model"] = served
    else:
        if not isinstance(chosen, str) or not chosen:
            raise ValueError("record chosen_leg.model missing; seat model unwitnessed")
        # F2/D-2905: an unwitnessed served model may be replaced by the chosen
        # leg only on an opencode/*-free leg (the request-side witness rule);
        # any other leg never witnesses a family.
        if not (chosen.startswith("opencode/") and chosen.endswith("-free")):
            leg_id = leg.get("id") if isinstance(leg, dict) else None
            raise ValueError(f"served_model unwitnessed and chosen leg {leg_id!r} "
                             f"({chosen!r}) is not an opencode/*-free leg (D-2905); "
                             f"family not witnessed")
        evidence.update(served_model=chosen, witness_kind="record-chosen-leg",
                        request_side_model=chosen)
    verdict_line = _verdict_line()
    lines = review_text.splitlines()
    verdicts = {" ".join(m.group(1).split()).lower()
                for line in lines
                for m in [verdict_line.match(line)] if m}
    if len(verdicts) > 1:
        raise ValueError("conflicting VERDICT lines")
    last = max((i for i, line in enumerate(lines) if verdict_line.match(line)),
               default=-1)
    # F8 (D-3420): "VERDICT must be the last line" stays strict until the
    # routing rules say otherwise; only TEST[_-]GAMING lines may follow it.
    if last >= 0 and any(line.strip() and not _TEST_GAMING.match(line)
                         for line in lines[last + 1:]):
        raise ValueError("verdict is not the last line")
    gaming = None
    for match in _TEST_GAMING.finditer(review_text):
        gaming = match.group(1).strip()
    if gaming and "|" not in gaming:
        evidence["test_gaming"] = gaming.split()[0].lower()
    return evidence, review_text
