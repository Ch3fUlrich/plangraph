"""Read-only AutoOS provider overlay; credentials remain environment references."""
import copy
import json
import os
from pathlib import Path
import re
import logging

_parse_warned = False


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
    try:
        root = root or os.environ.get('AUTOOS_ROOT') or Path.home() / 'code/AutoOS'
        text = (Path(root) / 'opencode.jsonc').read_text()
        text = _strip_jsonc(text)
        providers = json.loads(text).get('providers')
        return copy.deepcopy(providers) if isinstance(providers, dict) else None
    except OSError:
        return None
    except (ValueError, TypeError, AttributeError):
        if not _parse_warned:
            logging.getLogger(__name__).warning('root_provider_config_parse_error')
            _parse_warned = True
        return None


def overlay_env(base_env):
    env = dict(base_env)
    providers = load_root_providers()
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
                    if key in ('package', 'name', 'env', 'models') and key in original:
                        continue
                    if isinstance(original.get(key), dict) and isinstance(item, dict):
                        original[key].update(item)
                    else:
                        original[key] = item
            else:
                providers[name] = value
        existing['providers'] = providers
    env['OPENCODE_CONFIG_CONTENT'] = json.dumps(existing)
    def names(node):
        if isinstance(node, dict):
            for value in node.values():
                yield from names(value)
        elif isinstance(node, list):
            for value in node:
                yield from names(value)
        elif isinstance(node, str):
            if re.fullmatch(r'[A-Z][A-Z0-9_]+', node):
                yield node
            yield from re.findall(r'\{env:([A-Za-z_][A-Za-z0-9_]*)\}', node)
    for name in names(providers):
        if name not in env and name in os.environ:
            env[name] = os.environ[name]
    return env
