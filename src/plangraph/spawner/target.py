"""Resolve explicit spawn targets before admission or provider rendering."""
from plangraph.planner.legs import DEFAULT_LEGS, leg_active
from plangraph.routing_errors import SpawnRefused


def resolve_target(target):
    # Bare Mimo spellings are explicitly refused, even when also a leg id.
    if target.startswith('mimo-'):
        from plangraph.planner.routing import validate_model
        validate_model(target)
    leg = next((l for l in DEFAULT_LEGS if l.id == target or l.model == target), None)
    if leg is None:
        from .resolver import get_catalog_models, catalog_row_admitted
        row = next((r for r in get_catalog_models()
                    if target in (r.get('combos') or []) and catalog_row_admitted(r)), None)
        if row is not None:
            target = row['id']
            leg = next((l for l in DEFAULT_LEGS if l.model == target), None)
    if leg is not None:
        if not leg_active(leg):
            raise SpawnRefused('leg disabled: ' + leg.id + ': ' + (leg.disabled or 'expired'))
        target = leg.model
    return target, leg
