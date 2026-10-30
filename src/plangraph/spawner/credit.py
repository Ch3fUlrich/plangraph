"""Private, best-effort console accounting; failures remain visible."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone, date
import json
import logging
import math
import os
from pathlib import Path
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


def capture_spend(job, env):
    # Capture before isolated child data is removed. Tier belongs to the chosen
    # leg, not DEFAULT_LEGS or a model-name guess; record_spend applies it later.
    if job is None:
        LOG.warning('credit source=unavailable reason=missing job during capture')
        return
    job.credit_spend = session_spend(opencode_db_path(env), job.job_id,
                                     started=getattr(job, 'start_time', None))


def read_entries(path):
    entries, malformed = {}, 0
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return entries, malformed
    for line in lines:
        try:
            entry = json.loads(line)
            if not isinstance(entry, dict) or not isinstance(entry.get('run_id'), str):
                raise ValueError('missing run_id')
            entries[entry['run_id']] = entry
        except (ValueError, TypeError):
            malformed += 1
    return entries, malformed


@contextmanager
def ledger_lock(path):
    lock = path.with_suffix('.lock')
    for attempt in range(201):
        try:
            lock.mkdir(mode=0o700)
            break
        except FileExistsError:
            if attempt == 200:
                raise TimeoutError('credit ledger lock busy')
            time.sleep(.01)
    try:
        yield
    finally:
        lock.rmdir()


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
            # Preserve evidence of torn/invalid lines across subsequent writes.
            text = ''.join(json.dumps(e) + '\n' for e in entries.values()) + 'invalid\n' * malformed
            atomic_write_private(path, text)
    except Exception as exc:
        reason = f'{type(exc).__name__}: {exc}'
        record['credit_spend'] = unavailable(reason)
        LOG.warning('credit source=unavailable reason=%s', reason)


def credit_report(state, *, policy=None, today=None):
    from plangraph.planner.route_policy import load_route_policy
    policy = policy or load_route_policy()
    # The same frozen today the policy gates credit with, never a second clock.
    today = today or policy.today
    try:
        entries, malformed = read_entries(Path(state) / 'credit-spend.jsonl')
    except OSError as exc:
        LOG.warning('credit source=unavailable reason=%s', exc)
        entries, malformed = {}, 1
    rows = []
    for provider, config in policy.credit_tiers.items():
        runs = [e for e in entries.values() if str(e.get('model', '')).partition('/')[0] == provider]
        rows.append(dict(provider=provider, expires=config['expires'],
                         days_left=(date.fromisoformat(config['expires']) - today).days,
                         runs=len(runs), unknown_runs=sum(e.get('cost_usd') is None or e.get('source') == 'unavailable' for e in runs),
                         unavailable_runs=sum(e.get('source') == 'unavailable' for e in runs),
                         malformed_lines=malformed,
                         **{key: sum(e.get(key) or 0 for e in runs) for key in KEYS}))
    return rows
