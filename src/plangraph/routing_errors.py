"""Shared routing refusals, independent of planner and provider imports."""

import json
import re


# Credit and auth require a diagnostic phrase. A bare 401, 402 or 403 may be prose.
CREDIT_AUTH_MARKER = re.compile(
    r'credits? (?:exhausted|depleted|exceeded|ran out)|out of credits|insufficient (?:credits?|balance|quota)'
    r'|payment required|unauthori[sz]ed|authentication (?:failed|error|required)|invalid api key', re.I)
_BUSY_LINE = re.compile(
    r'\s*(?:error:\s*(?:server busy|too busy|overloaded)|HTTP 503: Service Unavailable)\s*$', re.I)


def normalize_output(text):
    """Strip terminal escapes and standardize line endings for diagnostic parsing."""
    return re.sub(
        r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))', '', text
    ).replace('\r\n', '\n').replace('\r', '\n')


def busy_refusal(text):
    """Return a standalone busy diagnostic or one from a structured 503."""
    for line in normalize_output(text).splitlines():
        if _BUSY_LINE.match(line):
            return line.strip()
        if not line.strip().startswith('{'):
            continue
        try:
            response = json.loads(line).get('autoos_provider_response', {})
            if response.get('status') == 503:
                return 'HTTP 503: ' + ' '.join(normalize_output(str(response.get('body', ''))).split())[:200]
        except (ValueError, TypeError, AttributeError):
            pass
    return None


class RouteError(ValueError):
    """No usable route (estimate too big, or no eligible leg)."""

    def __init__(self, message, *, exclusion_reasons=None):
        super().__init__(message)
        self.exclusion_reasons = dict(exclusion_reasons or {})


class SpawnRefused(RouteError):
    """A spawn failed mandatory admission or audited-override policy."""


class ParameterRefused(SpawnRefused):
    """Malformed caller parameters, rather than a business-policy refusal."""
