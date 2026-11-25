"""Read-only plangraph provider-file overlay; credentials remain environment references."""
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
OVERLAY_ROOT_BROKEN = '_PLANGRAPH_ROOT_BROKEN'


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


def load_root_providers(path=None):
    return _load_root_providers(path)[0]


def _load_root_providers(path=None):
    global _parse_warned, _missing_warned
    if path is None:
        path = os.environ.get('PLANGRAPH_PROVIDERS_FILE') or (
            Path(__file__).resolve().parents[3] / 'configuration/opencode/providers.json')
    path = Path(path)
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
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        if not _parse_warned:
            key = 'unreadable' if isinstance(exc, OSError) else 'parse_error'
            logging.getLogger(__name__).warning('root_provider_config_%s path=%s', key, path)
            _parse_warned = True
        return UNKNOWN_PROVIDERS, path


def overlay_env(base_env):
    global _content_warned
    env = dict(base_env)
    env.pop(OVERLAY_ROOT_MISSING, None)
    env.pop(OVERLAY_ROOT_BROKEN, None)
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
    providers, root_path = _load_root_providers()
    if root_path is not None:
        marker = OVERLAY_ROOT_BROKEN if providers is UNKNOWN_PROVIDERS else OVERLAY_ROOT_MISSING
        env[marker] = str(root_path)
    if providers is UNKNOWN_PROVIDERS:
        env[OVERLAY_UNKNOWN] = '1'
        return env
    if providers is None:
        return env
    from plangraph.review.gateway import GATEWAY_ENV_VAR
    from .clients import effective_gateway_base_url
    gateway = effective_gateway_base_url(env)
    child = existing.get('providers', {})
    child = child.get('omniroute', {}) if isinstance(child, dict) else {}
    settings = child.get('settings') if isinstance(child, dict) else None
    child_gateway = settings.get('baseURL') if isinstance(settings, dict) else None

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
            elif (location in (('settings', 'baseURL'), ('options', 'baseURL'))
                  and value == '{env:' + GATEWAY_ENV_VAR + '}'
                  and child_gateway and generated[key] == child_gateway):
                pass  # A config-only child gateway replaces a root placeholder, not a concrete URL.
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


def prepare_gateway_env(base_env, model, *, validate=None):
    """Apply the launch overlay and render the selected model's provider."""
    from .opencode_render import render_gateway_providers
    env = overlay_env(base_env)
    if validate is not None:
        validate(env)
    return env, render_gateway_providers(env, model)


#: No child-side ``settings`` node exists to honour: no ``OPENCODE_CONFIG_CONTENT``,
#: unparsable content, or a provider block ``build_child_env`` builds from scratch.
_ABSENT = object()


def _child_settings(env):
    """The ``providers.omniroute.settings`` node the child's config already holds.

    Whatever JSON value sits under ``settings`` is returned as-is: a ``dict`` is
    what ``build_child_env`` merges into with ``setdefault``, any other value
    blocks the ``baseURL`` write outright.  ``_ABSENT`` means there is nothing
    to honour yet, so the deployment-config sources decide instead.
    """
    raw = (env or {}).get("OPENCODE_CONFIG_CONTENT")
    if not isinstance(raw, str) or not raw.strip():
        return _ABSENT
    try:
        parsed = json.loads(raw)
    except ValueError:
        return _ABSENT
    node = parsed
    for key in ("providers", "omniroute", "settings"):
        if not isinstance(node, dict) or key not in node:
            return _ABSENT
        node = node[key]
    return node


def catalog_output_maximum(model):
    """The catalog's maximum output tokens for ``model``, or None when absent or not positive."""
    from plangraph.catalog_data import SEED_MODELS
    maximum = next((row.get('max_output_tokens') for row in SEED_MODELS if row.get('model_id') == model), None)
    return maximum if type(maximum) is int and maximum > 0 else None


def launch_output_reserve(model, registry_output):
    """Output tokens the launched child reserves: ``cap_reasoning`` writes the catalog maximum for a MiMo id.

    The one transformation of a registry ``limit.output`` that launch applies; request bounds use it so the
    planner, the launch overlay and the child config agree on the reserve.
    """
    from plangraph.mimo_policy import is_mimo_id
    maximum = catalog_output_maximum(model) if is_mimo_id(model) else None
    return maximum if maximum is not None else registry_output


class RequestBoundError(ValueError):
    """A registry entry that names a limit no request can honour (not a positive context minus reserve)."""


class ProviderView:
    """One consistent read of the providers the launch overlay hands a child (a request-bound snapshot).

    ``providers`` is None when no registry is readable (file absent or unparsable); every bound is then
    the compatibility value its leg declares. Only the providers node is kept.
    """

    __slots__ = ("providers",)

    def __init__(self, base_env=None, *, overlaid=False):
        base_env = os.environ if base_env is None else base_env
        env = base_env if overlaid else overlay_env(base_env)
        providers = None
        if OVERLAY_UNKNOWN not in env:
            try:
                node = json.loads(env.get('OPENCODE_CONFIG_CONTENT') or '{}').get('providers')
            except (ValueError, TypeError, AttributeError):
                node = None
            providers = node if isinstance(node, dict) else None
        self.providers = providers

    def request_bound(self, model):
        """Largest request the registry lets ``model`` take, or None for a compatibility case.

        None: no registry, no entry, no limit, or a limit the renderer synthesises (a cx effort variant
        whose base entry declares none).  A supported model has a positive context minus its effective
        reserved output (``launch_output_reserve``: the catalog maximum for MiMo, else ``limit.output``).
        Raises ``RequestBoundError`` for an entry whose limit is malformed.
        """
        from .opencode_render import _declare_cx_effort_model
        model = model.split('#', 1)[0].strip() if isinstance(model, str) else ''
        provider, _, key = model.partition('/')
        block = (self.providers or {}).get(provider)
        models = block.get('models') if isinstance(block, dict) else None
        if not key or not isinstance(models, dict):
            return None
        entry = models.get(key)
        if entry is None and provider == 'omniroute':
            scratch = {'omniroute': {'models': dict(models)}}
            _declare_cx_effort_model(scratch, model)    # the renderer's effort-variant rule, not a copy of it
            entry = scratch['omniroute']['models'].get(key)
            base = models.get(key.rpartition('-')[0])
            if entry is not None and not (isinstance(base, dict) and isinstance(base.get('limit'), dict)):
                return None                             # renderer default: compatibility, not a measured window
        if entry is None:
            return None
        if not isinstance(entry, dict):
            raise RequestBoundError(f'{model}: entry is not an object')
        limit = entry.get('limit')
        if limit is None:
            return None
        if not isinstance(limit, dict):
            raise RequestBoundError(f'{model}: limit is not an object')
        if 'context' not in limit:
            return None
        context, output = limit['context'], limit.get('output', 0)
        for name, value in (('context', context), ('output', output)):
            if type(value) is not int or value < 0:
                raise RequestBoundError(f'{model}: limit.{name} is not a non-negative integer')
        bound = context - launch_output_reserve(model, output)
        if bound <= 0:
            raise RequestBoundError(f'{model}: context {context} leaves no room after its output reserve')
        return bound
