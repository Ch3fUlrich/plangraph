"""Resolve explicit spawn targets before admission or provider rendering."""
from plangraph.planner.legs import leg_active
from plangraph.routing_errors import SpawnRefused


def admit_leg(leg):
    """An override cannot enable a disabled leg, regardless of override_reason."""
    if leg is not None and not leg_active(leg):
        raise SpawnRefused('leg disabled: ' + leg.id + ': ' + (leg.disabled or 'expired')
                           + '; override cannot enable a disabled leg')


def resolve_target(target, *, chosen=None):
    """Resolve and admit a target; override cannot enable a disabled leg."""
    from plangraph.planner.legs import EXPLICIT_LEGS, spawnable_legs
    SPAWNABLE_LEGS = spawnable_legs()   # call time: callers rebind DEFAULT_LEGS
    if chosen is not None and target != chosen.model:
        raise SpawnRefused('model and leg disagree')
    admit_leg(chosen)
    # Bare Mimo spellings are explicitly refused, even when also a leg id.
    if target.startswith('mimo-'):
        from plangraph.planner.routing import validate_model
        validate_model(target)
    leg = chosen or next((l for l in SPAWNABLE_LEGS if l.id == target or l.model == target), None)
    if leg is None and target.startswith('agy/'):
        # The catalog spells agy models "agy/<model>"; an agy leg holds the bare
        # model the agy CLI takes (leg_probe/measured_down add the same prefix).
        # Prefer the explicit effort leg even if a routed leg also serves review.
        same = [l for l in SPAWNABLE_LEGS if l.harness == 'agy' and l.model == target[len('agy/'):]]
        leg = next((l for l in same if l in EXPLICIT_LEGS), same[0] if same else None)
    if leg is None:
        from .resolver import get_catalog_models, catalog_row_admitted
        row = next((r for r in get_catalog_models()
                    if target in (r.get('combos') or []) and catalog_row_admitted(r)), None)
        if row is not None:
            target = row['id']
            leg = next((l for l in SPAWNABLE_LEGS if l.model == target), None)
    if leg is not None:
        admit_leg(leg)
        target = leg.model
    return target, leg
