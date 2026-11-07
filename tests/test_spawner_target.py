"""Explicit target/leg consistency must precede leg admission (D-4520)."""
from dataclasses import replace

import pytest

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.routing_errors import SpawnRefused
from plangraph.spawner.target import resolve_target


@pytest.mark.parametrize('disabled', [None, 'test policy'])
def test_mismatched_target_refused_before_leg_admission(disabled):
    chosen = replace(DEFAULT_LEGS[0], disabled=disabled)
    target = next(leg.model for leg in DEFAULT_LEGS
                  if leg.model != chosen.model and leg.disabled is None)
    with pytest.raises(SpawnRefused, match='^model and leg disagree$'):
        resolve_target(target, chosen=chosen)


def test_matching_target_preserves_chosen_leg():
    chosen = replace(DEFAULT_LEGS[0], disabled=None)
    assert resolve_target(chosen.model, chosen=chosen) == (chosen.model, chosen)


def test_matching_disabled_leg_still_refused():
    chosen = replace(DEFAULT_LEGS[0], disabled='test policy')
    with pytest.raises(SpawnRefused, match='leg disabled: .*test policy'):
        resolve_target(chosen.model, chosen=chosen)
