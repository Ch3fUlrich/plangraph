"""Spawner ``record_run`` verb for :class:`plangraph.keeper.store.Store`.

The spawner records which models ran on an item (``run_recorded`` events);
``show``/``snapshot`` expose the cached columns plus derived fields.
"""
from __future__ import annotations

import json
import re

from .errors import Refused

__all__ = ["RunsMixin", "normalise_run"]

_RUN_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_PHASES = ("started", "finished")
_VERDICTS = ("pass", "fail", "invalid", None)
_TOP_KEYS = frozenset({"run_id", "phase", "rc", "writer", "seats"})
_WRITER_KEYS = frozenset({"model", "family", "source"})
_SEAT_KEYS = frozenset({"model", "verdict", "run_id"})


def _dump(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _check_run_id(value, where="run_id"):
    if not isinstance(value, str) or not _RUN_ID_RE.fullmatch(value):
        raise Refused("validation", f"{where} must match [A-Za-z0-9._:-]{{1,128}}")
    return value


def normalise_run(run) -> dict:
    """Validate *run* and return the normalised payload (refuses on violation)."""
    if not isinstance(run, dict):
        raise Refused("validation", "run must be an object")
    unknown = set(run) - _TOP_KEYS
    if unknown:
        raise Refused("validation", f"unknown run keys: {sorted(unknown)}")
    if "run_id" not in run:
        raise Refused("validation", "run_id is required")
    if "phase" not in run:
        raise Refused("validation", "phase is required")
    run_id = _check_run_id(run["run_id"])
    phase = run["phase"]
    if phase not in _PHASES:
        raise Refused("validation", "phase must be one of ['started', 'finished']")
    rc = run.get("rc")
    if rc is not None and (isinstance(rc, bool) or not isinstance(rc, int)):
        raise Refused("validation", "rc must be an int or null")
    writer = run.get("writer")
    if writer is not None:
        if not isinstance(writer, dict):
            raise Refused("validation", "writer must be an object or null")
        unknown = set(writer) - _WRITER_KEYS
        if unknown:
            raise Refused("validation", f"unknown writer keys: {sorted(unknown)}")
        model = writer.get("model")
        if not isinstance(model, str) or not 1 <= len(model) <= 128:
            raise Refused("validation", "writer.model must be a string<=128")
        family = writer.get("family")
        if family is not None and (not isinstance(family, str) or len(family) > 64):
            raise Refused("validation", "writer.family must be a string<=64 or null")
        source = writer.get("source")
        if source is not None and (not isinstance(source, str) or len(source) > 32):
            raise Refused("validation", "writer.source must be a string<=32 or null")
        writer = {"model": model, "family": family, "source": source}
    seats = run.get("seats")
    if seats is not None:
        if not isinstance(seats, list) or len(seats) > 8:
            raise Refused("validation", "seats must be a list<=8 or null")
        normed = []
        for seat in seats:
            if not isinstance(seat, dict):
                raise Refused("validation", "seats entries must be objects")
            unknown = set(seat) - _SEAT_KEYS
            if unknown:
                raise Refused("validation", f"unknown seat keys: {sorted(unknown)}")
            model = seat.get("model")
            if not isinstance(model, str) or not 1 <= len(model) <= 128:
                raise Refused("validation", "seat.model must be a string<=128")
            verdict = seat.get("verdict")
            if verdict not in _VERDICTS:
                raise Refused("validation",
                              "seat.verdict must be one of pass/fail/invalid/null")
            entry = {"model": model, "verdict": verdict}
            if seat.get("run_id") is not None:
                entry["run_id"] = _check_run_id(seat["run_id"], "seat.run_id")
            normed.append(entry)
        seats = normed
    return {"run_id": run_id, "phase": phase, "rc": rc,
            "writer": writer, "seats": seats}


class RunsMixin:
    """The spawner-only ``record_run`` verb."""

    def record_run(self, principal, key, run):
        return self._run(principal, "record_run", key,
                         lambda now: self._record_run(principal, key, run, now))

    def _has_run(self, key, run_id, phase) -> bool:
        for row in self.conn.execute(
                "SELECT payload FROM events WHERE key=? AND type='run_recorded'",
                (key,)):
            try:
                payload = json.loads(row["payload"] or "{}")
            except (ValueError, TypeError):
                continue
            if (isinstance(payload, dict) and payload.get("run_id") == run_id
                    and payload.get("phase") == phase):
                return True
        return False

    def _record_run(self, principal, key, run, now):
        self._require_role(principal, "spawner")
        item = self._get_or_refuse(key)
        self._assert_unlocked(item)
        if item["state"] not in ("claimed", "in_review"):
            raise Refused("bad_state", f"{key} is {item['state']}")
        normed = normalise_run(run)
        run_id, phase = normed["run_id"], normed["phase"]
        if self._has_run(key, run_id, phase):
            return {"key": key, "run_id": run_id, "phase": phase,
                    "duplicate": True}
        with self._write():
            fresh = self._get_or_refuse(key)
            self._assert_unlocked(fresh)
            if fresh["state"] not in ("claimed", "in_review"):
                raise Refused("conflict",
                              f"{fresh['key']} is {fresh['state']}")
            if self._has_run(key, run_id, phase):
                return {"key": key, "run_id": run_id, "phase": phase,
                        "duplicate": True}
            self._append(principal, key, "run_recorded", normed, now)
            try:
                have = json.loads(fresh.get("run_ids")) if fresh.get("run_ids") else []
            except (ValueError, TypeError):
                have = []
            if not isinstance(have, list):
                have = []
            seen = set(have)
            ordered = list(have)
            cands = [run_id]
            if isinstance(normed.get("seats"), list):
                for seat in normed["seats"]:
                    if seat.get("run_id") is not None:
                        cands.append(seat["run_id"])
            for cand in cands:
                if cand not in seen:
                    seen.add(cand)
                    ordered.append(cand)
            run_ids_text = _dump(ordered)
            writer_model = fresh.get("writer_model")
            if normed.get("writer") is not None:
                writer_model = normed["writer"]["model"]
            seat_models_text = fresh.get("seat_models")
            if normed.get("seats") is not None:
                seat_models_text = _dump(
                    [{"model": s["model"], "verdict": s["verdict"]}
                     for s in normed["seats"]])
            self.conn.execute(
                "UPDATE items SET run_ids=?, writer_model=?, seat_models=?,"
                " updated_at=? WHERE key=?",
                (run_ids_text, writer_model, seat_models_text,
                 self._iso(now), key))
        try:
            seat_models = json.loads(seat_models_text) if seat_models_text else None
        except (ValueError, TypeError):
            seat_models = None
        return {"key": key, "run_id": run_id, "phase": phase,
                "writer_model": writer_model, "seat_models": seat_models,
                "run_ids": ordered}
