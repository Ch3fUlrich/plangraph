"""Response-side served-model witness from the OmniRoute call log (D-3226).

An OpenCode run on an ``omniroute`` provider sends ``x-omniroute-session-id:
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
import time
import urllib.parse
import urllib.request

from plangraph.routing_errors import SpawnRefused
from plangraph.routing_support.opencode_overlay import _strip_jsonc, prepare_gateway_env
from plangraph.routing_support.opencode_render import (  # management token loader: one implementation
    TOKEN_ENV, TOKEN_FILE_ENV, _owner_only, load_token,
    _NoManagementRedirect, gateway_management_base_url, valid_gateway_endpoint,
)
from .gateway_models import (  # compatibility re-exports for callers
    model_segment, served_family, served_models,
)

SESSION_HEADER = 'x-omniroute-session-id'
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
    from plangraph.routing_support.clients import effective_child_base_url
    value = effective_child_base_url(env)
    return value.rstrip('/') if value else None


def _gateway_mimo(env, mimo_endpoints=None):
    gateway = _gateway(env)
    if not gateway:
        return False
    seen, endpoints = _mimo_endpoints(env) if mimo_endpoints is None else mimo_endpoints
    def normalize(value):
        return gateway_management_base_url(value) if isinstance(value, str) else None
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


def gateway_leg(model, env=None, *, mimo_endpoints=None):
    """Whether the selected provider is routed through the gateway."""
    if env is None:
        from plangraph.routing_support.clients import build_child_env
        env = build_child_env()
    provider = str(model or '').split('/', 1)[0]
    return provider == 'omniroute' or (provider == 'mimo' and _gateway_mimo(env, mimo_endpoints))


def require_witness(model, env=None, *, secrets=None):
    """Fail closed before launching a review whose card requires gateway evidence."""
    env = review_child_env(model) if env is None else env
    secrets = os.environ if secrets is None else secrets
    provider = str(model or '').split('/', 1)[0]
    reason = None if gateway_leg(model, env) else (
        'mimo leg not routed through gateway' if provider == 'mimo'
        else f'not a gateway leg: provider {provider}')
    try:
        if reason:
            raise LookupError(reason)
        child, parent = _gateway(env), _gateway(secrets)
        if not child or not parent:
            raise LookupError('gateway endpoint not configured')
        if gateway_management_base_url(child) != gateway_management_base_url(parent):
            raise LookupError('child gateway endpoint differs from witness endpoint')
        if not valid_gateway_endpoint(parent):
            raise LookupError('gateway endpoint must use HTTP(S) with a host and HTTPS outside loopback')
        load_token(secrets)
    except LookupError as exc:
        raise SpawnRefused('witness required: ' + str(exc)) from None


def fetch_rows(tag, env, *, timeout=5, opener=None, secrets=None):
    """Call-log rows for ``tag``; raises LookupError naming why they are unreadable.

    Both URL and token come from the spawner's own env (``secrets``), never
    the child's. ``build_child_env`` drops the token.
    """
    query = urllib.parse.urlencode({'sessionTag': tag, 'limit': 200})
    return _fetch_log('?' + query, timeout, opener, secrets)


def _fetch_log(suffix, timeout, opener=None, secrets=None):
    secrets = os.environ if secrets is None else secrets
    base = _gateway(secrets)
    if not base:
        raise LookupError('gateway endpoint not configured')
    if not valid_gateway_endpoint(base):
        raise LookupError('gateway endpoint must use HTTP(S) with a host and HTTPS outside loopback')
    token = load_token(secrets)
    base = gateway_management_base_url(base)
    try:
        request = urllib.request.Request(f'{base}/api/usage/call-logs{suffix}',
                                         headers={'Authorization': 'Bearer ' + token})
        if opener is None:
            opener = urllib.request.build_opener(_NoManagementRedirect()).open
        with opener(request, timeout=timeout) as response:
            return json.loads(response.read().decode('utf-8'))
    except (OSError, ValueError) as exc:
        raise LookupError('call log unreadable: ' + type(exc).__name__) from None


QUOTA_POLL_IDLE_S = 60.0   # a gateway leg silent this long is checked against the call log
QUOTA_POLL_EVERY_S = 30.0
QUOTA_POLL_TIMEOUT_S = 3.0


def _latest_row(rows, tag):
    from plangraph.routing_support.quota_snapshots import _ts

    def order(pair):
        index, row = pair
        at = _ts(row.get('createdAt') or row.get('created_at') or row.get('timestamp')) or 0
        try:
            ident = int(row.get('id') or 0)
        except (TypeError, ValueError):
            ident = 0
        return at, ident, index

    matching = ((i, r) for i, r in enumerate(rows if isinstance(rows, list) else ())
                if isinstance(r, dict) and r.get('sessionTag') == tag)
    return max(matching, key=order, default=(0, None))[1]


def _finish_reason(row):
    reason = row.get('finishReason') or row.get('finish_reason') or row.get('stop_reason')
    return reason if isinstance(reason, str) else None


def _finish_from_log(row, tag, model, fetch_detail):
    if not row or served_models([row], tag) != {model}:
        return None
    finish = _finish_reason(row)
    ident = row.get('id')
    if finish or not fetch_detail or not isinstance(ident, (int, str)) or str(ident) in ('', '.', '..'):
        return finish
    try:
        detail = fetch_detail('/' + urllib.parse.quote(str(ident), safe=''), 5)
    except LookupError:
        return None
    if not isinstance(detail, dict) or detail.get('id') != ident or served_models([detail], tag) != {model}:
        return None
    body = detail.get('responseBody')
    if not isinstance(body, dict):
        return None
    choices = body.get('choices')
    if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict):
        return _finish_reason(choices[0])
    return _finish_reason(body)


def quota_refusal(rows, tag):
    """Only the newest request of a session can refuse it; unversioned rows use input order."""
    from .env_redact import early_refusal
    row = _latest_row(rows, tag)
    if not row:
        return None
    try:
        status = int(row.get('status'))
    except (TypeError, ValueError):
        status = 0
    if 200 <= status < 300:
        return None
    line = ' '.join(' '.join(str(row.get(k) or '') for k in
                    ('error_summary', 'errorSummary', 'error')).split())[:300]
    found = early_refusal(line) or early_refusal('Error: ' + line)
    return found or (('HTTP 429: ' + line if line else 'HTTP 429') if status == 429 else None)



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
        self.model = getattr(job, 'model_or_combo', None)
        self.wake = gateway_leg(self.model, env)   # wake cadence only; check() re-reads the env
        self.last = 0.0

    def delay(self, delay):
        return min(delay, QUOTA_POLL_EVERY_S) if self.wake else delay

    def check(self, job_id, job, env, refused):
        now = time.time()
        if (refused.is_set() or now - self.last < QUOTA_POLL_EVERY_S
                or now - max(getattr(job, 'last_activity_ts', 0) or 0, getattr(job, 'start_time', now) or now) < QUOTA_POLL_IDLE_S
                or not gateway_leg(self.model, env)):
            return False
        self.last = now
        return True

    async def refusal(self, job_id, job, env, refused):
        """Refusal text that ends the run, else None; a failing check never ends or fails the run."""
        try:
            return await self.poll(job_id, job, env) if self.check(job_id, job, env, refused) else None
        except Exception:
            return None

    async def poll(self, job_id, job, env):
        """Refusal text, or None; bounded by QUOTA_POLL_TIMEOUT_S so a slow gateway never stalls the watchdog loop."""
        import asyncio
        try:
            return await asyncio.wait_for(asyncio.to_thread(poll_quota, job_id, job, env), QUOTA_POLL_TIMEOUT_S)
        except Exception:
            return None


def witness(runner, job_id, job, env, *, attempts=3, delay=1.0, fetch=None, fetch_detail=None):
    """Record the gateway-served model on ``job`` through the runner's adapter check."""
    if fetch is None and fetch_detail is None:
        fetch_detail = _fetch_log
    fetch = fetch or fetch_rows
    # Only a leg served through the gateway can have rows: build_child_env adds an
    # omniroute block to every config, so the tag alone does not mean a gateway leg.
    provider = str(getattr(job, 'model_or_combo', '') or '').split('/', 1)[0]
    def unavailable(reason):
        if provider in ('mimo', 'omniroute'):
            job.served_witness_reason = reason.replace('no call log row', 'no call-log row')
        job.events.append({'event': 'gateway_witness_unavailable', 'reason': reason})
    mimo_endpoints = _mimo_endpoints(env) if provider == 'mimo' else None
    if not gateway_leg(job.model_or_combo, env, mimo_endpoints=mimo_endpoints):
        if provider == 'mimo' and not _gateway(env):
            unavailable('gateway endpoint not configured')
            return
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
            rows = fetch(tag, env)
            served = served_models(rows, tag)
        except LookupError as exc:
            reason = str(exc)
            break
        if len(served) > 1:
            reason = 'ambiguous: call log rows name %d served models' % len(served)
            break
        model = served.pop() if served else None
        if model:
            try:
                finish = (_finish_from_log(_latest_row(rows, tag), tag, model, fetch_detail)
                          if getattr(job, 'role', None) == 'review' else None)
                runner.served_witness(job_id, model, adapter_token=runner._adapter_tokens.get(job_id),
                                      finish_reason=finish)
            except ValueError as exc:
                reason = str(exc)
                break
            if model_segment(model) != model_segment(job.model_or_combo):
                job.events.append(dict(event='served_model_mismatch',
                                       requested_model=job.model_or_combo, served_model=model))
            job.witness_kind = WITNESS_KIND
            job.served_witness_reason = 'gateway call log (sessionTag) response-side model'
            return
        if attempt + 1 < attempts:
            time.sleep(delay)
    unavailable(reason[:200])


def witness_gate(card, role, model, env=None, *, secrets=None):
    """Refuse before launch a review whose card requires a witness this leg cannot give."""
    if role == 'review' and card and card.get('witness_required'):
        require_witness(model, env, secrets=secrets)


def review_child_env(model=None):
    """Mirror runner.spawn's review env (writer_launch returns no review extras).

    The fresh run nonce and later session tag do not affect routing evidence.
    Use the runner's default envelope, including headers that trigger rendering.
    """
    from plangraph.routing_support.clients import build_child_env, build_routing_envelope_headers
    env = build_child_env({}, headers=build_routing_envelope_headers(
        role='review', risk='normal', min_intelligence=44.0, required_context=32000))
    if isinstance(model, str) and model.startswith(('mimo/', 'omniroute/')):
        env, refusal = prepare_gateway_env(env, model)
        if refusal:
            raise SpawnRefused('witness required: ' + refusal)
    return env


def witness_order(card, role, legs, down):
    """Sort using the same child config and capability predicate as the gate."""
    if not (role == 'review' and card and card.get('witness_required')):
        return tuple(sorted(legs, key=lambda leg: leg.id in down))

    def priority(leg):
        try:
            capable = gateway_leg(leg.model, review_child_env(leg.model))
        except SpawnRefused:
            capable = False
        return not capable, leg.id in down

    return tuple(sorted(legs, key=priority))


def witness_gate_legs(card, role, legs):
    """Keep only witness-capable fallbacks; refuse only if none can witness."""
    if not (role == 'review' and card and card.get('witness_required')):
        return legs
    capable = []
    refusals = []
    for leg in legs:
        try:
            witness_gate(card, role, leg.model)
        except SpawnRefused as exc:
            refusals.append(str(exc))
        else:
            capable.append(leg)
    if not capable:
        raise SpawnRefused('; '.join(dict.fromkeys(refusals)) or 'witness required: no eligible leg')
    return tuple(capable)
