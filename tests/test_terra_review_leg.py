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
    plan = plan_spawn(dict(CARD, risk_tags=['auth'] if high else []), role='review', writer_families=(writer,))
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


def test_loaded_review_head_retains_order_and_seating(tmp_path):
    import json
    from dataclasses import asdict
    from plangraph.planner.legs import load_legs
    path = tmp_path / 'legs.json'
    raw = [dict(asdict(l), id='custom-head') if l.id == TERRA else asdict(l) for l in DEFAULT_LEGS]
    path.write_text(json.dumps({'legs': raw}))
    legs = load_legs(path)
    plan = plan_spawn(CARD, legs=legs, role='review', writer_families=('gpt',))
    assert plan.first_leg.id == 'custom-head'
    assert plan.review_seats[0].id == 'custom-head'
    assert plan.seat_coverage[0]['covers'] == []


def test_single_writer_suppression_matches_route_card():
    from plangraph.planner.routing import RouteError
    luna = next(l for l in DEFAULT_LEGS if l.id == 'cx-gpt-6-luna')
    plan = plan_spawn(CARD, role='review', writer_families=('gpt',))
    reason = next(s['reason'] for s in plan.skipped if s['leg'] == luna.id)
    with pytest.raises(RouteError) as caught:
        route_card(CARD, legs=(luna,), role='review', exclude_families=('gpt',))
    assert reason == 'excluded_family'
    assert reason in caught.value.exclusion_reasons


@pytest.mark.parametrize('invalid', ['roles', 'duplicate'])
def test_review_head_config_refused(tmp_path, invalid):
    import json
    from dataclasses import asdict
    from plangraph.planner.legs import load_legs
    head = asdict(next(l for l in DEFAULT_LEGS if l.review_head))
    raw = [head]
    if invalid == 'roles':
        head['roles'] = ['implement', 'review']
    else:
        raw.append(dict(head, id='second-head'))
    path = tmp_path / 'legs.json'
    path.write_text(json.dumps({'legs': raw}))
    with pytest.raises(ValueError, match='review_head'):
        load_legs(path)


def test_catalog_seeds_terra():
    from plangraph.routing_support import resolver
    from plangraph.routing_support.catalog_db import CatalogDB
    db = CatalogDB(':memory:')
    db.init_and_seed()
    rows = [r for r in resolver.get_catalog_models(db) if r['id'] == 'omniroute/' + TERRA]
    assert len(rows) == 1
    row = rows[0]
    assert row['harness'] == 'opencode' and row['family'] == 'gpt'
    assert row['tool_mode'] == 'inline-only'
    assert row['context_tokens'] == 900000 and row['trains_on_prompts'] == 0


@pytest.mark.parametrize('model', [TERRA, 'cx-gpt-6-luna'])
@pytest.mark.parametrize('review_of', [False, True])
def test_override_same_family_only_review_head(tmp_path, monkeypatch, model, review_of):
    import asyncio
    from registry_route_util import _service
    from plangraph.planner.routing import SpawnRefused
    svc = _service(tmp_path, monkeypatch)
    if review_of:
        original_result = svc.result
        monkeypatch.setattr(svc, 'result', lambda run, **k: {'writer_family': 'gpt'}
                            if run == 'writer' else original_result(run, **k))
    kwargs = {'review_of': 'writer'} if review_of else {'writer_families': ('gpt',)}
    def spawn():
        return asyncio.run(svc.spawn(card=CARD, prompt='review', role='review',
                                    model='omniroute/' + model, override_reason='Terra override regression',
                                    dry_run=True, **kwargs))
    if model == TERRA:
        assert spawn()['chosen_leg']['id'] == TERRA
    else:
        with pytest.raises(SpawnRefused, match='excluded writer family'):
            spawn()
