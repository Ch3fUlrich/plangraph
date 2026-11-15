"""Pure model identity admission and response-side call-log row parsing."""
from __future__ import annotations

import types

# Rows come from the gateway's own call log, so xiaomi-mimo is its connection, never a direct leg.
PROVIDER_ALIASES = types.MappingProxyType({'xiaomi-mimo': 'mimo'})


def model_segment(model):
    """Provider-independent identity; cx- is the route table's GPT alias prefix."""
    segment = str(model or '').rsplit('/', 1)[-1]
    return segment.removeprefix('cx-') if segment.startswith('cx-gpt-') else segment


def served_family(model):
    """Admit exact route-table model segments, never substrings in provider names."""
    from plangraph.planner.legs import DEFAULT_LEGS, EXPLICIT_LEGS
    families = {leg.family for leg in (*DEFAULT_LEGS, *EXPLICIT_LEGS)
                if model_segment(leg.model) == model_segment(model)}
    if len(families) == 1:
        family = families.pop()
        return 'gpt' if family == 'openai' else family
    return None


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
