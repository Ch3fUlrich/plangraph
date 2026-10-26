"""Provider-response evidence, scoped to a serving leg/account/combo.

Writers are transport adapters, never prompt parsers. No CLI or MCP writer is
provided. Probe passes must be called only after an actual provider success.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
import re
import sqlite3
import time
from datetime import datetime
from email.utils import parsedate_to_datetime

REPROBE_MINUTES = 10

# E3b (D-2913) "verified before counted": a leg counts as a review seat only
# after a passing verify within VERIFY_MAX_AGE. The legs that already served
# as seats are grandfathered in code; a later failed or expired verify
# revokes even that state.
VERIFY_MAX_AGE = 7 * 24 * 60 * 60
GRANDFATHERED_VERIFIED = frozenset({
    "qodercli-qwen3.8-flash", "agy-sonnet-5.5", "opencode-glm-5.3-flash-free"})


class LegStateError(ValueError):
    """Not a provider limit response."""


def _redact(body):
    body = re.sub(r"(?i)\bBearer\s+[^\s\"',;]+", "[REDACTED]", body)
    body = re.sub(r"(?i)\bsk-[\w-]+", "[REDACTED]", body)
    body = re.sub(r"(?i)(?:api[_-]?key|access[_-]?token|authorization)\s*[\"']?\s*[:=]\s*[\"']?[^\s\"',;}]+", "[REDACTED]", body)
    body = re.sub(r"\b[A-Za-z0-9_+/=-]{32,}\b", "[REDACTED]", body)
    return body[:300]


def _reset(headers, body, now):
    for key in ("retry-after", "x-ratelimit-reset"):
        value = headers.get(key)
        if value is None:
            continue
        try:
            number = float(value)
            if math.isfinite(number) and number >= 0:
                return now + number if key == "retry-after" else number
        except (ValueError, TypeError):
            try:
                return parsedate_to_datetime(value).timestamp()
            except (ValueError, TypeError, OverflowError):
                try:
                    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
                except (ValueError, TypeError, AttributeError):
                    pass
    match = re.search(r"resets?\s+in\s+(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)\b", body, re.I)
    if match:
        return now + float(match[1]) * {"s": 1, "m": 60, "h": 3600}[match[2][0].lower()]
    return None


class LegStateStore:
    def __init__(self, path):
        self.path = Path(path).absolute()

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        from .private_io import create_private
        self.permissions_secure = create_private(self.path)
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        from .private_io import secure_sqlite_files
        self.permissions_secure = secure_sqlite_files(self.path) and self.permissions_secure
        db.execute("""CREATE TABLE IF NOT EXISTS leg_events (
            id INTEGER PRIMARY KEY, leg_key TEXT, account TEXT, combo TEXT,
            status INTEGER, body_excerpt TEXT, retry_after_s REAL, reset_at REAL,
            ts REAL, caller TEXT, source TEXT)""")
        db.execute("""CREATE TABLE IF NOT EXISTS leg_verifies (
            id INTEGER PRIMARY KEY, leg_key TEXT, ok INTEGER, run_id TEXT, ts REAL)""")
        return db

    def record_provider_response(self, leg, status, body, headers, caller, now=None, account=None, combo=None):
        now = time.time() if now is None else now
        headers = {str(k).lower(): v for k, v in (headers or {}).items()}
        if type(status) is not int or status not in (429, 402) or not caller or not str(caller).strip():
            raise LegStateError("requires provider 429/402 and caller")
        if not isinstance(body, str) or (not body.strip() and not any(headers.get(k) for k in ("retry-after", "x-ratelimit-reset"))):
            raise LegStateError("requires provider body or reset header")
        if not isinstance(leg, str) or not leg.strip():
            raise LegStateError("requires provider/model leg")
        reset = _reset(headers, body, now)
        with self._connect() as db:
            db.execute("INSERT INTO leg_events VALUES (NULL,?,?,?,?,?,?,?,?,?,?)", (leg, account, combo, status, _redact(body), None if reset is None else reset - now, reset, now, caller, "provider-response"))

    def record_probe_pass(self, leg, caller, now=None, account=None, combo=None):
        if not caller:
            raise LegStateError("requires successful provider response caller")
        with self._connect() as db:
            db.execute("INSERT INTO leg_events VALUES (NULL,?,?,?,?,?,?,?,?,?,?)", (leg, account, combo, 200, "", None, None, time.time() if now is None else now, caller, "provider-probe"))

    def record_verify(self, leg, ok, run_id, now=None):
        """Record one `probe --verify <leg>` outcome (E3b item 5)."""
        if not isinstance(leg, str) or not leg.strip():
            raise LegStateError("requires leg")
        if type(ok) is not bool:
            raise LegStateError("requires ok boolean")
        if not isinstance(run_id, str) or not run_id.strip():
            raise LegStateError("requires run_id")
        with self._connect() as db:
            db.execute("INSERT INTO leg_verifies VALUES (NULL,?,?,?,?)",
                       (leg, 1 if ok else 0, run_id,
                        time.time() if now is None else now))

    def verified(self, leg, now=None):
        """True only when the last verify passed within VERIFY_MAX_AGE.

        No verify record falls back to the code-owned grandfathered legs;
        a failed or expired verify revokes even that state.
        """
        now = time.time() if now is None else now
        if not self.path.exists() or self.path.stat().st_size == 0:
            return leg in GRANDFATHERED_VERIFIED
        try:
            with sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True) as db:
                db.row_factory = sqlite3.Row
                row = db.execute(
                    "SELECT ok, ts FROM leg_verifies WHERE leg_key=? AND ts<=? "
                    "ORDER BY ts DESC, id DESC LIMIT 1", (leg, now)).fetchone()
        except (sqlite3.Error, OSError):
            return leg in GRANDFATHERED_VERIFIED
        if row is None:
            return leg in GRANDFATHERED_VERIFIED
        return bool(row["ok"]) and now - row["ts"] < VERIFY_MAX_AGE

    def leg_status(self, leg, now=None, account=None, combo=None):
        now = time.time() if now is None else now
        if not self.path.exists():
            return {"state": "ok", "evidence": None}
        if self.path.stat().st_size == 0:
            return {"state": "ok", "evidence": None}
        # Read-only: querying state never creates a database or mutates expiry.
        try:
            with sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True) as db:
                db.row_factory = sqlite3.Row
                row = db.execute("SELECT * FROM leg_events WHERE leg_key=? AND account IS ? AND combo IS ? AND ts<=? ORDER BY ts DESC,id DESC LIMIT 1", (leg, account, combo, now)).fetchone()
        except (sqlite3.Error, OSError):
            return {"state": "ok", "evidence": None}
        if row is None or row["status"] == 200:
            return {"state": "ok", "evidence": None}
        evidence = {k: row[k] for k in ("status", "body_excerpt", "reset_at", "ts", "caller")}
        evidence["event_id"] = row["id"]
        reset = row["reset_at"]
        state = ("limited" if now < reset else "ok") if reset is not None else ("limited" if now < row["ts"] + REPROBE_MINUTES * 60 else "reprobe")
        return {"state": state, "evidence": evidence}


def _default_store():
    return LegStateStore(Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "plangraph" / "leg-state.sqlite")


def record_provider_response(leg, status, body, headers, caller, now=None, account=None, combo=None):
    return _default_store().record_provider_response(leg, status, body, headers, caller, now, account, combo)


def record_probe_pass(leg, caller, now=None, account=None, combo=None):
    return _default_store().record_probe_pass(leg, caller, now, account, combo)


def leg_status(leg, now=None, account=None, combo=None):
    return _default_store().leg_status(leg, now, account, combo)
