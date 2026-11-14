"""Operator retirement enforcement at request and model-list boundaries."""
import re
from collections.abc import Mapping
from plangraph.routing_errors import SpawnRefused


class RetiredModelError(SpawnRefused, ValueError):
    """A request names an operator-retired model."""


def is_retired(value):
    return isinstance(value, str) and bool(re.search(r'gemini[^\s]*flash', value, re.I))


def check_retired(value):
    """Inspect target fields only; prompts and historical records remain readable."""
    if isinstance(value, Mapping):
        for key in ('id', 'model', 'model_id', 'upstream_id', 'leg', 'model_or_combo',
                    'override', 'overrides', 'model_override', 'leg_override', 'chosen_leg', 'legs'):
            if key in value:
                check_retired(value[key])
    elif isinstance(value, (list, tuple)):
        for item in value:
            check_retired(item)
    elif is_retired(value):
        exc = RetiredModelError('gemini_flash_retired: model retired by operator order '
                                '2026-11-14; use agy-gemini-4-hydrogen (medium: '
                                'agy-gemini-4-hydrogen-medium)')
        exc.reason = exc.code = 'gemini_flash_retired'
        raise exc


def visible_model(row):
    return not any(is_retired(row.get(key)) for key in
                   ('id', 'model', 'model_id', 'upstream_id'))


def check_route_targets(card, legs):
    check_retired(card)
    for leg in legs:
        check_retired(dict(id=getattr(leg, "id", None), model=getattr(leg, "model", None)))
