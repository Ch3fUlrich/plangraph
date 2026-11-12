"""Resolve explicit spawn targets before admission or provider rendering."""
from collections.abc import Mapping
import os
from pathlib import Path
import re

from plangraph.planner.legs import leg_active
from plangraph.routing_errors import SpawnRefused

QUOTA_EXEMPT_ROLES = frozenset({'basic-analysis'})


def quota_paused(state=None, role=None, leg=None):
    """The usage owner creates quota-pause at USAGE >=95%, unlinks at reset.

    Presence alone is authoritative: no second meter, expiry, or auto-clear.
    Accept a bare role or host/level-prefixed session name with a numeric counter.
    Unknown leg providers cannot bypass a pause.
    """
    role = re.sub(r'-[0-9]+$', '', role or '')
    if any(role == exempt or role.endswith('-' + exempt) for exempt in QUOTA_EXEMPT_ROLES):
        return False
    if leg is not None:
        values = [leg.get(k) if isinstance(leg, Mapping) else getattr(leg, k, None)
                  for k in ('id', 'model')]
        targets = [value for value in values if isinstance(value, str) and value]
        if targets and not any(value.startswith('claude-') for value in targets):
            return False
    if not state:
        state = os.environ.get('PLANGRAPH_SPAWN_STATE') or (
            Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 'plangraph')
    try:
        os.lstat(Path(state) / 'quota-pause')  # Unlike lexists, preserves read errors.
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True
    return True


def admit_leg(leg):
    """An override cannot enable a disabled leg, regardless of override_reason."""
    if leg is not None and not leg_active(leg):
        raise SpawnRefused('leg disabled: ' + leg.id + ': ' + (leg.disabled or 'expired')
                           + '; override cannot enable a disabled leg')


def paid_pin_reason(available, role, override_reason, down):
    """A paid writer pin cannot bypass subscriptions; free legs need an audit."""
    subscriptions = [leg.id for leg in available if leg.tier == 'subscription']
    if role == 'implement' and subscriptions:
        raise SpawnRefused('paid writer pin refused; subscription writers available: '
                           + ', '.join(subscriptions))
    free = [leg.id for leg in available if leg.tier == 'free' and leg.id not in down]
    if override_reason is None and not free:
        override_reason = 'No eligible free leg is up on this host'
    if not isinstance(override_reason, str) or len(override_reason.strip()) < 10:
        raise SpawnRefused('paid pin refused; free legs up: ' + ', '.join(free)
                           + '; --override-reason required (D-4382: no free leg witnesses is a valid reason)')
    return override_reason.strip()


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
        # An orchestrate-only alias must not shadow a routed seat for that model.
        same = [l for l in SPAWNABLE_LEGS if l.harness == 'agy'
                and l.model == target[len('agy/'):] and l.roles != ('orchestrate',)]
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
