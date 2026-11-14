"""Operator 2026-11-14: retirement applies to requests, never record reads."""
import asyncio
import dataclasses
import json
import re
from pathlib import Path

import pytest

from plangraph.planner.legs import DEFAULT_LEGS, EXPLICIT_LEGS, WRITER_ORDER, load_legs
from plangraph.planner.routing import SpawnRefused, plan_spawn, validate_model
from plangraph.spawner.service import SpawnService

IDS = ('agy-gemini-3.8-flash', 'gemini-3.8-flash-medium',
       'omniroute/gemini-3.8-flash', 'vertex/gemini-2.5-flash', 'GEMINI-9-FLASH-high')
CAUSE = 'gemini_flash_retired.*retired by operator order.*agy-gemini-4-hydrogen'


@pytest.mark.parametrize('target', IDS)
def test_model_refused_even_with_catalog_admission(target):
    with pytest.raises(SpawnRefused, match=CAUSE):
        validate_model(target, catalog_rows=[dict(id=target, available=1, active=True)])


@pytest.mark.parametrize('target', IDS[:3])
@pytest.mark.parametrize('field', ['leg', 'model'])
def test_cli_refuses_retired_target(tmp_path, capsys, field, target):
    from plangraph.spawner.cli import main
    assert main(['--state', str(tmp_path), 'spawn', '--card', '{"estimate_tokens":1000}',
                 '--' + field, target, '--dry-run', '--override-reason', 'operator test override', 'task']) == 2
    captured = capsys.readouterr()
    assert re.search(CAUSE, captured.out + captured.err)


@pytest.mark.parametrize('target', IDS[:3])
@pytest.mark.parametrize('verb', ['spawn', 'route'])
@pytest.mark.parametrize('card_target', [False, True])
def test_mcp_retirement(tmp_path, verb, card_target, target):
    from plangraph.spawner.mcp import MCPSpawnerServer
    card = dict(estimate_tokens=1000)
    options = dict(card=card, prompt='task', dry_run=True, override_reason='operator test override')
    if card_target or verb == 'route':
        card['model'] = target
    else:
        options['model_or_combo'] = target
    with pytest.raises(SpawnRefused, match=CAUSE):
        asyncio.run(MCPSpawnerServer(state=tmp_path).call_tool('plangraph_agent_' + verb, options))


@pytest.mark.parametrize('field', ['id', 'model'])
def test_stored_leg_override_refused(tmp_path, field):
    row = dataclasses.asdict(DEFAULT_LEGS[0])
    row[field] = IDS[0]
    path = tmp_path / 'legs.json'
    path.write_text(json.dumps(dict(legs=[row])))
    with pytest.raises(ValueError, match=CAUSE):
        load_legs(path)


def test_guard_leg_catalog_and_configuration_data():
    from plangraph.catalog_data import SEED_MODELS
    from plangraph.routing_support.catalog_seed import SEED_ROUTE_LEGS
    from plangraph.routing_support.resolver import DEFAULT_FALLBACK_MODELS, get_catalog_models
    def flash(value):
        return re.search('gemini[^ ]*flash', str(value), re.I)
    assert not any(flash((l.id, l.model)) for l in (*DEFAULT_LEGS, *EXPLICIT_LEGS))
    # Retired catalog rows exist solely for benchmark foreign keys, never model lists.
    for row in SEED_MODELS:
        if flash(row):
            assert row['model_id'] == 'agy/gemini-3.8-flash-tiered-high'
            assert (row['status'], row['available']) == ('retired', 0)
            assert 'history' in row['unavailable_reason']
    assert not any(flash(r) for r in [*SEED_ROUTE_LEGS, *DEFAULT_FALLBACK_MODELS, *get_catalog_models()])
    root = Path(__file__).resolve().parents[1]
    for name in ('configuration/opencode/providers.json', 'deploy/omniroute/combos.json',
                 'deploy/omniroute/context-overrides.json'):
        data = json.loads((root / name).read_text())
        if name.endswith('combos.json'):
            data.pop('retired')  # Cleanup tombstones, never spawnable candidates.
        assert not flash(json.dumps(data)), name


def test_hydrogen_replaces_writer_position_without_inventing_admission():
    # D-5903 now authorizes the two registrations; telemetry remains unwitnessed.
    assert WRITER_ORDER[:2] == ('cx-gpt-6.1-sol-xhigh', 'agy-gemini-4-hydrogen-high')
    for effort, role in (('high', 'implement'), ('medium', 'review')):
        leg = next(l for l in DEFAULT_LEGS if l.id == 'agy-gemini-4-hydrogen-' + effort)
        assert leg.disabled is None and leg.roles == (role,)
        assert (leg.tier, leg.harness, leg.family, leg.effort) == ('subscription', 'agy', 'gemini', effort)
        assert (leg.windows, leg.max_request_tokens, leg.trains_on_data) == (('128k', '256k', '1m'), 900000, False)
        assert leg.tool_mode == 'tools'
        assert 'unwitnessed' in leg.note and 'D-5903' in leg.note


@pytest.mark.parametrize('writer', ['gpt', 'claude'])
def test_spawner_review_routes_hydrogen_in_retired_flash_position(tmp_path, monkeypatch, writer):
    from plangraph.routing_support import resolver
    monkeypatch.setattr(resolver, 'get_catalog_models', lambda: [])
    service = SpawnService(state=tmp_path, leg_reader=lambda *a, **kw: dict(state='available', evidence={}))
    card = dict(estimate_tokens=1000, diff_ref=dict(repo=str(tmp_path), base='HEAD~1', head='HEAD'))
    plan = service.route(card, role='review', writer_families=(writer,))
    ids = [leg.id for leg in plan.legs]
    assert 'agy-gemini-4-hydrogen-medium' in ids
    assert ids.index('mimo-v2.6-pro') < ids.index('agy-gemini-4-hydrogen-medium')
    assert ids.index('agy-gemini-4-hydrogen-medium') < ids.index('agy-gemini-4-argon')
    from plangraph.planner.legs import spawnable_legs
    from plangraph.planner.review_order import payment_order
    for tier, first in (('high', 'agy-gemini-4-hydrogen-medium'), ('standard', 'mimo-v2.6-pro')):
        fixed = payment_order(spawnable_legs(), 'review', order_class=tier,
                              writer_families=(writer,), diff_tokens=1000)
        assert fixed[0].id == first
    assert not any(re.search('gemini.*flash', leg.id + leg.model, re.I) for leg in plan.legs)
    implement = service.route(dict(estimate_tokens=1000), role='implement')
    assert [leg.id for leg in implement.legs[:2]] == list(WRITER_ORDER)


def test_hydrogen_prompt_uses_stdin_and_seat_stays_unwitnessed():
    from plangraph.routing_support.clients import build_standalone_fallback_command, launch_stdin_payload
    from plangraph.spawner.runner import JobInfo
    from plangraph.spawner.served_telemetry import native_models
    model = 'gemini-4-hydrogen-medium'
    prompt = 'inline diff ' * 20000  # Exceeds Linux's single-argument limit.
    cmd = build_standalone_fallback_command('agy', prompt, model, role='review', tool_mode='tools')
    assert prompt not in cmd and cmd[cmd.index('-p') + 1] == ''
    assert json.loads(launch_stdin_payload(cmd, prompt, 'agy'))['message']['content'] == prompt
    assert not native_models('agy', json.dumps(dict(type='result', model=model, text='VERDICT: PASS')))
    job = JobInfo('test', 'completed', prompt, model, role='review')
    assert (job.served_model, job.served_family, job.witness_kind) == ('unwitnessed', 'unwitnessed', None)


def test_old_record_still_loads(tmp_path):
    service = SpawnService(state=tmp_path)
    path = service._path('run-historical')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(run_id='run-historical', model=IDS[0], status='completed')))
    assert service.result('run-historical')['model'] == IDS[0]


def test_stale_database_cannot_restore_mcp_models(tmp_path):
    from plangraph.routing_support.resolver import get_catalog_models, catalog_row_admitted
    class OldCatalog:
        def list_models(self, **kwargs):
            return [dict(id=IDS[0], model_id=IDS[0], available=1, active=True,
                         status='active', family='gemini')]
    assert get_catalog_models(OldCatalog()) == []
    assert not catalog_row_admitted(dict(id=IDS[0], available=1, active=True))


@pytest.mark.parametrize('target', IDS)
def test_command_and_runner_boundaries_refuse(target, tmp_path):
    from plangraph.routing_support.clients import build_command, build_opencode_command
    from plangraph.spawner.runner import SpawnerRunner
    for call in (lambda: build_command('task', target),
                 lambda: build_opencode_command('task', target),
                 lambda: asyncio.run(SpawnerRunner().spawn('task', target, dry_run=True))):
        with pytest.raises(SpawnRefused, match=CAUSE):
            call()


def test_fixed_order_cannot_hide_retired_candidate():
    from plangraph.planner.review_order import payment_order
    leg = dataclasses.replace(DEFAULT_LEGS[0], id='agy-gemini-4-hydrogen-medium',
                              model='gemini-2.5-flash', roles=('review',))
    with pytest.raises(SpawnRefused, match=CAUSE):
        payment_order((leg,), 'review', order_class='high', writer_families=('gpt',))


@pytest.mark.parametrize('target', ['deepseek/deepseek-v4-flash-max', 'mimo/mimo-v2.6-flash',
                                  'opencode-go/qwen3.8-flash', 'opencode/glm-5.3-flash-free'])
def test_other_flash_families_remain_admissible(target):
    assert validate_model(target, catalog_rows=[dict(id=target, available=1, active=True)]) == target


def test_deployment_prunes_removed_combos_instead_of_leaving_stale_routes():
    root = Path(__file__).resolve().parents[1]
    data = json.loads((root / 'deploy/omniroute/combos.json').read_text())
    assert {'gemini-3.8-flash', 'vertex-gemini-3.8-flash'} <= set(data['retired'])
    assert {'l1-orchestrator-clean', 'l2-orchestrator-clean', 'l2-worker-clean',
            'l3-driver-clean'} <= set(data['omitted'])
    pruned = set(data['retired']) | set(data['omitted'])
    assert not pruned & {row['name'] for row in data['combos']}
    assert not pruned & set(data['d657_combos'])



def test_no_diff_review_keeps_hydrogen_when_other_seats_unavailable():
    # Operator round 3 explicitly requires no-diff reviews to keep this tools seat;
    # like other agy tools seats, it reads the selected working tree in plan mode.
    def reader(model, **kwargs):
        unavailable = any(name in model for name in ('mimo', 'opencode', 'muse', 'deepseek'))
        return dict(state='exhausted' if unavailable else 'available', evidence={})
    plan = plan_spawn(dict(estimate_tokens=1000), role='review',
                      writer_families=('gpt', 'claude'), leg_reader=reader, catalog_rows=[])
    assert plan.error is None
    assert 'agy-gemini-4-hydrogen-medium' in [leg.id for leg in plan.legs]


@pytest.mark.parametrize('field', ['leg', 'chosen_leg', 'legs', 'override'])
def test_nested_leg_object_refused(field):
    from plangraph.retired_models import check_retired
    leg = dataclasses.replace(DEFAULT_LEGS[0], model='gemini-3.8-flash-high')
    with pytest.raises(SpawnRefused, match=CAUSE):
        check_retired({field: [leg]})


@pytest.mark.parametrize('separator', [' ', '\t', '\n', '_', '/', ':', '.'])
def test_retired_separator_variants_refused_before_target_resolution(separator):
    from plangraph.spawner.target import resolve_target
    with pytest.raises(SpawnRefused, match=CAUSE):
        resolve_target('agy/GEMINI-3.8' + separator + 'FLASH')


@pytest.mark.parametrize('role', ['review', 'implement'])
@pytest.mark.parametrize('model', ['gemini-4-hydrogen-medium', 'Gemini-4-Hydrogen-medium'])
def test_agy_tools_command_uses_plan_only_for_review(role, model):
    from plangraph.routing_support.clients import build_standalone_fallback_command
    cmd = build_standalone_fallback_command('agy', 'review', model,
                                            role=role, tool_mode='tools')
    if role == 'review':
        assert cmd[cmd.index('--mode') + 1] == 'plan'
        assert '--dangerously-skip-permissions' not in cmd
    else:
        assert '--mode' not in cmd
        assert '--dangerously-skip-permissions' in cmd


def test_hydrogen_tools_review_launches_in_head_checkout(tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace
    from plangraph.spawner.runner import SpawnerRunner
    repo = tmp_path / 'checkout'
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
    git('init', '-q')
    (repo / 'marker.txt').write_text('head marker\n')
    git('add', 'marker.txt')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'head')
    head = git('rev-parse', 'HEAD')
    calls = []
    async def factory(*args, **kwargs):
        calls.append((args, kwargs))
        assert kwargs['cwd'] == str(repo) and kwargs['env']['PWD'] == str(repo)
        assert git('rev-parse', 'HEAD') == head
        return SimpleNamespace(pid=None, returncode=0)
    async def collect(*args):
        return json.dumps(dict(event='result', result=dict(status='SUCCESS',
                          response='VERDICT: ACCEPT\nTEST_GAMING: none'))).encode(), b''
    async def ready(*args, **kwargs):
        pass
    async def launch(plan, **kwargs):
        return plan
    monkeypatch.setattr('plangraph.spawner.run_watchdog.collect', collect)
    monkeypatch.setattr('plangraph.spawner.resolver.host_skip_reason_async', ready)
    monkeypatch.setattr('plangraph.spawner.resolver.plan_for_launch_async', launch)
    monkeypatch.setattr('plangraph.spawner.leg_probe.before_launch', ready)
    runner = SpawnerRunner(default_cwd=str(repo), process_factory=factory)
    monkeypatch.setattr(runner, '_reap_process', ready)
    service = SpawnService(runner=runner, state=tmp_path / 'state')
    record = asyncio.run(service.spawn(dict(estimate_tokens=1000), 'Read marker.txt and review',
                        role='review', writer_families=('gpt', 'claude'),
                        leg='agy-gemini-4-hydrogen-medium',
                        override_reason='hermetic checkout review test', wait_seconds=None))
    assert len(calls) == 1
    cmd = calls[0][0]
    assert Path(cmd[0]).name == 'agy' and cmd[cmd.index('--mode') + 1] == 'plan'
    assert '--dangerously-skip-permissions' not in cmd
    assert record['status'] == 'completed', json.dumps(record)
    job = runner.jobs[record['job_id']]
    assert job.task_prompt == 'Read marker.txt and review'
    assert (job.served_model, job.served_family, job.witness_kind) == ('unwitnessed', 'unwitnessed', None)



@pytest.mark.parametrize('model', ['claude-opus-5-5-high', 'claude-sonnet-5-5-medium',
                                   'gpt-oss-120b-medium', 'gemini-4-argon-high'])
def test_other_agy_review_commands_keep_existing_permissions(model):
    from plangraph.routing_support.clients import build_command
    cmd = build_command('review', model, role='review', client='agy')
    assert cmd[:2] == ['agy', '--dangerously-skip-permissions']
    assert '--mode' not in cmd
    assert cmd[cmd.index('--model') + 1] == model
