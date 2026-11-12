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

#: D-3685: cap booked resets at 24 h; readers expire evidence after six hours.
RESET_CAP_S = 86400
#: Failure evidence expires independently of its booked provider reset.
EVIDENCE_TTL_S = 6 * 60 * 60

#: Probe/harness evidence is leg-level; provider responses keep exact-tuple scope
#: (omitted account/combo dimensions are not wildcards for provider responses).
_SCOPE = "((account IS ? AND combo IS ?) OR (account IS NULL AND combo IS NULL AND source != 'provider-response'))"


def _down_seconds(down_s):
    """Validated PLANGRAPH_LEG_DOWN_S, capped at RESET_CAP_S; never raises.

    An invalid value falls back to 1800. The 24 h cap holds on every booking
    path (D-3685): an explicit down_s or a large PLANGRAPH_LEG_DOWN_S never
    parks a leg for days.
    """
    if down_s is not None:
        return min(down_s, RESET_CAP_S)
    try:
        value = float(os.environ.get('PLANGRAPH_LEG_DOWN_S', 1800))
    except (TypeError, ValueError):
        return 1800
    return min(value if math.isfinite(value) and value >= 0 else 1800, RESET_CAP_S)


class LegStateError(ValueError):
    """Not a provider limit response."""


def _redact(body, limit=300):
    body = re.sub(r"(?i)\bBearer\s+[^\s\"',;]+", "[REDACTED]", body)
    body = re.sub(r"(?i)\bsk-[\w-]+", "[REDACTED]", body)
    body = re.sub(r"(?i)(?:api[_-]?key|access[_-]?token|authorization)\s*[\"']?\s*[:=]\s*[\"']?[^\s\"',;}]+", "[REDACTED]", body)
    body = re.sub(r"\b[A-Za-z0-9_+/=-]{32,}\b", "[REDACTED]", body)
    return body[:limit]


#: D-3685: cooldown units by full word, so the match is longest-first with a
#: word boundary ("500ms" is 0.5 s, never 500 minutes). "ms"/milliseconds are
#: milliseconds; an unknown unit makes the whole reset unparsed (None), never
#: a misread.
_UNITS = {'ms': .001, 'msec': .001, 'msecs': .001,
          'millisecond': .001, 'milliseconds': .001,
          's': 1, 'sec': 1, 'secs': 1, 'second': 1, 'seconds': 1,
          'm': 60, 'min': 60, 'mins': 60, 'minute': 60, 'minutes': 60,
          'h': 3600, 'hr': 3600, 'hrs': 3600, 'hour': 3600, 'hours': 3600,
          'd': 86400, 'day': 86400, 'days': 86400}

_COOLDOWN_PREFIX = re.compile(r"resets?\s+(?:in|after)\s+", re.I)
_COOLDOWN_PAIR = re.compile(r"\s*(\d+(?:\.\d+)?)\s*([a-z]+)", re.I)


def _reset_seconds(body):
    """Return an explicit prose cooldown, keeping zero distinct from absent."""
    match = _COOLDOWN_PREFIX.search(body)
    if match is None:
        return None
    total, pos, seen = 0.0, match.end(), False
    while True:
        pair = _COOLDOWN_PAIR.match(body, pos)
        if pair is None:
            return total if seen else None
        factor = _UNITS.get(pair[2].lower())
        if factor is None:
            return None  # unknown unit: the whole reset is unparsed
        total += float(pair[1]) * factor
        pos, seen = pair.end(), True


def _elapsed_cooldown(headers, body):
    """True for an explicitly elapsed cooldown (D-3685): nothing to book.

    "reset after 0s"/"resets in 0s" prose or a retry-after header of 0 say the
    window already passed: no writer books a limit row or a down_s fallback
    for it (a shared rule, so the booking paths cannot drift apart).
    """
    try:
        if float(headers.get('retry-after', 'nan')) == 0:
            return True
    except (TypeError, ValueError):
        pass
    return _reset_seconds(body) == 0


def _reset(headers, body, now):
    reset = None
    for key in ("retry-after", "x-ratelimit-reset"):
        value = headers.get(key)
        if value is None:
            continue
        try:
            number = float(value)
            if math.isfinite(number) and number >= 0:
                reset = now + number if key == "retry-after" else number
                break
        except (ValueError, TypeError):
            try:
                reset = parsedate_to_datetime(value).timestamp()
                break
            except (ValueError, TypeError, OverflowError):
                try:
                    reset = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
                    break
                except (ValueError, TypeError, AttributeError):
                    pass
    if reset is None:
        # D-3685: OmniRoute prints "(reset after 3h 12m 5s)"; accept "reset
        # after" as well as "resets in" over the same unit parsing.
        total = _reset_seconds(body)
        if total is not None:
            # "reset after 0s": the cooldown already elapsed, nothing to book.
            reset = now + total if total > 0 else None
    return None if reset is None else min(reset, now + RESET_CAP_S)


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
        # D-3685: an explicitly elapsed cooldown books nothing -- no reset-less
        # limit row that would read "limited" for REPROBE_MINUTES.
        if _elapsed_cooldown(headers, body):
            return
        reset = _reset(headers, body, now)
        with self._connect() as db:
            db.execute("INSERT INTO leg_events VALUES (NULL,?,?,?,?,?,?,?,?,?,?)", (leg, account, combo, status, _redact(body), None if reset is None else reset - now, reset, now, caller, "provider-response"))

    def record_probe_pass(self, leg, caller, now=None, account=None, combo=None):
        if not caller:
            raise LegStateError("requires successful provider response caller")
        with self._connect() as db:
            db.execute("INSERT INTO leg_events VALUES (NULL,?,?,?,?,?,?,?,?,?,?)", (leg, account, combo, 200, "", None, None, time.time() if now is None else now, caller, "provider-probe"))

    def record_probe_fail(self, leg, reason, caller, now=None, down_s=None, account=None, combo=None, *, serving=False, stderr_excerpt=''):
        if not leg or not caller or not reason:
            raise LegStateError('requires leg, reason and caller')
        now = time.time() if now is None else now
        down_s = None if serving else _down_seconds(down_s)  # capped at RESET_CAP_S
        with self._connect() as db:
            db.execute('INSERT INTO leg_events VALUES (NULL,?,?,?,?,?,?,?,?,?,?)',
                       (leg, account, combo, 503, _redact(reason) + (': ' + _redact(stderr_excerpt, 400) if stderr_excerpt else ''), down_s,
                        None if serving else now + down_s, now, caller,
                        'probe_failed_leg_serving' if serving else 'liveness-probe'))

    def last_probe_ts(self, leg):
        """Successful probe evidence only: a failed probe is not fresh evidence."""
        if not self.path.exists():
            return None
        try:
            with sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True) as db:
                return db.execute("SELECT max(ts) FROM leg_events WHERE leg_key=? AND source='provider-probe'", (leg,)).fetchone()[0]
        except sqlite3.Error:
            return None

    def last_probe_attempt_ts(self, leg):
        """The newest probe attempt (pass or fail); single-flight reuse only."""
        if not self.path.exists():
            return None
        try:
            with sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True) as db:
                return db.execute("SELECT max(ts) FROM leg_events WHERE leg_key=? AND source IN ('liveness-probe','provider-probe','probe_failed_leg_serving')", (leg,)).fetchone()[0]
        except sqlite3.Error:
            return None

    def record_harness_limit(self, leg, line, caller, now=None, account=None, combo=None, down_s=None):
        if not leg or not caller or not line.strip():
            raise LegStateError('requires leg, quota line and caller')
        # An elapsed cooldown permits neither a fallback booking nor a reset-less limit.
        if _elapsed_cooldown({}, line):
            return
        now = time.time() if now is None else now
        reset = _reset({}, line, now)
        if reset is None and down_s is not None:
            # D-3605 (a): no provider reset time; _down_seconds is capped, so
            # the fallback stays inside RESET_CAP_S like every other booking.
            reset = now + _down_seconds(down_s)
        with self._connect() as db:
            db.execute("INSERT INTO leg_events VALUES (NULL,?,?,?,?,?,?,?,?,?,?)",
                       (leg, account, combo, 429, _redact(line),
                        None if reset is None else reset - now, reset, now, caller, 'harness-stderr'))

    def leg_status(self, leg, now=None, account=None, combo=None):
        now = time.time() if now is None else now
        if not self.path.exists() or self.path.stat().st_size == 0:
            return {"state": "ok", "evidence": None}
        try:  # Read-only: no database creation or expiry mutation.
            with sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True) as db:
                db.row_factory = sqlite3.Row
                rows = db.execute(
                    "SELECT * FROM leg_events WHERE leg_key=? AND " + _SCOPE + " AND ts<=? ORDER BY ts DESC,id DESC",
                    (leg, account, combo, now))
                rows = list(rows)
                # Serving evidence supersedes older probe failures, NEVER provider limits.
                serving = next((i for i, r in enumerate(rows) if r['source'] == 'probe_failed_leg_serving'), len(rows))
                rows = iter(r for i, r in enumerate(rows) if r['source'] != 'probe_failed_leg_serving'
                            and (r['source'] != 'liveness-probe' or i < serving))
                row = next(rows, None)
                if row is None or row["status"] == 200:
                    return {"state": "ok", "evidence": None}
                evidence = {k: row[k] for k in ("status", "body_excerpt", "reset_at", "ts", "caller")}
                evidence["event_id"] = row["id"]
                if now >= row['ts'] + EVIDENCE_TTL_S:
                    return {'state': 'reprobe', 'evidence': evidence}
                if row['source'] == 'liveness-probe':
                    # Two consecutive failures book down; serving evidence breaks the streak.
                    streak = 1
                    behind = None
                    for older in rows:
                        if older['source'] == 'liveness-probe' and older['status'] == 503:
                            # A live predecessor must exist when this failure books
                            # the streak; its later expiry cannot unbook that down window.
                            streak += int(row['ts'] < min(older['ts'] + EVIDENCE_TTL_S, older['reset_at'] or older['ts']))
                        else:
                            behind = older
                            break
                    # Probe failures never hide an older provider limit with a future reset (D-3685).
                    if (behind is not None and behind['source'] in ('harness-stderr', 'provider-response')
                            and behind['reset_at'] is not None and now < min(behind['reset_at'], behind['ts'] + EVIDENCE_TTL_S)):
                        evidence = {k: behind[k] for k in ("status", "body_excerpt", "reset_at", "ts", "caller")}
                        evidence["event_id"] = behind["id"]
                        return {"state": "limited", "evidence": evidence}
                    reset = row["reset_at"]
                    down = streak >= 2 and reset is not None and now < reset
                    return {'state': 'down' if down else 'reprobe', 'evidence': evidence}
                reset = row["reset_at"]
                state = (("limited" if now < reset else "ok") if reset is not None
                         else ("limited" if now < row["ts"] + REPROBE_MINUTES * 60 else "reprobe"))
                return {"state": state, "evidence": evidence}
        except (sqlite3.Error, OSError):
            return {"state": "ok", "evidence": None}

    def last_probe_outcome(self, leg):
        """Newest probe attempt as a reusable single-flight outcome, else None."""
        if not self.path.exists():
            return None
        try:
            with sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True) as db:
                db.row_factory = sqlite3.Row
                row = db.execute("SELECT source, body_excerpt, ts FROM leg_events WHERE leg_key=? AND source IN ('liveness-probe','provider-probe','probe_failed_leg_serving') ORDER BY ts DESC,id DESC LIMIT 1", (leg,)).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        ok = row['source'] == 'provider-probe'
        return dict(ok=ok, reason='' if ok else (row['body_excerpt'] or 'probe failed'), ts=row['ts'])


def _default_store():
    return LegStateStore(Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "plangraph" / "leg-state.sqlite")


def record_provider_response(leg, status, body, headers, caller, now=None, account=None, combo=None):
    return _default_store().record_provider_response(leg, status, body, headers, caller, now, account, combo)


def record_probe_pass(leg, caller, now=None, account=None, combo=None):
    return _default_store().record_probe_pass(leg, caller, now, account, combo)


def record_harness_limit(leg, line, caller, now=None, account=None, combo=None, down_s=None):
    return _default_store().record_harness_limit(leg, line, caller, now, account, combo, down_s)


def leg_status(leg, now=None, account=None, combo=None):
    return _default_store().leg_status(leg, now, account, combo)


def record_probe_fail(leg, reason, caller, now=None, down_s=None, account=None, combo=None, *, serving=False, stderr_excerpt=''):
    return _default_store().record_probe_fail(leg, reason, caller, now, down_s, account, combo, serving=serving, stderr_excerpt=stderr_excerpt)


def last_probe_ts(leg):
    return _default_store().last_probe_ts(leg)


def last_probe_outcome(leg):
    return _default_store().last_probe_outcome(leg)


def model_admitted(model, *, enabled=False):
    """Shared catalog policy; live authority replaces only local availability."""
    from .resolver import catalog_row_admitted
    from plangraph.mimo_policy import mimo_path_allowed
    mid = str(model.get('id') or model.get('model_id') or '')
    admission = dict(model, available=1) if enabled else model
    return (mimo_path_allowed(mid) and catalog_row_admitted(admission)
            and not (enabled and model.get('disabled'))
            and str(model.get('status', 'active')).lower() in ('active', '')
            and not (mid.lower().startswith('cxa/')
                     and not mid.lower().startswith('cxa/gpt-6-luna')))


class LegAdapter:
    """Explicit opt-in reader; one cached boundary shared by routing and probes.

    Discovery names actual standalone identities, never combo/request aliases.
    Failures retain the previous snapshot but never admit from it as healthy.
    """

    def __init__(self, store, provider, models=None, *, enabled=False,
                 clock=time.monotonic, ttl_s=60):
        self.store, self.provider = store, provider
        self.models = self.standalone_models if models is None else models
        self.enabled, self.clock, self.ttl_s = enabled, clock, ttl_s
        self._at, self._states, self._served = None, {}, set()
    def __getattr__(self, name):
        return getattr(self.store, name)
    @staticmethod
    def standalone_models():
        import subprocess
        try:
            result = subprocess.run(['opencode', 'models', '--standalone'], capture_output=True,
                                    text=True, timeout=8, shell=False, check=True)
        except (OSError, subprocess.SubprocessError):
            return []
        return result.stdout.splitlines()
    @staticmethod
    def identity(model):
        return re.sub(r'(claude-sonnet-5-5)-(?:medium|high|xhigh|max)$', r'\1', model)

    def __call__(self, leg, **kwargs):
        return self.leg_status(leg, **kwargs)

    def quarantine(self, leg):
        if self.enabled:
            self.leg_status(leg)
            self._served.discard(self.identity(leg))
    def leg_status(self, leg, **kwargs):
        if not self.enabled:
            return self.store.leg_status(leg, **kwargs)
        from .resolver import OPERATOR_REMOVED_MODELS
        if leg in OPERATOR_REMOVED_MODELS:
            return dict(state='unavailable', evidence=None)
        now = self.clock()
        if self._at is None or now - self._at >= self.ttl_s:
            try:
                states = self.provider.snapshot()
                served = {self.identity(mid) for mid in (self.models() if callable(self.models) else self.models)}
                self._states, self._served = states, served
                self._error = None if states else 'unreachable'
            except Exception:
                self._error = 'unknown'
            self._at = now
        state = self._error
        if not state:
            identity = self.identity(leg)
            provider = self._states.get(leg.split('/')[0])
            if identity not in self._served:
                state = 'not_listed'
            elif provider is None:
                state = 'unreachable'
            elif provider.stale:
                state = 'stale'
            else:
                state = 'ok' if provider.available else 'unavailable'
        # Legacy planner understands limited/down only; preserve explicit reason.
        return dict(state='ok' if state == 'ok' else 'limited',
                    reason=state, evidence=None)
