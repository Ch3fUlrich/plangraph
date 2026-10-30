"""Response-side served-model witness from the OmniRoute call log (D-3226).

A review seat on an ``omniroute`` provider sends ``x-omniroute-session-id:
plangraph-<job_id>``; the gateway stores it as the call log's ``sessionTag``.
After the child exits, the runner (a trusted in-process adapter) reads the
call log rows with that tag and records the model the gateway served. The
call-log API needs a management token: the operator provides it as
``PLANGRAPH_GATEWAY_LOG_TOKEN``; without it the seat stays unwitnessed with an
explicit event.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request

SESSION_HEADER = 'x-omniroute-session-id'
TOKEN_ENV = 'PLANGRAPH_GATEWAY_LOG_TOKEN'
URL_ENVS = ('OMNIROUTE_BASE_URL', 'PLANGRAPH_GATEWAY_URL')
DEFAULT_URL = 'http://127.0.0.1:20128'
WITNESS_KIND = 'response-side-gateway-call-log'


def session_tag(job_id):
    return f'plangraph-{job_id}'


def tag_session(cfg, job_id):
    """Add the session header to every ``omniroute`` provider block in an opencode config."""
    for key in ('provider', 'providers'):
        block = (cfg.get(key) or {}).get('omniroute') if isinstance(cfg.get(key), dict) else None
        if isinstance(block, dict):
            options = block.setdefault('options', {})
            if isinstance(options, dict):
                options.setdefault('headers', {})[SESSION_HEADER] = session_tag(job_id)


def tagged(config_content, job_id):
    """True when the child's final config carries this job's session header."""
    return session_tag(job_id) in (config_content or '') and SESSION_HEADER in (config_content or '')


def served_from_rows(rows, tag):
    """The one model the gateway served for ``tag``; None when absent or ambiguous."""
    served = set()
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict) or row.get('sessionTag') != tag:
            continue
        try:
            ok = 200 <= int(row.get('status')) < 300
        except (TypeError, ValueError):
            ok = False
        model = str(row.get('model') or '').strip()
        if ok and model:
            provider = str(row.get('provider') or '').strip()
            served.add(model if '/' in model or not provider else f'{provider}/{model}')
    return served.pop() if len(served) == 1 else None


def fetch_rows(tag, env, *, timeout=5, opener=urllib.request.urlopen, secrets=os.environ):
    """Call-log rows for ``tag``; raises LookupError naming why they are unreadable.

    The token comes from the spawner's own env (``secrets``), never the child's:
    ``build_child_env`` drops it.
    """
    token = secrets.get(TOKEN_ENV)
    if not token:
        raise LookupError(f'no {TOKEN_ENV}')
    base = next((env[n] for n in URL_ENVS if env.get(n)), DEFAULT_URL).rstrip('/')
    if base.endswith('/v1'):
        base = base[:-3]
    query = urllib.parse.urlencode({'sessionTag': tag, 'limit': 200})
    request = urllib.request.Request(f'{base}/api/usage/call-logs?{query}',
                                     headers={'Authorization': 'Bearer ' + token})
    try:
        with opener(request, timeout=timeout) as response:
            return json.loads(response.read().decode('utf-8'))
    except (OSError, ValueError) as exc:
        raise LookupError('call log unreadable: ' + type(exc).__name__) from None


def witness(runner, job_id, job, env, *, attempts=3, delay=1.0, fetch=None):
    """Record the gateway-served model on ``job`` through the runner's adapter check."""
    fetch = fetch or fetch_rows
    if job.served_model not in (None, '', 'unwitnessed') or not tagged(env.get('OPENCODE_CONFIG_CONTENT'), job_id):
        return
    tag = session_tag(job_id)
    reason = 'no call log row for the session tag'
    for attempt in range(attempts):   # the gateway may write the row after the response
        try:
            model = served_from_rows(fetch(tag, env), tag)
        except LookupError as exc:
            reason = str(exc)
            break
        if model:
            try:
                runner.served_witness(job_id, model, adapter_token=runner._adapter_tokens.get(job_id))
            except ValueError as exc:
                reason = str(exc)
                break
            job.witness_kind = WITNESS_KIND
            job.served_witness_reason = 'gateway call log (sessionTag) response-side model'
            return
        if attempt + 1 < attempts:
            time.sleep(delay)
    job.events.append({'event': 'gateway_witness_unavailable', 'reason': reason[:200]})
