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
WITNESS_KIND = 'response-side-gateway-call-log'


def session_tag(job_id):
    return f'plangraph-{job_id}'


def tag_session(cfg, job_id):
    """Add the session header to configured gateway-capable provider blocks.

    It goes into the provider-level ``headers`` map, where build_child_env puts the
    envelope headers that reach the gateway, and into ``options.headers``.
    """
    for key in ('provider', 'providers'):
        providers = cfg.get(key)
        if isinstance(providers, dict):
            for name in ('omniroute', 'mimo'):
                _tag_block(providers.get(name), job_id)


def _tag_block(block, job_id):
    if isinstance(block, dict):
        options = block.setdefault('options', {})
        for target in (block, options if isinstance(options, dict) else {}):
            headers = target.setdefault('headers', {})
            if isinstance(headers, dict):
                headers[SESSION_HEADER] = session_tag(job_id)


def tagged(config_content, job_id, provider=None):
    """True when the child's final config carries this job's session header."""
    cfg = _config(config_content)
    for key in ('provider', 'providers'):
        providers = cfg.get(key)
        if not isinstance(providers, dict):
            continue
        for name in ((provider,) if provider else ('omniroute', 'mimo')):
            block = providers.get(name)
            if not isinstance(block, dict):
                continue
            options = block.get('options')
            for target in (block, options if isinstance(options, dict) else {}):
                headers = target.get('headers')
                if isinstance(headers, dict) and headers.get(SESSION_HEADER) == session_tag(job_id):
                    return True
    return False


def _config(content):
    try:
        cfg = json.loads(content or '{}')
        return cfg if isinstance(cfg, dict) else {}
    except (TypeError, ValueError):
        return {}


def _gateway(env):
    return next((env[n].strip().rstrip('/') for n in URL_ENVS
                 if isinstance(env.get(n), str) and env[n].strip()), None)


def _gateway_mimo(env):
    gateway = _gateway(env)
    if not gateway:
        return False
    cfg = _config(env.get('OPENCODE_CONFIG_CONTENT'))
    endpoints = []
    for key in ('provider', 'providers'):
        providers = cfg.get(key)
        block = providers.get('mimo') if isinstance(providers, dict) else None
        if not isinstance(block, dict):
            continue
        for name in ('options', 'settings'):
            options = block.get(name)
            if isinstance(options, dict) and 'baseURL' in options:
                endpoints.append(options['baseURL'])
    normalize = lambda value: value.rstrip('/').removesuffix('/v1') if isinstance(value, str) else None
    return bool(endpoints) and all(normalize(e) == normalize(gateway) for e in endpoints)


def served_models(rows, tag):
    """Every model the gateway served (2xx rows) for ``tag``, as ``provider/model``."""
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
    return served


def fetch_rows(tag, env, *, timeout=5, opener=urllib.request.urlopen, secrets=os.environ):
    """Call-log rows for ``tag``; raises LookupError naming why they are unreadable.

    The token comes from the spawner's own env (``secrets``), never the child's:
    ``build_child_env`` drops it.
    """
    token = secrets.get(TOKEN_ENV)
    if not token:
        raise LookupError(f'no {TOKEN_ENV}')
    base = _gateway(env)
    if not base:
        raise LookupError('gateway endpoint not configured')
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
    # Only a leg served through the gateway can have rows: build_child_env adds an
    # omniroute block to every config, so the tag alone does not mean a gateway leg.
    provider = str(getattr(job, 'model_or_combo', '') or '').split('/', 1)[0]
    if provider != 'omniroute' and not (provider == 'mimo' and _gateway_mimo(env)):
        return
    if job.served_model not in (None, '', 'unwitnessed'):
        return
    tag = session_tag(job_id)
    reason = 'no call log row for the session tag'
    if not tagged(env.get('OPENCODE_CONFIG_CONTENT'), job_id, provider):
        attempts, reason = 0, 'final config carries no session tag'
    for attempt in range(attempts):   # the gateway may write the row after the response
        try:
            served = served_models(fetch(tag, env), tag)
        except LookupError as exc:
            reason = str(exc)
            break
        if len(served) > 1:
            reason = 'ambiguous: call log rows name %d served models' % len(served)
            break
        model = served.pop() if served else None
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
