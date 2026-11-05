"""D-3817: Terra leads review without covering its own writer family."""
import pytest
from plangraph.planner.legs import DEFAULT_LEGS, WRITER_ORDER
from plangraph.planner.routing import plan_spawn, route_card

TERRA = 'cx-gpt-6.1-terra'
CARD = {'estimate_tokens': 20000, 'diff_ref': {'repo': '/srv/repo', 'base': 'a1', 'head': 'b2'}}


def test_review_order_moves_only_terra():
    ordinary = route_card(CARD)
    review = route_card(CARD, role='review')
    assert [l.id for l in review.legs] == [TERRA] + [l.id for l in ordinary.legs if l.id != TERRA]
    leg = next(l for l in DEFAULT_LEGS if l.id == TERRA)
    assert leg.roles == ('review',) and leg.tool_mode == 'inline-only'
    assert TERRA not in WRITER_ORDER
    assert TERRA not in [l.id for l in plan_spawn(CARD, role='implement').legs]


@pytest.mark.parametrize('writer,high,count', [('gpt', False, 2), ('gpt', True, 3), ('mimo', False, 2), ('mimo', True, 2)])
def test_terra_seat_coverage(writer, high, count):
    plan = plan_spawn(dict(CARD, risk='high' if high else 'normal'), role='review', writer_families=(writer,))
    assert not plan.error
    assert plan.first_leg.id == TERRA and plan.review_seats[0].id == TERRA
    assert len(plan.review_seats) == count
    assert len({l.family for l in plan.review_seats if l.family != writer}) >= (2 if high else 1)
    assert plan.seat_coverage[0]['covers'] == ([] if writer == 'gpt' else ['xiaomi'])
    if writer == 'gpt':
        assert 'cx-gpt-6-luna' not in [l.id for l in plan.legs]


def test_explicit_gpt_exclusion_still_excludes_terra():
    plan = plan_spawn(CARD, role='review', writer_families=('mimo',), exclude_families=('gpt',))
    assert TERRA not in [l.id for l in plan.legs]
