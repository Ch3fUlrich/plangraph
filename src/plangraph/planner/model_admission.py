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
    import re
    lines = [re.sub(r'\x1b\[[0-9;]*m', '', line).strip() for line in result.stdout.splitlines()]
    return {line.split()[0] for line in lines if line and
            re.fullmatch(r'[\w./:#-]+', line.split()[0])}


def cached_models(source, loader, *, state=None, clock=time.time, strict=False):
    from plangraph.routing_support.private_io import atomic_write_private
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
        if 0 <= now - data['time'] < ttl:
            return set(data['models'])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        models = set(loader())
    except Exception as exc:
        if strict:
            raise OSError(source + ' unreachable') from exc
        return set()
    try:
        state.mkdir(parents=True, exist_ok=True)
        atomic_write_private(path, json.dumps({'time': now, 'models': sorted(models)}))
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
        if model in listed:
            return model
    if unreachable:
        raise SpawnRefused('could not verify model: ' + ', '.join(unreachable) + ' unreachable: ' + model)
    raise SpawnRefused('unknown model (not in catalog, gateway list or opencode models): ' + model)


def safe_list(loader):
    try:
        return set(loader())
    except Exception:
        return set()
