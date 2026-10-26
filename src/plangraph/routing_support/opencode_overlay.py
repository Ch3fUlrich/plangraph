"""Read-only AutoOS provider overlay; credentials remain environment references."""
import copy
import json
import os
from pathlib import Path
import re
import logging

_parse_warned = False
_content_warned = False
UNKNOWN_PROVIDERS = object()
OVERLAY_UNKNOWN = '_PLANGRAPH_OVERLAY_UNKNOWN'
OVERLAY_ROOT_MISSING = '_PLANGRAPH_ROOT_MISSING'


_missing_warned = False  # Missing roots warn once per process, even after recovery.


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
    return _load_root_providers(root)[0]


def _load_root_providers(root=None):
    global _parse_warned, _missing_warned
    root = root or os.environ.get('AUTOOS_ROOT') or Path.home() / 'code/AutoOS'
    path = Path(root) / 'opencode.jsonc'
    try:
        text = path.read_text(encoding='utf-8')
        text = _strip_jsonc(text)
        providers = json.loads(text).get('providers')
        return (copy.deepcopy(providers) if isinstance(providers, dict) else None), None
    except FileNotFoundError:
        if not _missing_warned:
            logging.getLogger(__name__).warning('root_provider_config_missing path=%s', path)
            _missing_warned = True
        return None, path
    except (OSError, ValueError, TypeError, AttributeError):
        if not _parse_warned:
            logging.getLogger(__name__).warning('root_provider_config_parse_error path=%s', path)
            _parse_warned = True
        return UNKNOWN_PROVIDERS, None


def overlay_env(base_env):
    global _content_warned
    env = dict(base_env)
    env.pop(OVERLAY_ROOT_MISSING, None)
    try:
        existing = json.loads(env.get('OPENCODE_CONFIG_CONTENT', '{}'))
        if not isinstance(existing, dict):
            raise ValueError('not an object')
    except (ValueError, TypeError):
        if not _content_warned:
            logging.getLogger(__name__).warning('caller_provider_config_parse_error')
            _content_warned = True
        env[OVERLAY_UNKNOWN] = '1'
        return env
    providers, missing_path = _load_root_providers()
    if missing_path is not None:
        env[OVERLAY_ROOT_MISSING] = str(missing_path)
    if providers is UNKNOWN_PROVIDERS:
        env[OVERLAY_UNKNOWN] = '1'
        return env
    if providers is None:
        return env
    from .clients import effective_gateway_base_url
    gateway = effective_gateway_base_url(env)

    def merge(root, generated, path=()):
        result = copy.deepcopy(generated)
        for key, value in root.items():
            location = path + (key,)
            if key not in generated:
                result[key] = copy.deepcopy(value)
            elif location == ('headers',):
                result[key] = {**value, **generated[key]} if isinstance(value, dict) and isinstance(generated[key], dict) else value
            elif location == ('env',) and isinstance(value, list) and isinstance(generated[key], list):
                result[key] = list(dict.fromkeys(value + generated[key]))
            elif location in (('settings', 'baseURL'), ('options', 'baseURL')) and gateway and generated[key] == gateway:
                pass  # Only the resolved per-run gateway displaces root URLs.
            elif isinstance(value, dict) and isinstance(generated[key], dict) and location != ('models',):
                result[key] = merge(value, generated[key], location)
            elif location == ('models',) and isinstance(value, dict) and isinstance(generated[key], dict):
                result[key] = {**generated[key], **value}
            else:
                result[key] = copy.deepcopy(value)
        return result
    overlay = existing.get('providers', {})
    if isinstance(overlay, dict):
        for name, value in overlay.items():
            original = providers.get(name)
            if isinstance(original, dict) and isinstance(value, dict):
                providers[name] = merge(original, value)
            else:
                providers[name] = value
        existing['providers'] = providers
    env['OPENCODE_CONFIG_CONTENT'] = json.dumps(existing)
    for name in credential_names(providers):
        if name not in env and name in os.environ:
            env[name] = os.environ[name]
    return env
