"""Response-side served-model witness from the OmniRoute call log (D-3226).

A review seat on an ``omniroute`` provider sends ``x-omniroute-session-id:
plangraph-<job_id>``; the gateway stores it as the call log's ``sessionTag``.
After the child exits, the runner (a trusted in-process adapter) reads the
call log rows with that tag and records the model the gateway served. The
call-log API needs a management token: the operator provides it as
``PLANGRAPH_GATEWAY_LOG_TOKEN`` or a private management-key file; without either
the seat stays unwitnessed with an explicit event (required seats refuse launch).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import time
import types
import urllib.parse
import urllib.request

from plangraph.routing_support.opencode_overlay import _strip_jsonc

SESSION_HEADER = 'x-omniroute-session-id'
TOKEN_ENV = 'PLANGRAPH_GATEWAY_LOG_TOKEN'
TOKEN_FILE_ENV = 'PLANGRAPH_GATEWAY_LOG_TOKEN_FILE'
URL_ENVS = ('OMNIROUTE_BASE_URL', 'PLANGRAPH_GATEWAY_URL')
# Rows come from the gateway's own call log, so xiaomi-mimo is its connection, never a direct leg.
PROVIDER_ALIASES = types.MappingProxyType({'xiaomi-mimo': 'mimo'})
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
    matches = []
    for key in ('provider', 'providers'):
        providers = cfg.get(key)
        if not isinstance(providers, dict):
            continue
        for name in ((provider,) if provider else ('omniroute', 'mimo')):
            block = providers.get(name)
            if not isinstance(block, dict) or block.get('disabled') is True:
                continue
            options = block.get('options')
            block_tagged = False
            for target in (block, options if isinstance(options, dict) else {}):
                headers = target.get('headers')
                if isinstance(headers, dict) and headers.get(SESSION_HEADER) == session_tag(job_id):
                    block_tagged = True
            matches.append(block_tagged)
    return bool(matches) and all(matches)


def _config(content):
    try:
        cfg = json.loads(_strip_jsonc(content or '{}'))
        return cfg if isinstance(cfg, dict) else {}
    except (TypeError, ValueError):
        return {}


def _gateway(env):
    for name in URL_ENVS:
        value = env.get(name)
        normalized = value.strip().rstrip('/') if isinstance(value, str) else ''
        if normalized:
            return normalized
    return None


def _gateway_mimo(env):
    gateway = _gateway(env)
    if not gateway:
        return False
    cfg = _config(env.get('OPENCODE_CONFIG_CONTENT'))
    endpoints = []
    seen = False   # a mimo block must exist; no config is not evidence
    for key in ('provider', 'providers'):
        providers = cfg.get(key)
        block = providers.get('mimo') if isinstance(providers, dict) else None
        if not isinstance(block, dict) or block.get('disabled') is True:
            continue
        seen = True
        for name in ('options', 'settings'):
            options = block.get(name)
            if isinstance(options, dict) and 'baseURL' in options:
                endpoints.append(options['baseURL'])
    def normalize(value):
        return value.rstrip('/').removesuffix('/v1') if isinstance(value, str) else None
    # Require an explicit gateway endpoint: a template default is not routing evidence.
    return seen and bool(endpoints) and all(normalize(e) == normalize(gateway) for e in endpoints)


def load_token(secrets=None):
    """Load management credentials only in the parent; never expose failure details."""
    secrets = os.environ if secrets is None else secrets
    token = secrets.get(TOKEN_ENV)
    if token:
        return token
    path = Path(secrets.get(TOKEN_FILE_ENV) or '~/.config/plangraph/omniroute-mgmt.key').expanduser()
    try:
        with path.open() as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                raise OSError
            token = stream.read().strip()
        if token:
            return token
    except (OSError, UnicodeError):
        pass
    raise LookupError('no gateway token in spawn env')


def require_witness(model, env):
    """Fail closed before launching a review whose card requires gateway evidence."""
    from plangraph.planner.routing import SpawnRefused
    provider = str(model or '').split('/', 1)[0]
    reason = (f'not a gateway leg: provider {provider}' if provider not in ('omniroute', 'mimo')
              else 'mimo leg not routed through gateway' if provider == 'mimo' and not _gateway_mimo(env)
              else None)
    try:
        if reason:
            raise LookupError(reason)
        load_token()
        if not _gateway(os.environ):
            raise LookupError('gateway endpoint not configured')
    except LookupError as exc:
        raise SpawnRefused('witness required: ' + str(exc)) from None


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
            provider = PROVIDER_ALIASES.get(provider, provider)
            head, sep, rest = model.partition('/')
            if sep and head in PROVIDER_ALIASES:
                model = PROVIDER_ALIASES[head] + '/' + rest
            served.add(model if '/' in model or not provider else f'{provider}/{model}')
    return served


def fetch_rows(tag, env, *, timeout=5, opener=urllib.request.urlopen, secrets=None):
    """Call-log rows for ``tag``; raises LookupError naming why they are unreadable.

    Both URL and token come from the spawner's own env (``secrets``), never
    the child's. ``build_child_env`` drops the token.
    """
    secrets = os.environ if secrets is None else secrets
    token = load_token(secrets)
    base = _gateway(secrets)
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
    def unavailable(reason):
        job.events.append({'event': 'gateway_witness_unavailable', 'reason': reason})
    if provider not in ('omniroute', 'mimo'):
        unavailable(f'not a gateway leg: provider {provider}')
        return
    if provider == 'mimo' and not _gateway_mimo(env):
        unavailable('mimo leg not routed through gateway')
        return
    if job.served_model not in (None, '', 'unwitnessed'):
        unavailable('served_model already set')
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
