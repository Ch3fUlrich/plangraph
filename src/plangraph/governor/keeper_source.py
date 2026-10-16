"""Fail-open policy.Source and override logger backed by the keeper."""
from __future__ import annotations

from datetime import datetime, timezone

__all__ = ["KeeperSource", "KeeperLog", "BURN_METRIC", "_burn_name",
           "_quota_name", "_rate_name"]

BURN_METRIC = "claude_burn_index"


def _burn_name() -> str:
    """Metric name for the daily burn index."""
    return BURN_METRIC


def _quota_name(provider: str) -> str:
    """Metric name for a provider's used tokens."""
    return f"provider_used.{provider}"


def _rate_name(provider: str) -> str:
    """Metric name for a provider's rate-limit flag."""
    return f"rate_limited.{provider}"


class KeeperSource:
    """Read-only metrics source; every read fails open to ``None``."""

    def __init__(self, session, today=None):
        self.session = session
        self._today = today or (
            lambda: datetime.now(timezone.utc).date().isoformat())
        self._cache: dict[tuple[str, str], list] = {}

    def _rows(self, metric: str, day: str) -> list | None:
        """Return cached ``rows`` for (metric, day) or ``None`` on any error."""
        key = (metric, day)
        if key in self._cache:
            return self._cache[key]
        try:
            data = self.session.call("GET", "metrics",
                                     params={"day": day, "metric": metric})
            rows = data.get("rows")
            if not isinstance(rows, list):
                rows = None
        except Exception:
            rows = None
        self._cache[key] = rows
        return rows

    def _first_value(self, metric: str, day: str) -> float | None:
        """Numeric value of the first row, else ``None``."""
        rows = self._rows(metric, day)
        if not rows:
            return None
        try:
            return float(rows[0]["value"])
        except (KeyError, TypeError, ValueError):
            return None

    def burn(self, day: str) -> float | None:
        """Return the burn index for ``day`` or ``None``."""
        return self._first_value(_burn_name(), day)

    def quota_used(self, provider: str, day: str) -> float | None:
        """Return tokens used by ``provider`` for ``day`` or ``None``."""
        return self._first_value(_quota_name(provider), day)

    def rate_limited(self, provider: str) -> bool | None:
        """True if rate-limited today, False if a zero row exists, None else."""
        value = self._first_value(_rate_name(provider), self._today())
        if value is None:
            return None
        return value >= 1.0


class KeeperLog:
    """Log an override/fallback to the keeper; never raise."""

    def __init__(self, session):
        self.session = session

    def __call__(self, decision) -> None:
        """POST a governor_override for ``decision``; swallow all errors."""
        provider = decision.provider if isinstance(decision.provider, str) \
            else None
        body = {
            "start_kind": decision.start_kind,
            "initiator": decision.initiator,
            "reason": decision.reason[:500],
            "provider": provider,
        }
        try:
            self.session.call("POST", "governor_override", body=body)
        except Exception:
            pass
