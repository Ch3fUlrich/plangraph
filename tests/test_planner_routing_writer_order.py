"""Writer ordering regression entry points, split from the routing size budget."""


def test_writer_order_sol_family_excluded():
    from test_route_free_first import test_writer_order_sol_family_excluded as check
    check()


def test_writer_order_privacy_excludes_training_sol():
    from test_route_free_first import test_writer_order_privacy_excludes_training_sol as check
    check()


def test_medium_writer_and_review_defaults():
    from plangraph.planner.legs import DEFAULT_LEGS, LEAD_WRITER
    assert LEAD_WRITER == 'cx-gpt-6.1-sol-medium'
    assert next(l for l in DEFAULT_LEGS if l.id == LEAD_WRITER).effort == 'medium'
    assert next(l for l in DEFAULT_LEGS if l.id == 'cx-gpt-6-luna-medium').effort == 'medium'
    assert all(l.effort not in ('high', 'xhigh', 'max')
               for l in DEFAULT_LEGS if 'review' in l.roles)


def test_higher_effort_requires_card_and_decision():
    import pytest
    from plangraph.spawner.target import admit_effort
    from plangraph.routing_errors import SpawnRefused
    for card in ({}, {'effort': 'high'}, {'effort_decision': 'D-123'}):
        with pytest.raises(SpawnRefused, match='explicit card effort and D-id'):
            admit_effort('provider/gpt-6.1-sol-high', card)
    admit_effort('provider/gpt-6.1-sol-high', {'effort': 'high', 'effort_decision': 'D-123'})
    admit_effort('provider/gpt-6.1-sol-medium', {})


def test_medium_field_cannot_mask_high_model_suffix():
    import pytest
    from plangraph.spawner.target import admit_effort
    from plangraph.routing_errors import SpawnRefused
    with pytest.raises(SpawnRefused, match='explicit card effort and D-id'):
        admit_effort('provider/gpt-6.1-sol-high', {}, effort='medium')
