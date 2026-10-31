"""Explicit overrides may use host- or gateway-listed models (D-3259)."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from urllib.request import Request, urlopen

from plangraph.routing_errors import SpawnRefused


def gateway_models():
    from plangraph.routing_support.clients import effective_gateway_base_url
    from plangraph.routing_support.host_keys import add_host_keys, GATEWAY_KEY_ENV
    env = dict(os.environ)
    base = effective_gateway_base_url(env)
    if not base:
        raise OSError('gateway unreachable: no base URL')
    add_host_keys(env, 'omniroute/models', needs_gateway=True)
    url = base.rstrip('/')
    url += '/models' if url.endswith('/v1') else '/v1/models'
    headers = {'Authorization': 'Bearer ' + env[GATEWAY_KEY_ENV]} if env.get(GATEWAY_KEY_ENV) else {}
    with urlopen(Request(url, headers=headers), timeout=20) as response:
        return {row['id'] for row in json.load(response)['data'] if isinstance(row.get('id'), str)}


def opencode_models():
    exe = shutil.which('opencode')
    if not exe:
        raise OSError('opencode unreachable: executable missing')
    if os.name == 'nt' and Path(exe).suffix.lower() in ('.cmd', '.bat'):
        sibling = Path(exe).parent / 'node_modules/@opencode/cli/bin/opencode.exe'
        if sibling.is_file():
            exe = str(sibling)
    result = subprocess.run([exe, 'models'], capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=20, check=True)
    from .opencode_listing import parse_opencode_models
    models = parse_opencode_models(result.stdout)
    if result.stdout.strip() and not models:
        # Format drift is an outage, not an empty host: refuse to cache it.
        raise OSError('opencode models output unrecognised')
    return models


def listing_serves(model, listing):
    """Gateway-path listing match: case-insensitive and #variant-aware (D-3264).

    A bare request matches any listed variant of its base; a ``#variant``
    request matches that variant, or the bare base when no variant is listed.
    """
    base = str(model).lower().split('#', 1)[0]
    lowered = {str(m).lower() for m in listing}
    variants = {m for m in lowered if m.startswith(base + '#')}
    return (base in {m.split('#', 1)[0] for m in lowered}
            and ('#' not in model or not variants or str(model).lower() in variants))


def console_host_skip(model, probe, env, state=None):
    """Console providers require positive host evidence, including on probe failure."""
    from plangraph.routing_support.resolver import host_probe_env
    from .host_models import unavailable_models, state_dir
    if model.lower().split('#', 1)[0] in unavailable_models(state=state, env=env):
        return 'not_served_on_host: ' + model
    try:
        models = probe(host_probe_env(env)) if probe else cached_models('opencode', opencode_models, state=state_dir(state, env))
    except Exception:
        models = None
    served = models is not None and listing_serves(model, models)
    return None if served else 'not_served_on_host: ' + model


_FAILED_LOADS = {}
_FAILURE_BACKOFF_S = 30.0


def cached_models(source, loader, *, state=None, clock=time.time, strict=False):
    from plangraph.routing_support.private_io import atomic_write_private
    from .host_models import host_id
    state = Path(state or os.environ.get('PLANGRAPH_SPAWN_STATE') or
                 Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'plangraph/spawn')
    path = state / ('model-list-' + source + '.json')
    now = clock()
    try:
        ttl = max(0, float(os.environ.get('PLANGRAPH_MODEL_LIST_TTL_S', '600')))
    except ValueError:
        ttl = 600
    try:
        data = json.loads(path.read_text())
        if data.get('host') == host_id() and 0 <= now - data['time'] < ttl:
            return set(data['models'])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    # A failing loader backs off instead of re-running per leg (D-3264).
    failed_at = _FAILED_LOADS.get((source, str(path)))
    if failed_at is not None and 0 <= now - failed_at < _FAILURE_BACKOFF_S:
        if strict:
            raise OSError(source + ' unreachable')
        return set()
    try:
        models = set(loader())
    except Exception as exc:
        _FAILED_LOADS[(source, str(path))] = now
        if strict:
            raise OSError(source + ' unreachable') from exc
        return set()
    _FAILED_LOADS.pop((source, str(path)), None)
    try:
        from .host_models import ensure_private_dir
        ensure_private_dir(state)
        atomic_write_private(path, json.dumps({'host': host_id(), 'time': now, 'models': sorted(models)}))
    except OSError:
        pass
    return models


def validate_model(model, *, gateway_list=None, opencode_list=None, state=None):
    from plangraph.routing_support.resolver import get_catalog_models, catalog_row_admitted, OPERATOR_REMOVED_MODELS
    from plangraph.mimo_policy import check_mimo_path
    from .legs import DEFAULT_LEGS
    if not isinstance(model, str) or not model:
        raise SpawnRefused('model must be a nonempty string')
    check_mimo_path(model)
    rows = get_catalog_models()
    inactive = {row['id'] for row in rows if not catalog_row_admitted(row)}
    unreachable = []
    def lookup(source, explicit, loader):
        try:
            if explicit is not None:
                return set(explicit() if callable(explicit) else explicit)
            return cached_models(source, loader, state=state, strict=True)
        except Exception:
            unreachable.append(source)
            return set()
    if model not in inactive | OPERATOR_REMOVED_MODELS:
        if model in {row['id'] for row in rows if catalog_row_admitted(row)} | {leg.model for leg in DEFAULT_LEGS}:
            return model
        if model.startswith('omniroute/'):
            listed = lookup('gateway', gateway_list, gateway_models)
            if model.partition('/')[2] in listed:
                return model
        listed = lookup('opencode', opencode_list, opencode_models)
        if listing_serves(model, listed):
            return model
    if unreachable:
        raise SpawnRefused('could not verify model: ' + ', '.join(unreachable) + ' unreachable: ' + model)
    raise SpawnRefused('unknown model (not in catalog, gateway list or opencode models): ' + model)


def safe_list(loader):
    try:
        return set(loader())
    except Exception:
        return set()
