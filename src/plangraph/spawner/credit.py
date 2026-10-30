"""Best-effort, private accounting for prepaid console credits."""
from __future__ import annotations

from datetime import datetime, timezone, date
import json
import os
from pathlib import Path
import sqlite3
import time
from urllib.parse import quote

from plangraph.routing_support.private_io import append_private


def session_spend(db_path, job_id):
    unavailable = dict(cost_usd=None, tokens_input=None, tokens_output=None, source='unavailable')
    try:
        conn = sqlite3.connect('file:' + quote(str(db_path)) + '?mode=ro', uri=True, timeout=2)
        try:
            sessions = conn.execute('SELECT id FROM session_v2 WHERE title = ?',
                                    ('plangraph-' + job_id,)).fetchall()
            if len(sessions) != 1:
                return unavailable
            totals = dict(cost_usd=None, tokens_input=None, tokens_output=None, source='opencode')
            for (raw,) in conn.execute("SELECT data FROM session_message WHERE session_id = ? AND type = 'assistant'", sessions[0]):
                data = json.loads(raw)
                for key, value in [('cost_usd', data.get('cost')),
                                   ('tokens_input', data.get('tokens', {}).get('input')),
                                   ('tokens_output', data.get('tokens', {}).get('output'))]:
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        totals[key] = (totals[key] or 0) + value
            return totals
        finally:
            conn.close()
    except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError):
        return unavailable


def capture_spend(job, env):
    try:
        from plangraph.planner.legs import DEFAULT_LEGS
        if any(l.tier == 'credit' and l.model == job.model_or_combo for l in DEFAULT_LEGS):
            path = Path(env.get('XDG_DATA_HOME') or Path.home() / '.local/share') / 'opencode/opencode.db'
            job.credit_spend = session_spend(path, job.job_id)
    except Exception:
        pass


def record_spend(state, record, leg, job, *, env=None, db_path=None):
    """Accounting must never change the outcome of a run."""
    try:
        if leg.tier != 'credit':
            return
        env = os.environ if env is None else env
        path = db_path or Path(env.get('XDG_DATA_HOME') or Path.home() / '.local/share') / 'opencode/opencode.db'
        entry = dict(run_id=record['run_id'], leg=leg.id, model=leg.model,
                     started=record.get('started'), ended=time.time(), status=job.status)
        entry.update(getattr(job, 'credit_spend', None) or session_spend(path, job.job_id))
        append_private(Path(state) / 'credit-spend.jsonl', json.dumps(entry) + '\n')
    except Exception:
        pass


def credit_report(state, *, policy=None, today=None):
    from plangraph.planner.route_policy import load_route_policy
    policy = policy or load_route_policy()
    today = today or datetime.now(timezone.utc).date()
    entries = []
    try:
        for line in (Path(state) / 'credit-spend.jsonl').read_text().splitlines():
            try:
                entries.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    rows = []
    for provider, config in policy.credit_tiers.items():
        runs = [e for e in entries if e.get('model', '').partition('/')[0] == provider]
        rows.append(dict(provider=provider, expires=config['expires'],
                         days_left=(date.fromisoformat(config['expires']) - today).days,
                         runs=len(runs), **{key: sum(e.get(key) or 0 for e in runs)
                         for key in ('cost_usd', 'tokens_input', 'tokens_output')}))
    return rows
