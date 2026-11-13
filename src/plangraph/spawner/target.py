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


def admit_daily_budget(leg=None, *, model=None, dry_run=False):
    """Every paid launch/retry asks the Rust gate; unavailable evidence refuses."""
    if dry_run:
        return
    from .resolver import get_catalog_models
    from plangraph.planner.review_order import catalog_tiers, payment_tier
    tiers = set()
    rows = get_catalog_models()
    if leg is not None and not isinstance(leg, Mapping):
        projected, = catalog_tiers((leg,), rows)
        tiers.update((payment_tier(leg, 'review'), payment_tier(projected, 'review')))
    elif isinstance(leg, Mapping):
        tiers.add(leg.get('tier'))
    target = model or (leg.get('model') if isinstance(leg, Mapping) else getattr(leg, 'model', None))
    for row in rows:
        if target == row.get('id') or target in (row.get('combos') or []):
            tiers.add(row.get('tier'))
    if 'paid' not in tiers and tiers.intersection({'trial', 'free', 'subscription'}):
        return
    import subprocess
    try:
        result = subprocess.run([os.environ.get('PLANGRAPH_BIN') or 'plangraph',
                                 'cost-gate', 'admit'], timeout=30,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        raise SpawnRefused('daily cost gate unavailable; paid launch refused') from None
    if result.returncode != 0:
        raise SpawnRefused('daily cost gate refused paid launch')
