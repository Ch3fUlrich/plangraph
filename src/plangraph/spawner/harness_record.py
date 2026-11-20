"""The harness a run actually ran in, written onto the run record at launch.

Record fields: ``harness`` (from the launched leg, not the requested one), ``harness_version``
(``<client> --version``, ``unknown`` when unreadable, never guessed), ``harness_fallback``
(why the model's own harness was not used) and ``host``.  ``report`` lists runs, failures and
no-verdict reviews per harness.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from functools import lru_cache

UNKNOWN = "unknown"
_VERSION_TIMEOUT_S = 10


@lru_cache(maxsize=None)
def client_version(name: str) -> str:
    """First line of ``<name> --version`` or ``unknown``."""
    path = shutil.which(name)
    if not path:
        return UNKNOWN
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=_VERSION_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN
    lines = (out.stdout or out.stderr).strip().splitlines() if out.returncode == 0 else []
    return lines[0].strip()[:200] if lines and lines[0].strip() else UNKNOWN


def host_name() -> str:
    return os.environ.get("PLANGRAPH_HOST_NAME") or socket.gethostname()


def fields(harness: str, fallback: str | None, host: str | None = None) -> dict:
    return {"harness": harness or UNKNOWN, "harness_version": client_version(harness) if harness else UNKNOWN,
            "harness_fallback": fallback, "host": host or host_name()}


def stamp(record: dict, leg, host: str | None = None) -> None:
    """Write the launched leg's harness fields onto ``record``."""
    record.update(fields(getattr(leg, "harness", None), getattr(leg, "harness_fallback", None), host))


def native_fields(host: str | None = None) -> dict:
    """Fields of a native Codex writer run."""
    return fields("codex", None, host)


def report(runs, *, now: float | None = None, hours: float = 24) -> dict:
    """Runs, failed runs and no-verdict reviews of the last ``hours`` hours, per harness."""
    now = time.time() if now is None else now
    cutoff = now - hours * 3600
    counts: dict[str, int] = {}
    failed: dict[str, list] = {}
    no_verdict: dict[str, list] = {}
    for run in runs:
        when = run.get("ended") or run.get("started") or 0
        if when < cutoff:
            continue
        harness = run.get("harness") or UNKNOWN
        counts[harness] = counts.get(harness, 0) + 1
        if run.get("status") in ("failed", "refused", "timeout"):
            failed.setdefault(harness, []).append(run["run_id"])
        if run.get("role") == "review" and run.get("reason") == "no_verdict":
            no_verdict.setdefault(harness, []).append(run["run_id"])
    return {"hours": hours, "runs": counts, "failed": failed, "no_verdict": no_verdict}


#: Consecutive no-verdict review runs after which a leg leaves the review route.
NO_VERDICT_LIMIT = 3


def _review_leg_id(run) -> str | None:
    attempts = [a for a in run.get("attempts") or () if isinstance(a, dict) and a.get("status") != "skipped"]
    leg = (attempts[-1].get("leg") if attempts else None) or run.get("chosen_leg") or {}
    return leg.get("id") if isinstance(leg, dict) else None


def no_verdict_legs(runs, limit: int = NO_VERDICT_LIMIT) -> set[str]:
    """Review legs whose last ``limit`` finished runs all ended without a VERDICT line."""
    recent: dict[str, list[bool]] = {}
    finished = [r for r in runs if isinstance(r, dict) and r.get("role") == "review"
                and isinstance(r.get("ended"), (int, float))]
    for run in sorted(finished, key=lambda r: r["ended"], reverse=True):
        leg_id = _review_leg_id(run)
        if leg_id and len(recent.setdefault(leg_id, [])) < limit:
            recent[leg_id].append(run.get("reason") == "no_verdict")
    return {leg_id for leg_id, outcomes in recent.items() if len(outcomes) == limit and all(outcomes)}

