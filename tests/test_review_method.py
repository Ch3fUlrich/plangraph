import asyncio
import json
import os
from pathlib import Path
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


def test_explicit_scope_all_roles(monkeypatch):
    from plangraph.settings import enforce_review_scope, SettingsError
    monkeypatch.delenv('PLANGRAPH_SETTINGS', raising=False)
    with pytest.raises(SettingsError, match='review_scope only valid for role=review'):
        enforce_review_scope({}, 'implement', 'per-fixer')
    with pytest.raises(SettingsError, match='review_scope only valid for role=review'):
        enforce_review_scope({}, 'implement', 'batch')
    with pytest.raises(SettingsError, match='review_scope only valid for role=review'):
        enforce_review_scope({'review_scope': 'per-fixer'}, None, None)
    with pytest.raises(SettingsError, match='review_scope only valid for role=review'):
        enforce_review_scope({'review_scope': 'per-fixer'}, 'implement', None)
    with pytest.raises(SpawnRefused, match='review_method_batch_per_fixer'):
        enforce_review_scope({}, 'review', 'per-fixer')


def test_stamped_scope_validation(monkeypatch):
    from plangraph.settings import enforce_review_scope, SettingsError
    monkeypatch.delenv('PLANGRAPH_SETTINGS', raising=False)
    with pytest.raises(SettingsError, match='review_scope'):
        enforce_review_scope({'review_scope': 'per_fixer'}, 'review', 'batch')


def test_policy_flip_context(tmp_path, monkeypatch):
    from plangraph.settings import enforce_review_scope
    path = tmp_path / 'policy.json'
    monkeypatch.setenv('PLANGRAPH_SETTINGS', 'policy.json')
    # Layers come from the trusted keeper item (R2); root from root= (R1).
    item = {'key': 'demo:C', 'phase': 'demo:P'}
    card = {'review_scope': 'per-fixer'}
    path.write_text('{"phases":{"demo:P":{"review_method":{"mode":"per-item"}}}}')
    enforce_review_scope(card, 'review', None, item=item, root=tmp_path)
    path.write_text('{"review_method":{"mode":"batch"}}')
    with pytest.raises(SpawnRefused):
        enforce_review_scope(card, 'review', None, item=item, root=tmp_path)
    card['review_scope'] = 'batch'
    path.write_text('{"review_method":{"mode":"per-item"}}')
    enforce_review_scope(card, 'review', None, item=item, root=tmp_path)


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
    with pytest.raises(SettingsError, match='review_scope only valid for role=review'):
        enforce_review_scope({}, 'implement', 'batch')


def test_documented_advisory_and_batch_boundaries(tmp_path, monkeypatch):
    from pathlib import Path
    from plangraph.settings import enforce_review_scope
    text = (Path(__file__).parent.parent / 'docs' / 'REVIEW-METHOD.md').read_text(encoding='utf-8')
    assert 'ADVISORY' in text
    assert 'TOCTOU' in text
    assert 'spoof' in text
    assert 'per-fixer seat' in text

    # Behavioral test 1: advisory fields (seats, max_rounds, distinct_families, family_source)
    # do not prevent admission or alter enforce_review_scope
    path = tmp_path / 'advisory.json'
    path.write_text(json.dumps({
        'review_method': {
            'seats': {'standard': 10, 'high': 20},
            'max_rounds': 99,
            'distinct_families': False,
            'family_source': 'custom'
        }
    }))
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(path))
    # Admission is unaffected by advisory fields
    enforce_review_scope({'key': 'demo:C'}, 'review', 'batch')

    # Behavioral test 2: policy file is dynamically re-read per call without caching
    path.write_text(json.dumps({'review_method': {'max_rounds': 5}}))
    assert review_method().max_rounds == 5
    path.write_text(json.dumps({'review_method': {'max_rounds': 42}}))
    assert review_method().max_rounds == 42


def test_finding_9_mcp_review_scope_passthrough(tmp_path, monkeypatch):
    from plangraph.spawner.mcp import MCPSpawnerServer
    from plangraph.routing_errors import SpawnRefused
    server = MCPSpawnerServer(state=tmp_path)
    monkeypatch.delenv('PLANGRAPH_SETTINGS', raising=False)
    # 1. MCP passes review_scope='per-fixer' to spawn, which refuses under batch mode
    with pytest.raises(SpawnRefused, match='review_method_batch_per_fixer'):
        asyncio.run(server.call_tool('plangraph_agent_spawn', {
            'card': {'key': 'demo:C', 'estimate_tokens': 1000},
            'prompt': 'review task',
            'role': 'review', 'writer_families': ['claude'],
            'review_scope': 'per-fixer',
            'model_or_combo': 'mimo/mimo-v2.6-flash',
            'override_reason': 'explicit test override',
            'dry_run': True
        }))
    # 2. MCP passes review_scope='batch' to spawn, which is admitted
    res = asyncio.run(server.call_tool('plangraph_agent_spawn', {
        'card': {'key': 'demo:C', 'estimate_tokens': 1000},
        'prompt': 'review task',
        'role': 'review', 'writer_families': ['claude'],
        'review_scope': 'batch',
        'model_or_combo': 'mimo/mimo-v2.6-flash',
        'override_reason': 'explicit test override',
        'dry_run': True
    }))
    assert res['status'] == 'dry_run'


def test_finding_9_cli_spawn_refused_audited(tmp_path, monkeypatch, capsys):
    from plangraph.spawner.cli import main
    from plangraph.routing_errors import SpawnRefused
    state_dir = tmp_path / 'spawner_state'
    monkeypatch.delenv('PLANGRAPH_SETTINGS', raising=False)
    saved_records = []
    orig_save = SpawnService.save_refusal
    def mock_save(self, exc, role):
        rec = orig_save(self, exc, role)
        saved_records.append((exc, role, rec))
        return rec
    monkeypatch.setattr(SpawnService, 'save_refusal', mock_save)

    code = main(['--state', str(state_dir), 'spawn', 'task',
                 '--card', '{"key":"demo:C"}',
                 '--role', 'review', '--review-scope', 'per-fixer', '--dry-run'])
    assert code == 2
    err = capsys.readouterr().err
    assert 'review_method_batch_per_fixer' in err
    assert len(saved_records) == 1
    exc, role, rec = saved_records[0]
    assert isinstance(exc, SpawnRefused)
    assert exc.reason == 'review_method_batch_per_fixer'
    assert role == 'review'
    # Verify refusal record was actually written to state runs
    persisted = SpawnService(state=state_dir).result(rec['run_id'])
    assert persisted['error'] == 'review_method_batch_per_fixer'
    assert persisted['role'] == 'review'
    assert persisted['status'] == 'refused'





def test_cli_settings_errors_audited_as_review_method_invalid(tmp_path, monkeypatch):
    """Settings problems are audited as review_method_invalid: one record, no double-record."""
    from plangraph.spawner.cli import main
    path = tmp_path / 'policy.json'
    path.write_text('{')
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(path))
    calls = []
    monkeypatch.setattr(SpawnService, 'save_refusal', lambda self, exc, role: calls.append((exc, role)))
    rc = main(['--state', str(tmp_path / 'state'), 'spawn', 'task', '--card', '{"key":"demo:C"}',
               '--role', 'review', '--review-scope', 'batch', '--dry-run'])
    assert rc == 2
    assert len(calls) == 1
    exc, role = calls[0]
    assert isinstance(exc, SpawnRefused)
    assert exc.reason == 'review_method_invalid'
    assert '{' not in str(exc)


def test_finding_1_spawner_paths_convert_settings_errors(tmp_path, monkeypatch):
    """service._spawn, spawn fixer dispatch, and runner non-delegating path convert SettingsError."""
    import plangraph.spawner.runner as runner_mod
    path = tmp_path / 'policy.json'
    path.write_text('{bad-json')
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(path))

    # 1. service._spawn converts SettingsError to SpawnRefused('review_method_invalid')
    service = SpawnService(runner=object(), state=tmp_path)
    with pytest.raises(SpawnRefused) as exc_spawn:
        asyncio.run(service._spawn(card={'key': 'demo:C'}, role='review', review_scope='batch'))
    assert exc_spawn.value.reason == 'review_method_invalid'
    assert 'bad-json' not in str(exc_spawn.value)

    # 2. SpawnService.spawn fixer dispatch converts SettingsError
    card_findings = {'key': 'demo:C', 'findings': [{'text': 'gap1'}, {'text': 'gap2'}]}
    with pytest.raises(SpawnRefused) as exc_fixer:
        asyncio.run(service.spawn(card=card_findings, role='implement', prompt='fix stuff'))
    assert exc_fixer.value.reason == 'review_method_invalid'
    assert 'bad-json' not in str(exc_fixer.value)

    # 3. runner non-delegating path converts SettingsError
    runner_service = runner_mod.spawn_service(runner=object(), state=tmp_path)
    with pytest.raises(SpawnRefused) as exc_runner:
        asyncio.run(runner_service._spawn(card={'key': 'demo:C'}, role='review', review_scope='batch'))
    assert exc_runner.value.reason == 'review_method_invalid'
    assert 'bad-json' not in str(exc_runner.value)



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


def test_finding_10_review_method_source_validation():
    from plangraph.settings import SettingsError
    # Valid sources
    assert ReviewMethod(source='default').source == 'default'
    assert ReviewMethod(source='global').source == 'global'
    assert ReviewMethod(source='projects:demo').source == 'projects:demo'
    assert ReviewMethod(source='phases:demo:P').source == 'phases:demo:P'

    # Invalid sources with path separators or empty
    with pytest.raises(SettingsError, match='path separators not allowed'):
        ReviewMethod(source='/etc/settings.json')
    with pytest.raises(SettingsError, match='path separators not allowed'):
        ReviewMethod(source='relative/path')
    with pytest.raises(SettingsError, match='path separators not allowed'):
        ReviewMethod(source='win\\path')
    with pytest.raises(SettingsError, match='nonempty string'):
        ReviewMethod(source='')
    with pytest.raises(SettingsError, match='nonempty string'):
        ReviewMethod(source=123)


def test_finding_11_settings_suffix_case_insensitive(tmp_path, monkeypatch):
    path = tmp_path / 'POLICY.JSON'
    path.write_text('{"review_method": {"max_rounds": 9}}')
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(path))
    assert review_method().max_rounds == 9

    path_toml = tmp_path / 'POLICY.TOML'
    path_toml.write_text('[review_method]\nmax_rounds = 8\n')
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(path_toml))
    assert review_method().max_rounds == 8


def test_finding_2_derive_project_from_phase(tmp_path, monkeypatch):
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps({
        'projects': {'demo': {'review_method': {'mode': 'per-item'}}}
    }))
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(path))
    val = review_method(project=None, phase='demo:P')
    assert val.mode == 'per-item'
    assert val.source == 'projects:demo'


def test_finding_5_fixer_threshold(tmp_path, monkeypatch):
    from plangraph.settings import fixer_threshold
    monkeypatch.delenv('PLANGRAPH_SETTINGS', raising=False)
    # Default without context
    assert fixer_threshold() == 10
    assert fixer_threshold(item={}) == 10

    # With keeper item project/phase override (R2: card fields never select)
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps({
        'review_method': {'fixer_split_threshold': 12},
        'projects': {'demo': {'review_method': {'fixer_split_threshold': 5}}},
        'phases': {'demo:special': {'review_method': {'fixer_split_threshold': 2}}}
    }))
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(path))
    assert fixer_threshold(item={'key': 'other:1'}) == 12
    assert fixer_threshold(item={'key': 'demo:1'}) == 5
    assert fixer_threshold(item={'key': 'demo:1', 'track': 'demo:special'}) == 2
    assert fixer_threshold(item={'key': 'demo:1', 'phase': 'demo:special'}) == 2


def test_finding_7_runner_spawn_keyword_and_single_guard(tmp_path, monkeypatch):
    import plangraph.spawner.runner as runner_mod
    import plangraph.settings as settings_mod

    guard_calls = []
    monkeypatch.setattr(settings_mod, 'enforce_review_scope',
                        lambda *a, **k: guard_calls.append((a, k)))

    super_kwargs = []
    async def fake_super_spawn(self, *args, **kwargs):
        super_kwargs.append((args, kwargs))
        return {'status': 'super_spawn_ok'}
    monkeypatch.setattr(SpawnService, '_spawn', fake_super_spawn)

    service = runner_mod.spawn_service(runner=object(), state=tmp_path)

    # Delegating path (with model override): must pass by keyword and run guard once
    guard_calls.clear()
    super_kwargs.clear()
    res = asyncio.run(service._spawn(card={'key': 'demo:C', 'estimate_tokens': 100},
                                     role='review', model='mimo/mimo-v2.6-flash',
                                     review_scope='batch'))
    assert res['status'] == 'super_spawn_ok'
    assert len(super_kwargs) == 1
    args, kwargs = super_kwargs[0]
    # Verify review_scope was passed by keyword
    assert kwargs.get('review_scope') == 'batch'
    # And positional args must be empty (all passed by keyword to super()._spawn)
    assert args == ()
    # Guard runs only once (in super()._spawn, not runner before delegating)
    # Since fake_super_spawn bypassed super, runner shouldn't have called it before delegating
    assert len(guard_calls) == 0

    # Non-delegating path: runner guards on its own
    guard_calls.clear()
    dummy_plan = type('Plan', (), {'legs': (), 'error': 'no leg', 'skipped': (), 'to_dict': lambda self: {'skipped': []}})()
    service._route = lambda *a, **k: dummy_plan
    service._save = lambda *a: None
    service.result = lambda run_id, **kw: {'status': 'refused'}
    asyncio.run(service._spawn(card={'key': 'demo:C', 'estimate_tokens': 100},
                               role='review', review_scope='batch'))
    assert len(guard_calls) == 1


def test_finding_8_cli_review_scope_rejected_on_non_spawn(tmp_path, capsys):
    from plangraph.spawner.cli import main
    # --review-scope passed to route verb must be rejected by CLI argparser
    with pytest.raises(SystemExit) as exc:
        main(['--state', str(tmp_path), 'route', '--card', '{"estimate_tokens":1000}',
              '--review-scope', 'batch'])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert 'unrecognized arguments: --review-scope' in err


def test_finding_1_top_level_review_scope_and_per_card(tmp_path, monkeypatch):
    from keeper_util import World, defn
    from plangraph.planner.phase import plan_phase
    monkeypatch.delenv('PLANGRAPH_SETTINGS', raising=False)
    repo = tmp_path / 'repo'
    (repo / 'src').mkdir(parents=True)
    (repo / 'src' / 'a.py').write_text('A = 1\n')
    world = World(tmp_path / 'keeper')
    try:
        world.store.create_items(world.p('router'), [
            defn('C1'),
            defn('P', ['C1'], kind='gate'),
            defn('GOAL', ['P'], kind='goal'),
        ])
        card_data = {'outcome': 'done', 'write_set': ['src/a.py'], 'estimate_tokens': 1000}
        world.store.put_card(world.p('router'), 'demo:C1', card_data)
        # 1. Real keeper card (has NO role column)
        plan = plan_phase(world.store, world.p('router'), 'demo:P', repo_root=repo)
        assert 'review_scope' in plan
        assert plan['review_scope'] == 'batch'
        # Card should not have review_scope stamped since role is not review
        card_entry = next(c for c in plan['cards'] if c['key'] == 'demo:C1')
        assert 'review_scope' not in card_entry
        assert 'role' not in card_entry

        # 2. Monkeypatched store.get_card returning role='review'
        orig_get_card = world.store.get_card
        def mock_get_card(principal, key):
            c = orig_get_card(principal, key)
            return {**c, 'role': 'review'}
        monkeypatch.setattr(world.store, 'get_card', mock_get_card)
        plan_review = plan_phase(world.store, world.p('router'), 'demo:P', repo_root=repo)
        assert plan_review['review_scope'] == 'batch'
        rev_card = next(c for c in plan_review['cards'] if c['key'] == 'demo:C1')
        assert rev_card['role'] == 'review'
        assert rev_card['review_scope'] == 'batch'
    finally:
        world.close()


def test_finding_4_plan_phase_settings_error(tmp_path, monkeypatch):
    from keeper_util import World, defn
    from plangraph.planner.phase import plan_phase
    from plangraph.keeper.errors import Refused
    path = tmp_path / 'bad_policy.json'
    path.write_text('{invalid-json-content}')
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(path))
    world = World(tmp_path / 'keeper')
    try:
        world.store.create_items(world.p('router'), [
            defn('P', kind='gate'),
            defn('GOAL', ['P'], kind='goal'),
        ])
        with pytest.raises(Refused) as exc:
            plan_phase(world.store, world.p('router'), 'demo:P', repo_root=tmp_path)
        assert getattr(exc.value, 'code', None) == 'validation' or exc.value.args[0].startswith('validation:')
        assert 'review_method_invalid:' in str(exc.value)
        assert 'invalid-json-content' not in str(exc.value)
    finally:
        world.close()


def test_finding_4_routing_plan_spawn_settings_error(tmp_path, monkeypatch):
    from plangraph.planner.routing import plan_spawn
    from plangraph.routing_errors import SpawnRefused
    path = tmp_path / 'bad_policy.json'
    path.write_text('{secret_broken_json')
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(path))
    card = {'estimate_tokens': 1000, 'findings': [{'files': ['a.py']}]}
    with pytest.raises(SpawnRefused) as exc:
        plan_spawn(card, role='implement')
    assert exc.value.reason == 'review_method_invalid'
    assert 'secret_broken_json' not in str(exc.value)


def test_finding_3_card_repo_root_policy_file_ignored(tmp_path, monkeypatch):
    """R1 (flipped): a card repo_root pointing at a per-item policy is IGNORED.

    The trusted root holds batch policy, so the per-fixer seat is still refused
    under the global policy and the fixer threshold stays global.
    """
    from plangraph.settings import enforce_review_scope, fixer_threshold
    hostile = tmp_path / 'hostile'
    hostile.mkdir()
    (hostile / 'policy.json').write_text(
        '{"review_method": {"mode": "per-item", "fixer_split_threshold": 2}}')
    trusted = tmp_path / 'trusted'
    trusted.mkdir()
    (trusted / 'policy.json').write_text(
        '{"review_method": {"mode": "batch", "fixer_split_threshold": 12}}')
    monkeypatch.setenv('PLANGRAPH_SETTINGS', 'policy.json')
    monkeypatch.setenv('PLANGRAPH_REPO_ROOT', str(trusted))
    card = {'key': 'demo:C', 'phase': 'demo:P', 'repo_root': str(hostile)}
    with pytest.raises(SpawnRefused, match='review_method_batch_per_fixer'):
        enforce_review_scope(card, 'review', 'per-fixer')
    assert fixer_threshold() == 12
    # fixer_threshold no longer accepts a card at all
    with pytest.raises(TypeError):
        fixer_threshold(card)


def test_card_phase_key_project_spoof_cannot_admit_per_fixer(tmp_path, monkeypatch):
    """R2: card 'phase'/'project'/'key' never select a per-item layer; item= does."""
    from plangraph.settings import enforce_review_scope
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps({
        'review_method': {'mode': 'batch'},
        'projects': {'demo': {'review_method': {'mode': 'per-item'}}},
        'phases': {'demo:P': {'review_method': {'mode': 'per-item'}}}
    }))
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(path))
    # Each card shape names a per-item phase/project override and must be ignored.
    for card in ({'key': 'demo:C', 'phase': 'demo:P'},
                 {'key': 'demo:C', 'project': 'demo'},
                 {'key': 'demo:P'}):
        with pytest.raises(SpawnRefused, match='review_method_batch_per_fixer'):
            enforce_review_scope(card, 'review', 'per-fixer')
    # The same overrides passed via a trusted keeper item do admit.
    enforce_review_scope({'key': 'demo:C'}, 'review', 'per-fixer',
                         item={'key': 'demo:C', 'phase': 'demo:P'})
    enforce_review_scope({'key': 'demo:C'}, 'review', 'per-fixer',
                         item={'key': 'demo:C'})


def test_finding_2_project_of_helper_colon_free(tmp_path, monkeypatch):
    from plangraph.settings import project_of, fixer_threshold
    assert project_of('demo') == 'demo'
    assert project_of('demo:item') == 'demo'
    assert project_of({'key': 'demo'}) == 'demo'
    assert project_of({'key': 'demo:C'}) == 'demo'
    assert project_of({'project': 'demo', 'key': 'C'}) == 'demo'
    assert project_of(None) is None
    assert project_of({}) is None

    # Colon-free key resolves project in fixer_threshold (divergence fixed)
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps({
        'review_method': {'fixer_split_threshold': 10},
        'projects': {'myproject': {'review_method': {'fixer_split_threshold': 4}}}
    }))
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(path))
    assert fixer_threshold(item={'key': 'myproject'}) == 4


def test_finding_3_phase_key_with_slash(tmp_path, monkeypatch):
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps({
        'phases': {
            'demo:PH/2': {
                'review_method': {'max_rounds': 5}
            }
        }
    }))
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(path))
    method = review_method(phase='demo:PH/2')
    assert method.max_rounds == 5
    assert '/' not in method.source
    assert '\\' not in method.source


def test_finding_4_settings_cli_root_and_env(tmp_path, monkeypatch):
    custom_root = tmp_path / 'custom_root'
    custom_root.mkdir()
    path = custom_root / 'policy.json'
    path.write_text(json.dumps({'review_method': {'max_rounds': 9}}))
    monkeypatch.setenv('PLANGRAPH_SETTINGS', 'policy.json')

    # Test 1: --root PATH
    res = subprocess.run(
        [sys.executable, '-m', 'plangraph.settings', 'show', '--root', str(custom_root)],
        capture_output=True, text=True, check=True,
        env={**os.environ, 'PYTHONPATH': 'src'}
    )
    data = json.loads(res.stdout)
    assert data['max_rounds'] == 9

    # Test 2: PLANGRAPH_REPO_ROOT honored when --root not provided
    monkeypatch.setenv('PLANGRAPH_REPO_ROOT', str(custom_root))
    res2 = subprocess.run(
        [sys.executable, '-m', 'plangraph.settings', 'show'],
        capture_output=True, text=True, check=True,
        env={**os.environ, 'PLANGRAPH_REPO_ROOT': str(custom_root), 'PYTHONPATH': 'src'}
    )
    data2 = json.loads(res2.stdout)
    assert data2['max_rounds'] == 9


def test_finding_7_doc_distinguishes_policy_refusal_and_malformed_input():
    text = (Path(__file__).parent.parent / 'docs' / 'REVIEW-METHOD.md').read_text(encoding='utf-8')
    assert 'review_method_batch_per_fixer' in text
    assert 'review_method_invalid' in text
    assert 'Policy refusal' in text
    assert 'Malformed input' in text
    assert '`validation` (message prefix `review_method_invalid:`)' in text


def test_finding_1_unused_imports_and_single_helper():
    import ast
    import plangraph.settings as settings

    # admit_settings dead code decorator must be deleted
    assert not hasattr(settings, 'admit_settings'), 'admit_settings decorator should be deleted'

    # Single conversion helper exists and converts SettingsError to SpawnRefused
    assert hasattr(settings, 'as_spawn_refused'), 'as_spawn_refused helper should exist'
    err = settings.as_spawn_refused(settings.SettingsError('test detail'))
    assert isinstance(err, SpawnRefused)
    assert err.reason == 'review_method_invalid'
    assert err.code == 'review_method_invalid'
    assert 'test detail' in str(err)

    # routing.py plan_spawn must not import unused fixer_threshold, SettingsError, project_of
    routing_path = Path(__file__).parent.parent / 'src' / 'plangraph' / 'planner' / 'routing.py'
    tree = ast.parse(routing_path.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == 'plangraph.settings':
            imported_names = {alias.name for alias in node.names}
            assert 'fixer_threshold' not in imported_names
            assert 'SettingsError' not in imported_names
            assert 'project_of' not in imported_names


def test_finding_2_phase_fallback_to_item_key(tmp_path, monkeypatch):
    from plangraph.settings import enforce_review_scope, fixer_threshold
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps({
        'review_method': {'mode': 'batch', 'fixer_split_threshold': 10},
        'phases': {
            'demo:C': {
                'review_method': {'mode': 'per-item', 'fixer_split_threshold': 4}
            }
        }
    }))
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(path))

    # item has only 'key', no 'phase'; the item key falls back as the phase
    item = {'key': 'demo:C'}

    # 1. fixer_threshold must honor phases['demo:C'] override
    assert fixer_threshold(item=item) == 4

    # 2. enforce_review_scope must honor mode='per-item' override and allow per-fixer
    enforce_review_scope({'key': 'demo:C'}, 'review', 'per-fixer', item=item)


def test_finding_3_plan_phase_settings_error_sanitized_and_prefix(tmp_path, monkeypatch):
    from keeper_util import World, defn
    from plangraph.planner.phase import plan_phase
    from plangraph.keeper.errors import Refused
    path = tmp_path / 'bad_policy.json'
    path.write_text('{invalid-json-content}')
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(path))
    world = World(tmp_path / 'keeper')
    try:
        world.store.create_items(world.p('router'), [
            defn('P', kind='gate'),
            defn('GOAL', ['P'], kind='goal'),
        ])
        with pytest.raises(Refused) as exc:
            plan_phase(world.store, world.p('router'), 'demo:P', repo_root=tmp_path)
        assert exc.value.code == 'validation'
        assert exc.value.msg.startswith('review_method_invalid:')
        assert 'invalid-json-content' not in str(exc.value)
        assert str(path) not in str(exc.value)
    finally:
        world.close()


def test_finding_4_no_path_or_layer_in_error_and_control_char_sanitization(tmp_path, monkeypatch):
    from plangraph.settings import SettingsError

    # 1. No path in error message on missing file or syntax error
    missing_path = tmp_path / 'subdir' / 'policy.json'
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(missing_path))
    with pytest.raises(SettingsError) as exc_missing:
        review_method()
    assert '/' not in str(exc_missing.value)
    assert '\\' not in str(exc_missing.value)
    assert 'subdir' not in str(exc_missing.value)
    assert str(exc_missing.value) == 'file: FileNotFoundError'

    # 2. Layer name not included in SettingsError for invalid table
    bad_table_path = tmp_path / 'bad_layer.json'
    bad_table_path.write_text(json.dumps({'projects': {'demo_proj': 'not-a-dict'}}))
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(bad_table_path))
    with pytest.raises(SettingsError) as exc_layer:
        review_method(project='demo_proj')
    assert 'projects:demo_proj' not in str(exc_layer.value)
    assert '/' not in str(exc_layer.value)
    assert '\\' not in str(exc_layer.value)

    bad_override_path = tmp_path / 'bad_override.json'
    bad_override_path.write_text(json.dumps({'projects': {'demo_proj': {'review_method': 'not-a-dict'}}}))
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(bad_override_path))
    with pytest.raises(SettingsError) as exc_override:
        review_method(project='demo_proj')
    assert 'projects:demo_proj' not in str(exc_override.value)
    assert '/' not in str(exc_override.value)
    assert '\\' not in str(exc_override.value)

    # 3. Source/label sanitization replaces ALL control chars (\n, \t, \0) and path separators
    phase_key = "demo:PH/1\\2\n3\t4\x00"
    control_path = tmp_path / 'control.json'
    control_path.write_text(json.dumps({
        'phases': {
            phase_key: {'review_method': {'max_rounds': 8}}
        }
    }))
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(control_path))
    method = review_method(phase=phase_key)
    assert method.max_rounds == 8
    assert method.source == 'phases:demo:PH_1_2_3_4_'
    assert '\n' not in method.source
    assert '\t' not in method.source
    assert '\x00' not in method.source
    assert '/' not in method.source
    assert '\\' not in method.source

    # 4. Unknown-field-name interpolation path: the interpolated field name is
    # sanitized even when it carries control chars and path separators.
    field_name = 'bad\x00\x7f\x80\u2028\u202a\u2066\u200e/na\\me'
    unknown_path = tmp_path / 'unknown_field.json'
    unknown_path.write_text(json.dumps({'review_method': {field_name: 1}}))
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(unknown_path))
    with pytest.raises(SettingsError) as exc_unknown:
        review_method()
    message = str(exc_unknown.value)
    assert message == 'bad________na_me: unknown review_method setting'
    assert 'unknown review_method setting' in message
    assert not any(c in message for c in '/\\\x00\x7f\x80\u2028\u202a\u2066\u200e')


# LOW 1: C1 controls, U+2028/U+2029 and bidi controls must be sanitized in
# labels and rejected in ReviewMethod sources. One case per character class.
EXTENDED_CONTROL_CHARS = [
    ('c1_lo', '\x80'),
    ('c1_nel', '\x85'),
    ('c1_hi', '\x9f'),
    ('line_sep', '\u2028'),
    ('para_sep', '\u2029'),
    ('bidi_lre', '\u202a'),
    ('bidi_rle', '\u202b'),
    ('bidi_rlo', '\u202e'),
    ('bidi_lri', '\u2066'),
    ('bidi_rli', '\u2067'),
    ('bidi_fsi', '\u2068'),
    ('bidi_pdi', '\u2069'),
    ('lrm', '\u200e'),
    ('rlm', '\u200f'),
]


@pytest.mark.parametrize('label,char', EXTENDED_CONTROL_CHARS,
                         ids=[case[0] for case in EXTENDED_CONTROL_CHARS])
def test_sanitize_label_replaces_extended_control_classes(tmp_path, monkeypatch, label, char):
    """LOW 1: each class char becomes '_' in the review_method source label."""
    phase_key = f'demo:P{char}X'
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps({'phases': {phase_key: {'review_method': {'max_rounds': 8}}}}))
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(path))
    method = review_method(phase=phase_key)
    assert method.max_rounds == 8
    assert method.source == 'phases:demo:P_X'
    assert char not in method.source


@pytest.mark.parametrize('label,char', EXTENDED_CONTROL_CHARS,
                         ids=[case[0] for case in EXTENDED_CONTROL_CHARS])
def test_source_rejects_extended_control_classes(label, char):
    """LOW 1: ReviewMethod(source=...) rejects the same character classes."""
    from plangraph.settings import ReviewMethod, SettingsError
    with pytest.raises(SettingsError, match='control characters not allowed'):
        ReviewMethod(source=f'global{char}x')


def test_plan_phase_empty_settings_error_keeps_colon_prefix(tmp_path, monkeypatch):
    """LOW 2: SettingsError() with no detail still emits 'review_method_invalid:'."""
    import plangraph.settings as settings_mod
    from keeper_util import World, defn
    from plangraph.planner.phase import plan_phase
    from plangraph.keeper.errors import Refused

    def raise_empty(*args, **kwargs):
        raise settings_mod.SettingsError()

    monkeypatch.setattr(settings_mod, 'review_method', raise_empty)
    world = World(tmp_path / 'keeper')
    try:
        world.store.create_items(world.p('router'), [
            defn('P', kind='gate'),
            defn('GOAL', ['P'], kind='goal'),
        ])
        with pytest.raises(Refused) as exc:
            plan_phase(world.store, world.p('router'), 'demo:P', repo_root=tmp_path)
        assert exc.value.code == 'validation'
        assert exc.value.msg == 'review_method_invalid:'
        assert exc.value.msg.startswith('review_method_invalid:')
        assert 'review_method_invalid:' in str(exc.value)
    finally:
        world.close()


def test_r5_behaviour_r1_r2_and_no_scope_admitted(tmp_path, monkeypatch):
    """R1 root, R2 trusted item layers, and the by-design no-scope admission,
    as behaviour (replaces the R5 doc substring test, D-2784)."""
    from plangraph.settings import enforce_review_scope, fixer_threshold
    hostile = tmp_path / 'hostile'
    hostile.mkdir()
    (hostile / 'policy.json').write_text(
        '{"review_method": {"mode": "per-item", "fixer_split_threshold": 2}}')
    trusted = tmp_path / 'trusted'
    trusted.mkdir()
    (trusted / 'policy.json').write_text(json.dumps({
        'review_method': {'mode': 'batch', 'fixer_split_threshold': 12},
        'phases': {'demo:P': {'review_method': {'mode': 'per-item'}}},
    }))
    monkeypatch.setenv('PLANGRAPH_SETTINGS', 'policy.json')
    monkeypatch.setenv('PLANGRAPH_REPO_ROOT', str(trusted))
    card = {'key': 'demo:C', 'phase': 'demo:P', 'repo_root': str(hostile)}
    # R1: the settings root is never taken from the card.
    assert fixer_threshold() == 12
    assert fixer_threshold(card=card) == 12
    # R3: the card-selected per-item layer may not loosen the batch global.
    with pytest.raises(SpawnRefused, match='review_method_batch_per_fixer'):
        enforce_review_scope(card, 'review', 'per-fixer')
    # R2: the trusted keeper item may loosen; its phase layer is per-item.
    enforce_review_scope(card, 'review', 'per-fixer',
                         item={'key': 'demo:C', 'phase': 'demo:P'})
    # Finding 3 by design: a review spawn with no explicit scope is admitted
    # in batch -- only an explicit per-fixer scope is refused.
    enforce_review_scope(card, 'review', None,
                         item={'key': 'demo:C', 'phase': 'demo:P'})
    enforce_review_scope({'key': 'demo:C'}, 'review', None)


# D-2784 (R3): the layers a card's project/phase select are evaluated with the
# same root but may only tighten. These pin the round-6 findings: spawner and
# planner paths hold no keeper item, so tightening overrides were dropped and
# a per-fixer seat was admitted where policy said batch.


def test_card_layers_cannot_loosen_spoof(tmp_path, monkeypatch):
    """Spoof-loosen (D-2784): per-item/20 card layers never loosen batch/10."""
    from plangraph.settings import enforce_review_scope, fixer_threshold
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps({
        'review_method': {'mode': 'batch', 'fixer_split_threshold': 10},
        'projects': {'demo': {'review_method': {'mode': 'per-item',
                                                'fixer_split_threshold': 20}}},
        'phases': {'demo:P': {'review_method': {'mode': 'per-item',
                                                'fixer_split_threshold': 20}}},
    }))
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(path))
    for card in ({'key': 'demo:C', 'phase': 'demo:P'},
                 {'key': 'demo:C', 'project': 'demo'},
                 {'key': 'demo:P'}):
        with pytest.raises(SpawnRefused, match='review_method_batch_per_fixer'):
            enforce_review_scope(card, 'review', 'per-fixer')
        assert fixer_threshold(card=card) == 10


def test_card_phase_layer_batch_tightens_scope(tmp_path, monkeypatch):
    """Phase-tighten (D-2784): a card-selected batch phase layer refuses
    per-fixer under a per-item global; without the card layer it is admitted."""
    from plangraph.settings import enforce_review_scope
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps({
        'review_method': {'mode': 'per-item'},
        'phases': {'demo:P': {'review_method': {'mode': 'batch'}}},
    }))
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(path))
    with pytest.raises(SpawnRefused, match='review_method_batch_per_fixer'):
        enforce_review_scope({'key': 'demo:C', 'phase': 'demo:P'}, 'review', 'per-fixer')
    # The same spawn without the card layer (no phase on the card) is admitted.
    enforce_review_scope({'key': 'demo:C'}, 'review', 'per-fixer')
    # An explicit batch scope is the safe direction and stays admitted.
    enforce_review_scope({'key': 'demo:C', 'phase': 'demo:P'}, 'review', 'batch')


def test_card_threshold_min_only_tightens(tmp_path, monkeypatch):
    """Threshold (D-2784): fixer_threshold is min(trusted, card-selected)."""
    from plangraph.settings import fixer_threshold
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps({
        'review_method': {'fixer_split_threshold': 10},
        'phases': {'demo:low': {'review_method': {'fixer_split_threshold': 4}},
                   'demo:high': {'review_method': {'fixer_split_threshold': 20}}},
    }))
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(path))
    assert fixer_threshold() == 10
    assert fixer_threshold(card={'phase': 'demo:low'}) == 4
    # A card layer can never raise the threshold.
    assert fixer_threshold(card={'phase': 'demo:high'}) == 10
    # The min applies to both sides: trusted 4 beats card 20.
    assert fixer_threshold(item={'key': 'demo:x', 'phase': 'demo:low'},
                           card={'phase': 'demo:high'}) == 4


def test_production_path_card_phase_batch_refuses_per_fixer(tmp_path, monkeypatch):
    """The SpawnService admission path honours card tightening (D-2784):
    a card-selected batch phase layer refuses the per-fixer seat."""
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps({
        'review_method': {'mode': 'per-item'},
        'phases': {'demo:P': {'review_method': {'mode': 'batch'}}},
    }))
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(path))
    service = SpawnService(runner=object(), state=tmp_path / 'state')
    with pytest.raises(SpawnRefused) as caught:
        asyncio.run(service.spawn(card={'key': 'demo:C', 'phase': 'demo:P',
                                        'estimate_tokens': 1000},
                                  prompt='review task', role='review',
                                  review_scope='per-fixer'))
    assert caught.value.reason == 'review_method_batch_per_fixer'


def test_production_threshold_paths_pass_card(tmp_path, monkeypatch):
    """The threshold callers pass the card (D-2784): planner/routing.py
    plan_spawn and the SpawnService fixer dispatch honour the card-tightened
    threshold (12 findings split 3x4, not 2x6)."""
    from plangraph.planner.routing import plan_spawn
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps({
        'review_method': {'fixer_split_threshold': 6},
        'phases': {'demo:P': {'review_method': {'fixer_split_threshold': 4}}},
    }))
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(path))
    findings = [{'text': f'gap{i}', 'files': [f'src/f{i}.py']} for i in range(12)]
    card = {'key': 'demo:C', 'phase': 'demo:P', 'estimate_tokens': 1000,
            'findings': findings}
    plan = plan_spawn(card, role='implement')
    assert len(plan.writer_groups) == 3

    service = SpawnService(runner=object(), state=tmp_path / 'state')
    calls = []
    # Mirror SpawnService._spawn's signature: spawn() binds against it to split
    # findings, so a **kwargs fake would silently skip the dispatch path.
    async def fake_spawn(self, card=None, prompt='', role='implement', writer_families=(),
                         review_of=None, model=None, leg=None, override_reason=None,
                         dry_run=False, wait_seconds=0, no_fallthrough=False,
                         benchmark=None, require_bench=False, review_scope=None):
        calls.append(card)
        return {'status': 'stub'}
    monkeypatch.setattr(SpawnService, '_spawn', fake_spawn)
    asyncio.run(service.spawn(card=card, prompt='fix', role='implement'))
    assert len(calls) == 3
    assert all(len(child['findings']) == 4 for child in calls)


def test_card_layer_resolution_error_fails_closed(tmp_path, monkeypatch):
    """Fail rule (D-2784): an error resolving card layers refuses per-fixer
    (fail closed) and leaves the threshold at the trusted value."""
    from plangraph.settings import enforce_review_scope, fixer_threshold
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps({
        'review_method': {'mode': 'per-item', 'fixer_split_threshold': 10},
        'projects': {'demo': 'not-a-dict'},
    }))
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(path))
    with pytest.raises(SpawnRefused, match='review_method_batch_per_fixer'):
        enforce_review_scope({'key': 'demo:C'}, 'review', 'per-fixer')
    assert fixer_threshold(card={'key': 'demo:C'}) == 10
    # Cards that do not select the broken layer are unaffected.
    enforce_review_scope({'key': 'other:C'}, 'review', 'per-fixer')
    assert fixer_threshold(card={'key': 'other:C'}) == 10


def test_non_dict_item_refused(tmp_path, monkeypatch):
    """A non-dict item is refused, not silently dropped to global layers (D-2800)."""
    from plangraph.settings import (_item_layers, SettingsError,
                                    enforce_review_scope, fixer_threshold)
    # None (no keeper item) still yields the global layers.
    assert _item_layers(None) == (None, None)
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps({
        'review_method': {'mode': 'per-item', 'fixer_split_threshold': 10},
        'projects': {'demo': {'review_method': {'fixer_split_threshold': 5}}},
    }))
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(path))
    assert fixer_threshold(item=None) == 10
    assert fixer_threshold(item={'key': 'demo:1'}) == 5
    # Any other non-dict item is refused everywhere.
    for bad in ('demo:1', 7):
        with pytest.raises(SettingsError, match='item: expected a keeper item dict'):
            _item_layers(bad)
        with pytest.raises(SettingsError, match='item: expected a keeper item dict'):
            fixer_threshold(item=bad)
        with pytest.raises(SettingsError, match='item: expected a keeper item dict'):
            enforce_review_scope({}, 'review', 'per-fixer', item=bad)


def test_root_kwarg_beats_repo_root_env(tmp_path, monkeypatch):
    """root= wins over PLANGRAPH_REPO_ROOT for trusted and card layers."""
    from plangraph.settings import fixer_threshold
    env_root = tmp_path / 'envroot'
    env_root.mkdir()
    (env_root / 'policy.json').write_text(
        '{"review_method": {"fixer_split_threshold": 5}}')
    arg_root = tmp_path / 'argroot'
    arg_root.mkdir()
    (arg_root / 'policy.json').write_text(json.dumps({
        'review_method': {'fixer_split_threshold': 7},
        'phases': {'demo:P': {'review_method': {'fixer_split_threshold': 3}}},
    }))
    monkeypatch.setenv('PLANGRAPH_SETTINGS', 'policy.json')
    monkeypatch.setenv('PLANGRAPH_REPO_ROOT', str(env_root))
    assert fixer_threshold() == 5
    assert fixer_threshold(root=arg_root) == 7
    # Card layers are evaluated with the same root.
    assert fixer_threshold(root=arg_root, card={'phase': 'demo:P'}) == 3
    assert fixer_threshold(card={'phase': 'demo:P'}) == 5
