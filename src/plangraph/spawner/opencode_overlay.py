"""Read-only AutoOS provider overlay; credentials remain environment references."""
import copy
import json
import os
from pathlib import Path
import re
import logging

_parse_warned = False
UNKNOWN_PROVIDERS = object()
OVERLAY_UNKNOWN = '_PLANGRAPH_OVERLAY_UNKNOWN'


def credential_names(node):
    """Environment references only; never return credential values."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == 'env' and isinstance(value, list):
                yield from (name for name in value if isinstance(name, str))
            else:
                yield from credential_names(value)
    elif isinstance(node, list):
        for value in node:
            yield from credential_names(value)
    elif isinstance(node, str):
        yield from re.findall(r'\{env:([A-Za-z_][A-Za-z0-9_]*)\}', node)


def _strip_jsonc(text):
    out, i, quoted = [], 0, False
    while i < len(text):
        c = text[i]
        if quoted:
            out.append(c)
            if c == '\\' and i + 1 < len(text):
                i += 1
                out.append(text[i])
            elif c == '"':
                quoted = False
        elif c == '"':
            quoted = True
            out.append(c)
        elif text.startswith('//', i):
            end = text.find('\n', i)
            i = len(text) if end < 0 else end
            out.append('\n')
            continue
        elif text.startswith('/*', i):
            end = text.find('*/', i + 2)
            if end < 0:
                raise ValueError('unterminated comment')
            out.append(' ')
            i = end + 2
            continue
        else:
            out.append(c)
        i += 1
    return re.sub(r'("(?:\\.|[^"\\])*")|,\s*(?=[}\]])',
                  lambda m: m.group(1) or '', ''.join(out))


def load_root_providers(root=None):
    global _parse_warned
    root = root or os.environ.get('AUTOOS_ROOT') or Path.home() / 'code/AutoOS'
    path = Path(root) / 'opencode.jsonc'
    try:
        text = path.read_text()
        text = _strip_jsonc(text)
        providers = json.loads(text).get('providers')
        return copy.deepcopy(providers) if isinstance(providers, dict) else None
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, AttributeError):
        if not _parse_warned:
            logging.getLogger(__name__).warning('root_provider_config_parse_error path=%s', path)
            _parse_warned = True
        return UNKNOWN_PROVIDERS


def overlay_env(base_env):
    env = dict(base_env)
    providers = load_root_providers()
    if providers is UNKNOWN_PROVIDERS:
        env[OVERLAY_UNKNOWN] = '1'
        return env
    if providers is None:
        return env
    try:
        existing = json.loads(env.get('OPENCODE_CONFIG_CONTENT', '{}'))
    except (ValueError, TypeError):
        return env
    if not isinstance(existing, dict):
        return env
    overlay = existing.get('providers', {})
    if isinstance(overlay, dict):
        for name, value in overlay.items():
            original = providers.get(name)
            if isinstance(original, dict) and isinstance(value, dict):
                for key, item in value.items():
                    if key in ('package', 'name') and key in original:
                        continue
                    if key == 'env' and isinstance(original.get(key), list) and isinstance(item, list):
                        original[key] = list(dict.fromkeys(original[key] + item))
                        continue
                    if key == 'models' and isinstance(original.get(key), dict) and isinstance(item, dict):
                        original[key] = {**item, **original[key]}
                        continue
                    if isinstance(original.get(key), dict) and isinstance(item, dict):
                        original[key].update(item)
                    else:
                        original[key] = item
            else:
                providers[name] = value
        existing['providers'] = providers
    env['OPENCODE_CONFIG_CONTENT'] = json.dumps(existing)
    for name in credential_names(providers):
        if name not in env and name in os.environ:
            env[name] = os.environ[name]
    return env
