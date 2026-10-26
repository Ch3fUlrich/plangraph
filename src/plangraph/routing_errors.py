"""Shared routing refusals, independent of planner and provider imports."""


class RouteError(ValueError):
    """No usable route (estimate too big, or no eligible leg)."""

    def __init__(self, message, *, exclusion_reasons=None):
        super().__init__(message)
        self.exclusion_reasons = dict(exclusion_reasons or {})


class SpawnRefused(RouteError):
    """A spawn failed mandatory admission or audited-override policy."""


class ParameterRefused(SpawnRefused):
    """Malformed caller parameters, rather than a business-policy refusal."""
