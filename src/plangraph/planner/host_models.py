"""Host-local OpenCode evidence, including runtime discovery corrections."""
import hashlib
import json
import os
from pathlib import Path
import socket
import time

#: A runtime denial is a correction for now, not forever (D-3258).
DENIED_TTL_S = 3600.0


def host_id():
    return socket.gethostname()


def state_dir(state=None, env=None):
    env = os.environ if env is None else env
    return Path(state or env.get('PLANGRAPH_SPAWN_STATE') or
                Path(env.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'plangraph/spawn')


def ensure_private_dir(path):
    """State holds host evidence: created 0o700 on first write (D-3258)."""
    path = Path(path)
    if not path.is_dir():
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    return path


def negative_path(state=None, env=None):
    suffix = hashlib.sha256(host_id().encode()).hexdigest()[:16]
    return state_dir(state, env) / ('host-unavailable-' + suffix + '.json')


def _denied_ttl(env):
    env = os.environ if env is None else env
    try:
        return max(0.0, float(env.get('PLANGRAPH_DENIED_TTL_S', DENIED_TTL_S)))
    except ValueError:
        return DENIED_TTL_S


def _denied_records(state=None, env=None):
    """Live denials as ``{model: recorded_at}``; stale ones expire silently."""
    try:
        data = json.loads(negative_path(state, env).read_text())
    except (OSError, ValueError, TypeError):
        return {}
    entries = data.get('models') if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return {}  # legacy un-timestamped denials carry no evidence window
    now, ttl = time.time(), _denied_ttl(env)
    return {name: recorded for name, recorded in entries.items()
            if isinstance(recorded, (int, float)) and 0 <= now - recorded < ttl}


def unavailable_models(state=None, env=None):
    """Models this host runtime-denied, still inside their TTL window."""
    return set(_denied_records(state, env))


def mark_unavailable(model, *, state=None):
    from plangraph.routing_support.private_io import atomic_write_private
    path = negative_path(state)
    records = _denied_records(state)
    records[model.split('#', 1)[0].lower()] = time.time()
    ensure_private_dir(path.parent)
    atomic_write_private(path, json.dumps({'models': dict(sorted(records.items()))}))
    # Remove stale affirmative evidence as well as retaining the runtime denial.
    listing = state_dir(state) / 'model-list-opencode.json'
    try:
        data = json.loads(listing.read_text())
        data['models'] = [m for m in data['models'] if m.split('#', 1)[0].lower() not in records]
        atomic_write_private(listing, json.dumps(data))
    except (OSError, ValueError, KeyError, TypeError):
        pass


def is_gateway(model, child):
    from plangraph.routing_support.opencode_render import GATEWAY_KEY_NAME
    provider = model.split('/', 1)[0]
    if provider in ('omniroute', 'mimo'):
        return True
    try:
        block = json.loads(child.get('OPENCODE_CONFIG_CONTENT', '{}')).get('providers', {}).get(provider, {})
        return GATEWAY_KEY_NAME in (block.get('env') or [])
    except (ValueError, TypeError, AttributeError):
        return False


def runtime_unavailable(leg, state):
    if leg.harness == 'opencode':
        mark_unavailable(leg.model, state=state)
