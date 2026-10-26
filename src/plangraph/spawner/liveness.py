"""Liveness values and exit class vocabulary mapping (D101, D121)."""
from plangraph.keeper.schema import EXIT_CLASSES

LIVENESS_TO_EXIT_CLASS = {
    "truncated": "worker_crash", "provider_protocol": "worker_crash", "stalled": "stall", "capped": "timeout", "attempt_timeout": "timeout",
    "oom": "worker_crash", "memguard": "outage", "killed": "outage", "host_full": "outage",
}
LIVENESS_VALUES = tuple(LIVENESS_TO_EXIT_CLASS)


def exit_class_for_liveness(liveness: str | None) -> str | None:
    """Map a liveness to its exit class; refuse unknown values (never a silent None)."""
    if liveness is None: return None
    if liveness not in LIVENESS_TO_EXIT_CLASS: raise ValueError(f"unknown liveness: {liveness!r}")
    return LIVENESS_TO_EXIT_CLASS[liveness]


def classify_attempt(status: str, liveness: str | None, exit_class: str | None = None) -> tuple[str | None, str]:
    """``(liveness, exit_class)`` for one attempt; the run record keeps the last attempt's.
    Never a class outside ``EXIT_CLASSES`` (I13): unknown liveness is refused. A failure keeps the
    monitor's class for the same attempt (nonzero exit -> ``worker_crash``, qwen-D8); the status
    fallback (``check_fail``) applies only where the monitor never classified the attempt.
    """
    if liveness: return liveness, exit_class_for_liveness(liveness)
    if status == 'cancelled': return None, 'cancelled'
    ok = status in ('ok', 'completed', 'dry_run')
    if exit_class is not None and (exit_class != 'ok' or ok):
        if exit_class not in EXIT_CLASSES: raise ValueError(f"unknown exit_class: {exit_class!r}")
        return None, exit_class
    return None, 'ok' if ok else 'check_fail'
