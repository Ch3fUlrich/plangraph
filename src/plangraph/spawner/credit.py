"""Private, best-effort console accounting; failures remain visible."""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timezone, date
import json
import logging
import math
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time
from urllib.parse import quote

from plangraph.routing_support.private_io import atomic_write_private

KEYS = ('cost_usd', 'tokens_input', 'tokens_output')
LOG = logging.getLogger(__name__)


def unavailable(reason):
    return dict.fromkeys(KEYS) | dict(source='unavailable', reason=reason)


def opencode_db_path(env):
    home = Path(env.get('HOME') or env.get('USERPROFILE') or Path.home())
    if env.get('XDG_DATA_HOME'):
        base = Path(env['XDG_DATA_HOME'])
    elif sys.platform == 'win32':
        base = Path(env.get('LOCALAPPDATA') or env.get('APPDATA') or home / 'AppData/Local')
    elif sys.platform == 'darwin':
        base = home / 'Library/Application Support'
    else:
        base = home / '.local/share'
    return base / 'opencode/opencode.db'


def session_spend(db_path, job_id, *, started=None):
    for attempt in range(2):
        try:
            conn = sqlite3.connect('file:' + quote(str(db_path)) + '?mode=ro', uri=True, timeout=2)
            try:
                query = 'SELECT id FROM session_v2 WHERE title = ?'
                args = ['plangraph-' + job_id]
                if started is not None:
                    query += ' AND time_created >= ?'
                    args.append(int(started * 1000))
                sessions = conn.execute(query, args).fetchall()
                if len(sessions) != 1:
                    return unavailable('no matching session' if not sessions else 'ambiguous matching sessions')
                totals = dict.fromkeys(KEYS) | dict(source='opencode')
                bad = 0
                for (raw,) in conn.execute("SELECT data FROM session_message WHERE session_id = ? AND type = 'assistant'", sessions[0]):
                    try:
                        data = json.loads(raw)
                        tokens = data.get('tokens') or {}
                        values = (data.get('cost'), tokens.get('input'), tokens.get('output'))
                    except (ValueError, TypeError, AttributeError):
                        bad += 1
                        continue
                    for key, value in zip(KEYS, values):
                        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                            totals[key] = (totals[key] or 0) + value
                if bad:
                    totals['reason'] = f'skipped {bad} malformed assistant rows'
                return totals
            finally:
                conn.close()
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            if attempt == 0:
                time.sleep(1)
            else:
                return unavailable(f'{type(exc).__name__}: {exc}')


def capture_spend(job, env, *, tier=None):
    """Best-effort spend capture before isolated child data is removed.

    Only credit-tier legs read the store (the tier belongs to the chosen leg,
    never DEFAULT_LEGS or a model-name guess).  Capture can neither fail nor
    block a run: any failure lands on the job as ``unavailable`` evidence and
    no exception escapes.
    """
    if str(tier or '').lower() != 'credit':
        return
    if job is None:
        LOG.warning('credit source=unavailable reason=missing job during capture')
        return
    try:
        job.credit_spend = session_spend(opencode_db_path(env), job.job_id,
                                         started=getattr(job, 'start_time', None))
    except Exception as exc:
        job.credit_spend = unavailable(f'{type(exc).__name__}: {exc}')
        LOG.warning('credit source=unavailable reason=%s', exc)


def read_entries(path):
    """Ledger rows keyed by run_id plus every torn/invalid line kept verbatim.

    Malformed lines are returned as their original bytes so a rewrite can
    never destroy the evidence it could not parse (only a count survives).
    """
    entries, malformed = {}, []
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return entries, malformed
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
            if not isinstance(entry, dict) or not isinstance(entry.get('run_id'), str):
                raise ValueError('missing run_id')
            entries[entry['run_id']] = entry
        except (ValueError, TypeError):
            malformed.append(line)
    return entries, malformed


#: A lock older than this was left by a crashed writer, never a live one.
LOCK_STALE_SECONDS = 300


def ledger_lock_path(path):
    """The ledger's lock directory: outside the ledger's own basename family
    (``path.with_suffix('.lock')`` collides with the ledger's suffix handling)."""
    path = Path(path)
    return path.parent / (path.name + '.lock.d')


def _owner_alive(pid):
    if os.name == 'nt':
        return True   # no safe liveness probe on Windows: age alone reclaims
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _lock_is_stale(lock):
    """True for a crashed writer's lock: too old, or its recorded pid is gone."""
    try:
        age = time.time() - os.path.getmtime(lock)
    except OSError:
        return True
    if age > LOCK_STALE_SECONDS:
        return True
    try:
        pid = int((Path(lock) / 'owner.pid').read_text())
    except (OSError, ValueError):
        return False   # no owner recorded and young: still a live writer
    return not _owner_alive(pid)


@contextmanager
def ledger_lock(path):
    """Serialize ledger writes.

    A crashed writer's stale lock is reclaimed (never spins); a genuinely busy
    ledger raises ``TimeoutError`` so the caller records the spend as
    ``unavailable`` instead of silently dropping it.
    """
    lock = ledger_lock_path(path)
    for attempt in range(201):
        try:
            lock.mkdir(mode=0o700)
            break
        except FileExistsError:
            if attempt == 200:
                raise TimeoutError('credit ledger lock busy')
            if _lock_is_stale(lock):
                shutil.rmtree(lock, ignore_errors=True)
                continue   # reclaimed: retry now, no busy-wait spin
            time.sleep(.01)
    try:
        try:
            (lock / 'owner.pid').write_text(str(os.getpid()))
        except OSError:
            pass
        yield
    finally:
        shutil.rmtree(lock, ignore_errors=True)


def record_spend(state, record, leg, job, *, env=None, db_path=None):
    if leg.tier != 'credit':
        return
    try:
        spend = getattr(job, 'credit_spend', None) or session_spend(
            db_path or opencode_db_path(os.environ if env is None else env), job.job_id,
            started=getattr(job, 'start_time', record.get('started')))
        entry = dict(run_id=record['run_id'], leg=leg.id, model=leg.model,
                     started=record.get('started'), ended=time.time(), status=job.status)
        path = Path(state) / 'credit-spend.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with ledger_lock(path):
            entries, malformed = read_entries(path)
            previous = entries.get(entry['run_id'], {})
            attempts = previous.get('credit_attempts', {})
            attempts[job.job_id] = spend
            entry.update(spend, credit_attempts=attempts)
            for key in KEYS:
                values = [s.get(key) for s in attempts.values() if s.get(key) is not None]
                entry[key] = sum(values) if values else None
            if any(s.get('source') == 'unavailable' for s in attempts.values()):
                entry.update(source='unavailable', reason='; '.join(s.get('reason', 'unknown') for s in attempts.values() if s.get('source') == 'unavailable'))
            entries[entry['run_id']] = entry
            # Torn/invalid lines are rewritten verbatim: a rewrite preserves
            # the evidence it could not parse, never a placeholder.
            data = b''.join(line + b'\n' for line in malformed)
            data += ''.join(json.dumps(e) + '\n' for e in entries.values()).encode('utf-8')
            atomic_write_private(path, data)
    except Exception as exc:
        reason = f'{type(exc).__name__}: {exc}'
        record['credit_spend'] = unavailable(reason)
        LOG.warning('credit source=unavailable reason=%s', reason)


TABLE_COLUMNS = ('provider', 'expires', 'days_left', 'runs',
                 'unknown_runs', 'unavailable_runs', 'malformed_lines',
                 'cost_usd', 'tokens_input', 'tokens_output')


def _cell(row, key):
    value = row.get(key)
    return 'n/a' if value is None else str(value)


def credit_table(rows):
    """Human-readable report; machine-readable output stays behind --json."""
    widths = {key: len(key) for key in TABLE_COLUMNS}
    for row in rows:
        for key in TABLE_COLUMNS:
            widths[key] = max(widths[key], len(_cell(row, key)))
    lines = ['  '.join(key.ljust(widths[key]) for key in TABLE_COLUMNS)]
    for row in rows:
        lines.append('  '.join(_cell(row, key).ljust(widths[key]) for key in TABLE_COLUMNS))
    return '\n'.join(lines)


def _policy_today(policy):
    """The day the policy gates credit with; re-read per call (never frozen)."""
    getter = getattr(policy, 'current_today', None)
    return getter() if callable(getter) else policy.today


def _expiry(config, today):
    """``(expires, days_left, reason)``; a missing/unparsable ``expires`` gates
    as ``credit_expired`` exactly like the shared gate -- a report never raises
    on the degenerate policy entries the gate fail-closes on."""
    from plangraph.routing_support.credit_gate import GATED
    try:
        expires = date.fromisoformat(config['expires'])
    except (KeyError, TypeError, ValueError):
        return (config.get('expires') if isinstance(config, Mapping) else None, None, GATED)
    return config['expires'], (expires - today).days, GATED if expires < today else None


def credit_report(state, *, policy=None, today=None):
    from plangraph.planner.route_policy import load_route_policy
    policy = policy or load_route_policy()
    # The same day the policy gates credit with, never a second clock.
    today = today or _policy_today(policy)
    try:
        entries, malformed = read_entries(Path(state) / 'credit-spend.jsonl')
    except OSError as exc:
        LOG.warning('credit source=unavailable reason=%s', exc)
        entries, malformed = {}, []
    rows = []
    for provider, config in policy.credit_tiers.items():
        runs = [e for e in entries.values() if str(e.get('model', '')).partition('/')[0] == provider]
        expires, days_left, reason = _expiry(config, today)
        rows.append(dict(provider=provider, expires=expires, days_left=days_left, reason=reason,
                         runs=len(runs), unknown_runs=sum(e.get('cost_usd') is None or e.get('source') == 'unavailable' for e in runs),
                         unavailable_runs=sum(e.get('source') == 'unavailable' for e in runs),
                         malformed_lines=len(malformed),
                         **{key: sum(e.get(key) or 0 for e in runs) for key in KEYS}))
    return rows
