"""Free-text scrubbing and record-state derivation for session records.

Moved verbatim from sessions.py (SESSION-SPAWN-MCP split 3a, 400-line
file gate); plangraph.spawner.sessions re-exports every name defined here.
"""

import os


_SCRUB_KEYS = frozenset({"reason", "error", "warning", "card", "message", "argv"})


def _scrub_free(value):
    """Redact at every depth: this subtree is free text."""
    from plangraph.spawner.env_redact import redact_env_values
    if isinstance(value, str):
        return redact_env_values(value, os.environ)
    if isinstance(value, dict):
        return {k: _scrub_free(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_free(v) for v in value]
    return value


def _scrub_keep(value):
    """Structured context: scalars pass through untouched, containers are
    recursed so nested free-text fields are still found."""
    if isinstance(value, dict):
        return {k: (_scrub_free(v) if k in _SCRUB_KEYS else _scrub_keep(v))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_keep(v) for v in value]
    return value


def _scrub(value):
    """Deep scrub through the env redactor - free-text fields only (R-23).
    reason, error, warning, card and message texts and argv lists are
    redacted; structured fields - name, cwd, session_id and every path in a
    record - pass through untouched, since redacting those would corrupt ids
    and paths callers must act on. A bare string is free text by definition."""
    from plangraph.spawner.env_redact import redact_env_values
    if isinstance(value, str):
        return redact_env_values(value, os.environ)
    if isinstance(value, dict):
        return _scrub_keep(value)
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value

def _state(rec):
    """Record state for launch gating and list (D-3193). stop() writes only
    the stopped_ts timestamp, so a set stopped_ts means "stopped" whatever the
    state field says; records predating the write-ahead launch record carry no
    state field and derive it the same way."""
    if rec.get("stopped_ts") is not None:
        return "stopped"
    state = rec.get("state")
    return state if state in ("launching", "running", "unknown", "stopped") else "running"
