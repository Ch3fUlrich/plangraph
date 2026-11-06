"""D-4151: keep refusal probes and policy descriptions honest."""
from pathlib import Path


def test_refusal_probe_passes_explicit_arguments():
    text = Path('tests/test_leg_expiry.py').read_text()
    assert 'SpawnService(runner=SpawnerRunner(), state=tmp_path, leg_reader=reader)' in text
    assert 'card=card, prompt="task", role="implement"' in text


def test_writer_exclusion_comment_matches_policy():
    text = Path('src/plangraph/planner/routing.py').read_text()
    assert '# D-2511: exclude every writer family from all review seats.' in text
    assert 'EVERY writer covers no part' not in text


def test_policy_probe_names_match_assertions():
    registry = Path('tests/test_registry_route.py').read_text()
    order = Path('tests/test_route_round3.py').read_text()
    assert 'def test_seat_excludes_every_writer_family(' in registry
    assert 'def test_review_same_family_table_order(' in order
