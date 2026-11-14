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


def admit_effort(model, card, effort=None):
    """Elevated declared effort needs explicit authorization; names are not effort."""
    for selected in (effort, card.get('effort')):
        if selected in ('high', 'xhigh', 'max') and not (
                card.get('effort') == selected and
                re.fullmatch(r'D-[0-9]+', str(card.get('effort_decision', '')))):
            raise SpawnRefused('higher effort requires explicit card effort and D-id (effort_decision)')


def skip_effort(record, leg, card):
    """Persist a policy skip without preventing later routed legs from launching."""
    from dataclasses import asdict
    try:
        admit_effort(leg.model, card, leg.effort)
    except SpawnRefused as exc:
        record['attempts'].append(dict(leg=asdict(leg), status='skipped',
            skip_reason='effort_not_allowed: ' + str(exc)))
        return True
    return False


def refuse_effort_exhausted(record):
    attempts = record['attempts']
    if attempts and all(a.get('status') == 'skipped' for a in attempts) and any(
            a.get('skip_reason', '').startswith('effort_not_allowed:') for a in attempts):
        raise SpawnRefused('; '.join(a.get('skip_reason', '') for a in attempts))
