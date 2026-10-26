"""Live availability probe for review seats (E3b item 4, D-2913).

``available(legs, probe, now)`` splits candidate legs into reachable ids and
per-leg reasons.  The probe is injectable (default: ``opencode models``):
an opencode leg counts only when its
exact model id is listed in the probe output, and that output is cached 10
minutes under the spawner state dir so one listing serves a whole planning
window.  qodercli/agy legs have no listing; their binary must be on PATH.
Anything else is unavailable with a reason, never guessed.
"""
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from shutil import which

#: The probe listing is stale after 10 minutes (design "live probe" row).
CACHE_TTL = 600

_CACHE_NAME = "probe-models-cache.json"


def _state_dir() -> Path:
    # The spawner state dir is the probe cache home (design "live probe" row);
    # the env override is what keeps tests hermetic.
    return Path(os.environ.get("PLANGRAPH_SPAWN_STATE", "~/.cache/plangraph")).expanduser()


def _epoch(now) -> float:
    """``now`` is whatever plan_spawn forwards: datetime, epoch seconds, None."""
    if now is None:
        return time.time()
    if isinstance(now, datetime):
        return now.timestamp()
    return float(now)


def _read_cache(now):
    try:
        entry = json.loads((_state_dir() / _CACHE_NAME).read_text(encoding="utf-8"))
        if _epoch(now) - float(entry["ts"]) < CACHE_TTL:
            return entry["output"]
    except (OSError, ValueError, KeyError, TypeError):
        pass  # a missing or corrupt cache reads as stale
    return None


def _write_cache(now, output):
    try:
        path = _state_dir() / _CACHE_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"ts": _epoch(now), "output": output}), encoding="utf-8")
    except OSError:
        pass  # a read-only state dir must not break routing


def _models():
    """Bound the live listing command; a failed listing admits no models."""
    try:
        return subprocess.run(["opencode", "models"], capture_output=True,
                              text=True, check=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def available(legs, probe, now):
    """Return ``(ok_ids, {leg_id: reason})`` by live reachability.

    ``probe`` is a zero-argument callable returning the raw output of one
    ``opencode models`` call.  Its output is cached ``CACHE_TTL`` seconds under
    the spawner state dir, so a second call inside the window does not re-probe.
    """
    ok_ids, reasons = [], {}
    listing = None
    for leg in legs:
        if leg.harness == "opencode":
            if listing is None:
                listing = _read_cache(now)
                if listing is None:
                    listing = (probe or _models)()
                    _write_cache(now, listing)
                listing = set(str(listing).split())
            if leg.model in listing:
                ok_ids.append(leg.id)
            else:
                reasons[leg.id] = f"model not listed: {leg.model}"
        elif which(leg.harness):
            ok_ids.append(leg.id)
        else:
            reasons[leg.id] = f"{leg.harness} not on PATH"
    return ok_ids, reasons
