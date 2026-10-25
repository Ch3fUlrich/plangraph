"""Liveness values and exit class vocabulary mapping (D101, D121)."""
from plangraph.keeper.schema import EXIT_CLASSES

LIVENESS_TO_EXIT_CLASS = {
    "truncated": "worker_crash", "provider_protocol": "worker_crash", "stalled": "stall",
    "capped": "timeout", "attempt_timeout": "timeout", "oom": "worker_crash",
    "memguard": "outage", "killed": "outage", "host_full": "outage",
}
LIVENESS_VALUES = tuple(LIVENESS_TO_EXIT_CLASS)


def exit_class_for_liveness(liveness: str | None) -> str | None:
    return LIVENESS_TO_EXIT_CLASS.get(liveness) if liveness else None


def classify_attempt(status: str, liveness: str | None) -> tuple[str | None, str]:
    """``(liveness, exit_class)`` for one attempt; the run record keeps the last attempt's.

    Never returns a class outside ``EXIT_CLASSES`` (I13): unknown liveness is
    refused upstream (runner.cancel), plain failures land in ``check_fail``.
    """
    if liveness:
        return liveness, LIVENESS_TO_EXIT_CLASS[liveness]
    if status == 'cancelled':
        return None, 'cancelled'
    return None, ('ok' if status in ('completed', 'dry_run') else 'check_fail')
