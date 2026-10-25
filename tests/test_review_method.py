import asyncio
import json
import os
import subprocess
import sys

import pytest

from plangraph.settings import ReviewMethod, review_method
from plangraph.routing_errors import SpawnRefused
from plangraph.spawner.service import SpawnService


def test_defaults(monkeypatch):
    monkeypatch.delenv('PLANGRAPH_SETTINGS', raising=False)
    assert review_method() == ReviewMethod()
    assert review_method().seats == {'standard': 1, 'high': 2}


def test_root_and_safe_source(tmp_path, monkeypatch):
    (tmp_path / 'policy.json').write_text('{"review_method":{"max_rounds":4}}')
    monkeypatch.setenv('PLANGRAPH_SETTINGS', 'policy.json')
    assert review_method(root=tmp_path).source == 'global'


@pytest.mark.parametrize('role', ['implement', 'review'])
def test_explicit_scope_all_roles(monkeypatch, role):
    from plangraph.settings import enforce_review_scope
    monkeypatch.delenv('PLANGRAPH_SETTINGS', raising=False)
    with pytest.raises(SpawnRefused, match='review_method_batch_per_fixer'):
        enforce_review_scope({}, role, 'per-fixer')


def test_stamped_scope_validation(monkeypatch):
    from plangraph.settings import enforce_review_scope, SettingsError
    monkeypatch.delenv('PLANGRAPH_SETTINGS', raising=False)
    with pytest.raises(SettingsError, match='review_scope'):
        enforce_review_scope({'review_scope': 'per_fixer'}, 'review', 'batch')


def test_policy_flip_context(tmp_path, monkeypatch):
    from plangraph.settings import enforce_review_scope
    path = tmp_path / 'policy.json'
    monkeypatch.setenv('PLANGRAPH_SETTINGS', 'policy.json')
    card = {'project': 'demo', 'phase': 'demo:P', 'repo_root': str(tmp_path),
            'review_scope': 'per-fixer'}
    path.write_text('{"phases":{"demo:P":{"review_method":{"mode":"per-item"}}}}')
    enforce_review_scope(card, 'review', None)
    path.write_text('{"review_method":{"mode":"batch"}}')
    with pytest.raises(SpawnRefused):
        enforce_review_scope(card, 'review', None)
    card['review_scope'] = 'batch'
    path.write_text('{"review_method":{"mode":"per-item"}}')
    enforce_review_scope(card, 'review', None)


def test_safe_decode(tmp_path, monkeypatch):
    from plangraph.settings import SettingsError
    path = tmp_path / 'policy.toml'
    path.write_text('[secret-content')
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(path))
    with pytest.raises(SettingsError) as caught:
        review_method()
    assert 'secret-content' not in str(caught.value)
    assert 'TOMLDecodeError' in str(caught.value)


def test_unsupported_tier():
    from plangraph.settings import SettingsError
    with pytest.raises(SettingsError, match='unsupported tier'):
        ReviewMethod(seats={'standard': 1, 'high': 2, 'alien': 3})


def test_json_only_without_tomllib(tmp_path, monkeypatch):
    import plangraph.settings as settings
    monkeypatch.setattr(settings, 'tomllib', None)
    path = tmp_path / 'policy.toml'
    path.write_text('')
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(path))
    with pytest.raises(settings.SettingsError, match='tomllib unavailable'):
        review_method()
    path = tmp_path / 'policy.json'
    path.write_text('{}')
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(path))
    assert review_method().mode == 'batch'


def test_cwd_non_git_fallback(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'policy.json').write_text('{"review_method":{"max_rounds":7}}')
    monkeypatch.setenv('PLANGRAPH_SETTINGS', 'policy.json')
    monkeypatch.delenv('PLANGRAPH_REPO_ROOT', raising=False)
    assert review_method().max_rounds == 7


def test_non_review_batch_rejected(monkeypatch):
    from plangraph.settings import enforce_review_scope, SettingsError
    monkeypatch.delenv('PLANGRAPH_SETTINGS', raising=False)
    with pytest.raises(SettingsError, match='only supported'):
        enforce_review_scope({}, 'implement', 'batch')


def test_documented_advisory_and_batch_boundaries():
    from pathlib import Path
    text = Path('docs/REVIEW-METHOD.md').read_text()
    assert 'but advisory' in text
    assert 'does not synthesize' in text
    assert 'retains multiple existing review cards' in text
    assert 'read per call' in text


def test_cli_settings_errors_not_audited(tmp_path, monkeypatch):
    from plangraph.spawner.cli import main
    from plangraph.settings import SettingsError
    path = tmp_path / 'policy.json'
    path.write_text('{')
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(path))
    calls = []
    monkeypatch.setattr(SpawnService, 'save_refusal', lambda *a: calls.append(a))
    main(['--state', str(tmp_path / 'state'), 'spawn', 'task', '--card', '{"key":"demo:C"}',
          '--role', 'review', '--review-scope', 'batch', '--dry-run'])
    assert calls == []


def test_layer_label_cli(tmp_path, monkeypatch):
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps({'review_method': {'max_rounds': 4},
        'projects': {'demo': {'review_method': {'mode': 'per-item'}}}}))
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(path))
    result = subprocess.run([sys.executable, '-m', 'plangraph.settings', 'show',
        '--project', 'demo'], capture_output=True, text=True, check=True,
        env={**os.environ, 'PYTHONPATH': 'src'})
    value = json.loads(result.stdout)
    assert value['source'] == 'projects:demo'
    assert str(tmp_path) not in result.stdout


def test_precedence(tmp_path, monkeypatch):
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'review_method': {'max_rounds': 4},
        'projects': {'demo': {'review_method': {'mode': 'per-item'}}},
        'phases': {'demo:P': {'review_method': {'max_rounds': 2}}}}))
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(path))
    value = review_method('demo', 'demo:P')
    assert value.mode == 'per-item' and value.max_rounds == 2
    assert review_method().max_rounds == 4


@pytest.mark.parametrize('field,value', [('mode', 'bad'), ('seats', {'high': 0}),
    ('distinct_families', 1), ('family_source', ''), ('max_rounds', 0),
    ('fixer_split_threshold', True)])
def test_invalid(field, value):
    with pytest.raises(ValueError, match=field):
        ReviewMethod(**{field: value})


def test_spawn_scope(tmp_path, monkeypatch):
    service = SpawnService(runner=object(), state=tmp_path)
    monkeypatch.delenv('PLANGRAPH_SETTINGS', raising=False)
    with pytest.raises(SpawnRefused) as caught:
        asyncio.run(service._spawn(card={'key': 'demo:C'}, role='review', review_scope='per-fixer'))
    assert caught.value.reason == 'review_method_batch_per_fixer'
    path = tmp_path / 'settings.json'
    path.write_text('{"review_method": {"mode": "per-item"}}')
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(path))
    # A sentinel route proves admission reached routing rather than refusing.
    def route(*args, **kwargs):
        raise RuntimeError('admitted')
    service._route = route
    with pytest.raises(RuntimeError, match='admitted'):
        asyncio.run(service._spawn(card={'key': 'demo:C'}, role='review', review_scope='per-fixer'))


def test_cli_show(monkeypatch):
    monkeypatch.delenv('PLANGRAPH_SETTINGS', raising=False)
    result = subprocess.run([sys.executable, '-m', 'plangraph.settings', 'show',
        '--project', 'demo', '--phase', 'demo:P'], capture_output=True, text=True, check=True,
        env={**os.environ, "PYTHONPATH": "src"})
    value = json.loads(result.stdout)
    assert value['mode'] == 'batch' and value['source'] == 'default'


def test_toml_partial_seats(tmp_path, monkeypatch):
    path = tmp_path / 'settings.toml'
    path.write_text('[review_method.seats]\nhigh = 3\n')
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(path))
    assert review_method().seats == {'standard': 1, 'high': 3}


def test_dispatch_scope(tmp_path, monkeypatch):
    from plangraph.spawner.runner import spawn_service
    monkeypatch.delenv('PLANGRAPH_SETTINGS', raising=False)
    service = spawn_service(runner=object(), state=tmp_path)
    with pytest.raises(SpawnRefused, match='review_method_batch_per_fixer'):
        asyncio.run(service._spawn(card={'key': 'demo:C'}, role='review',
                                  review_scope='per-fixer'))


def test_plan_phase_carries_method(tmp_path, monkeypatch):
    from keeper_util import World, defn
    from plangraph.planner.phase import plan_phase
    monkeypatch.delenv('PLANGRAPH_SETTINGS', raising=False)
    world = World(tmp_path / 'keeper')
    try:
        world.store.create_items(world.p('router'), [defn('P', kind='gate'),
            defn('GOAL', ['P'], kind='goal')])
        plan = plan_phase(world.store, world.p('router'), 'demo:P', repo_root=tmp_path)
        assert plan['review_method']['mode'] == 'batch'
        assert plan['review_method']['max_rounds'] == 3
    finally:
        world.close()
