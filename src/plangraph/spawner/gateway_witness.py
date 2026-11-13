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


def _gateway_mimo(env, mimo_endpoints=None):
    gateway = _gateway(env)
    if not gateway:
        return False
    seen, endpoints = _mimo_endpoints(env) if mimo_endpoints is None else mimo_endpoints
    def normalize(value):
        return value.rstrip('/').removesuffix('/v1') if isinstance(value, str) else None
    # No explicit endpoint means the template default routes through the gateway.
    return seen and all(normalize(e) == normalize(gateway) for e in endpoints)


def _mimo_endpoints(env):
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
    return seen, endpoints


def _owner_only(info, windows=None):
    """Regular file readable by its owner only."""
    # Windows mode bits cannot verify owner-only access; silently refuse key files there.
    if windows is None:
        windows = os.name == 'nt'
    return stat.S_ISREG(info.st_mode) and not windows and stat.S_IMODE(info.st_mode) == 0o600


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
            if not _owner_only(info):
                raise OSError
            token = stream.read().strip()
        if token:
            return token
    except (OSError, UnicodeError):
        pass
    raise LookupError('no ' + TOKEN_ENV)


def gateway_leg(model, env=None, *, mimo_endpoints=None):
    """Whether the selected provider is routed through the gateway."""
    if env is None:
        from plangraph.routing_support.clients import build_child_env
        env = build_child_env()
    provider = str(model or '').split('/', 1)[0]
    return provider == 'omniroute' or (provider == 'mimo' and _gateway_mimo(env, mimo_endpoints))


def require_witness(model, env=None, *, secrets=None):
    """Fail closed before launching a review whose card requires gateway evidence."""
    from plangraph.planner.routing import SpawnRefused
    from plangraph.routing_support.clients import build_child_env
    env = build_child_env() if env is None else env
    secrets = os.environ if secrets is None else secrets
    provider = str(model or '').split('/', 1)[0]
    reason = None if gateway_leg(model, env) else (
        'mimo leg not routed through gateway' if provider == 'mimo'
        else f'not a gateway leg: provider {provider}')
    try:
        if reason:
            raise LookupError(reason)
        load_token(secrets)
        if not _gateway(env) or not _gateway(secrets):
            raise LookupError('gateway endpoint not configured')
        if _gateway(env).rstrip('/').removesuffix('/v1') != _gateway(secrets).rstrip('/').removesuffix('/v1'):
            raise LookupError('child gateway endpoint differs from witness endpoint')
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


QUOTA_POLL_IDLE_S = 60.0   # a gateway leg silent this long is checked against the call log
QUOTA_POLL_EVERY_S = 30.0


def quota_refusal(rows, tag):
    """First quota refusal line the gateway logged for ``tag`` (429 row or quota error text), else None.

    The stalled-run case: the harness printed nothing, but the call log row
    carries the gateway's error text, including its reset time.
    """
    from .env_redact import early_refusal
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict) or row.get('sessionTag') != tag:
            continue
        text = ' '.join(str(row.get(k) or '') for k in ('error_summary', 'errorSummary', 'error')).strip()
        try:
            status = int(row.get('status'))
        except (TypeError, ValueError):
            status = 0
        if not text or 200 <= status < 300:
            continue
        line = ' '.join(text.split())[:300]
        found = early_refusal(line) or early_refusal('Error: ' + line)
        if found:
            return found
        if status == 429:
            return 'HTTP 429: ' + line
    return None


def poll_quota(job_id, job, env, *, fetch=None):
    """Quota refusal text for a running gateway leg, or None; never raises."""
    if not gateway_leg(getattr(job, 'model_or_combo', None), env):
        return None
    try:
        return quota_refusal((fetch or fetch_rows)(session_tag(job_id), env), session_tag(job_id))
    except Exception:
        return None


class QuotaWatch:
    """Throttled call-log check for a gateway leg that has gone silent (used by the run watchdog)."""

    def __init__(self, job, env):
        self.on = gateway_leg(getattr(job, 'model_or_combo', None), env)
        self.last = 0.0

    def delay(self, delay):
        return min(delay, QUOTA_POLL_EVERY_S) if self.on else delay

    def check(self, job_id, job, env, refused):
        now = time.time()
        due = (self.on and not refused.is_set() and now - self.last >= QUOTA_POLL_EVERY_S
               and now - max(job.last_activity_ts or 0, job.start_time) >= QUOTA_POLL_IDLE_S)
        if due:
            self.last = now
        return due

    async def poll(self, job_id, job, env):
        import asyncio
        return await asyncio.to_thread(poll_quota, job_id, job, env)


def witness(runner, job_id, job, env, *, attempts=3, delay=1.0, fetch=None):
    """Record the gateway-served model on ``job`` through the runner's adapter check."""
    fetch = fetch or fetch_rows
    # Only a leg served through the gateway can have rows: build_child_env adds an
    # omniroute block to every config, so the tag alone does not mean a gateway leg.
    provider = str(getattr(job, 'model_or_combo', '') or '').split('/', 1)[0]
    def unavailable(reason):
        job.events.append({'event': 'gateway_witness_unavailable', 'reason': reason})
    mimo_endpoints = _mimo_endpoints(env) if provider == 'mimo' else None
    if not gateway_leg(job.model_or_combo, env, mimo_endpoints=mimo_endpoints):
        if provider == 'mimo' and mimo_endpoints[1]:
            unavailable('explicit direct mimo endpoint')
            return
        unavailable('mimo leg not routed through gateway' if provider == 'mimo'
                    else f'not a gateway leg: provider {provider}')
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


def witness_gate(card, role, model, env=None, *, secrets=None):
    """Refuse before launch a review whose card requires a witness this leg cannot give."""
    if role == 'review' and card and card.get('witness_required'):
        require_witness(model, env, secrets=secrets)


def review_child_env():
    """Mirror runner.spawn's review env (writer_launch returns no review extras).

    The fresh run nonce and later session tag do not affect routing evidence.
    Use the runner's default envelope, including headers that trigger rendering.
    """
    from plangraph.routing_support.clients import build_child_env, build_routing_envelope_headers
    return build_child_env({}, headers=build_routing_envelope_headers(
        role='review', risk='normal', min_intelligence=44.0, required_context=32000))


def witness_order(card, role, legs, down):
    """Sort using the same child config and capability predicate as the gate."""
    need = bool(role == 'review' and card and card.get('witness_required'))
    env = review_child_env() if need else None
    return tuple(sorted(legs, key=lambda leg: (
        need and not gateway_leg(leg.model, env), leg.id in down)))


def witness_gate_legs(card, role, legs):
    """Keep only witness-capable fallbacks; refuse only if none can witness."""
    if not (role == 'review' and card and card.get('witness_required')):
        return legs
    from plangraph.planner.routing import SpawnRefused
    env = review_child_env()
    capable = []
    refusals = []
    for leg in legs:
        try:
            witness_gate(card, role, leg.model, env)
        except SpawnRefused as exc:
            refusals.append(str(exc))
        else:
            capable.append(leg)
    if not capable:
        raise SpawnRefused('; '.join(dict.fromkeys(refusals)) or 'witness required: no eligible leg')
    return tuple(capable)
