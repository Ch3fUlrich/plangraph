"""Host-local OpenCode evidence, including runtime discovery corrections."""
import hashlib
import json
import os
from pathlib import Path
import socket


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


def unavailable_models(state=None, env=None):
    try:
        return set(json.loads(negative_path(state, env).read_text()))
    except (OSError, ValueError, TypeError):
        return set()


def mark_unavailable(model, *, state=None):
    from plangraph.routing_support.private_io import atomic_write_private
    path = negative_path(state)
    models = unavailable_models(state)
    models.add(model.split('#', 1)[0].lower())
    ensure_private_dir(path.parent)
    atomic_write_private(path, json.dumps(sorted(models)))
    # Remove stale affirmative evidence as well as retaining the runtime denial.
    listing = state_dir(state) / 'model-list-opencode.json'
    try:
        data = json.loads(listing.read_text())
        data['models'] = [m for m in data['models'] if m.split('#', 1)[0].lower() not in models]
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
