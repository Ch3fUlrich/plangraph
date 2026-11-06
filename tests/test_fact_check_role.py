"""Hermetic fact-check role contracts."""
from plangraph.spawner.fact_check import parse_report
import asyncio
import dataclasses
import json
from pathlib import Path
import pytest
from plangraph.planner.legs import DEFAULT_LEGS, load_legs
from plangraph.planner.routing import plan_spawn
from plangraph.planner.route_policy import load_route_policy
from plangraph.spawner import fact_check as f

CARD = {'outcome': 'check claims', 'estimate_tokens': 1000, 'risk_tags': []}
GOOD = '```fact-check\nC1 | verified | claim | plan:1 | file:1 quote\n```\nFACTS: 1 verified, 0 false, 0 unverifiable'


def test_plan_spawn_order():
    plan = plan_spawn(CARD, role=f.ROLE)
    assert [leg.id for leg in plan.legs] == list(f.ORDER)
    assert not plan.review_seats
    from plangraph.planner.route_policy import RoutePolicy
    policy = RoutePolicy(banned_families=('kimi', 'deepseek'), off_families=('glm',),
                         review_allowed_families=('kimi', 'deepseek', 'glm'))
    assert [leg.id for leg in plan_spawn(CARD, role=f.ROLE, policy=policy).legs] == list(f.ORDER)
    kimi = dataclasses.replace(DEFAULT_LEGS[0], family='kimi', roles=(f.ROLE,))
    assert policy.skip_reason(kimi, f.ROLE)


@pytest.mark.parametrize('change', [{'trains_on_data': True}, {'model': 'opencode-go/muse-spark-1.3-contributor'}])
def test_training_leg_skipped(change):
    leg = dataclasses.replace(next(l for l in DEFAULT_LEGS if l.id == f.ORDER[0]), **change)
    plan = plan_spawn(CARD, role=f.ROLE, legs=(leg,))
    assert plan.skipped[0]['reason'] == 'training_leg'


@pytest.mark.parametrize('leg', ['opencode-go-muse-spark', 'opencode-deepseek-v4.1-flash-free'])
def test_hand_pick_training_leg_refused(tmp_path, capsys, leg):
    from plangraph.spawner.cli import main
    prompt = tmp_path / 'prompt.txt'
    prompt.write_text('check claims')
    card = tmp_path / 'card.json'
    card.write_text(json.dumps(CARD))
    assert main(['spawn', '--role', f.ROLE, '--leg', leg, '--card', '@' + str(card), '--prompt-file', str(prompt), '--dry-run']) != 0
    captured = capsys.readouterr()
    assert 'training_leg' in captured.out + captured.err


def test_loaders(tmp_path):
    leg = dataclasses.asdict(next(l for l in DEFAULT_LEGS if l.id == f.ORDER[0]))
    path = tmp_path / 'legs.json'
    path.write_text(json.dumps({'legs': [leg]}))
    assert f.ROLE in load_legs(path)[0].roles
    leg['roles'] = ['unknown']
    path.write_text(json.dumps({'legs': [leg]}))
    with pytest.raises(ValueError):
        load_legs(path)
    policy = tmp_path / 'policy.json'
    policy.write_text(json.dumps({'paused_harnesses': {'opencode': {'allowed_roles': [f.ROLE]}}}))
    load_route_policy(policy)
    policy.write_text(json.dumps({'paused_harnesses': {'opencode': {'allowed_roles': ['unknown']}}}))
    with pytest.raises(ValueError):
        load_route_policy(policy)


def test_cli_and_launch_role(tmp_path, capsys):
    from plangraph.spawner.cli import main
    from plangraph.spawner.launch_spec import _validate
    _validate(dict(run_id='run-' + '0' * 32, job_id='check', role=f.ROLE,
                   dry_run=True, leg={'id': f.ORDER[0]}, prompt='check', card=CARD,
                   cwd=str(tmp_path), state=str(tmp_path / 'state')))
    card = tmp_path / 'card.json'
    card.write_text(json.dumps(CARD))
    prompt = tmp_path / 'prompt.txt'
    prompt.write_text('check claims')
    assert main(['route', '--role', f.ROLE, '--card', '@' + str(card)]) == 0
    assert main(['spawn', '--role', f.ROLE, '--card', '@' + str(card), '--prompt-file', str(prompt), '--dry-run']) == 0
    assert f.ROLE in capsys.readouterr().out


def test_runner_read_only_and_no_edit(tmp_path, monkeypatch):
    from test_opencode_served_witness import _run_leg
    from plangraph.spawner.runner import _DENIED_TOOLS, _opencode_tools_denied
    checked = []
    original = f.read_only_tools_proven
    def proof(config, env):
        assert original(config, env)
        assert not _opencode_tools_denied(config, env)
        cfg = json.loads(config)
        for mutate in (lambda p: p.update(bash='allow'), lambda p: p.pop('question')):
            bad = json.loads(config)
            mutate(bad['permission'])
            assert not original(json.dumps(bad), env)
        assert not original('not json', env)
        assert cfg['permission'] == f.permission_map(_DENIED_TOOLS)
        checked.append(True)
        return True
    monkeypatch.setattr(f, 'read_only_tools_proven', proof)
    job, ctx = _run_leg(tmp_path, monkeypatch, lambda *a: None, role=f.ROLE,
                        extra_env={'OPENCODE_CLI_CONFIG_CONTENT': '{}'})
    cfg = json.loads(ctx['env']['OPENCODE_CONFIG_CONTENT'])
    assert {k for k, v in cfg['permission'].items() if v == 'allow'} == set(f.READ_ONLY_TOOLS)
    for key in ('bash', 'edit', 'webfetch', 'websearch', 'external_directory', 'question'):
        assert cfg['permission'][key] == 'deny'
    assert cfg['mcp'] == {} and cfg['plugin'] == []
    assert 'OPENCODE_CLI_CONFIG_CONTENT' not in ctx['env']
    assert not any(k.startswith('GIT_AUTHOR') for k in ctx['env'])
    from plangraph.spawner.git_guard import writer_launch
    assert writer_launch('check', f.ROLE, 'model', 'run-id') == ('check', {})
    assert job.status == 'completed'
    assert {'event': 'opencode_tools_read_only'} in job.events
    assert job.request_side_model is None and checked


def test_runner_refuses_unproven(tmp_path, monkeypatch):
    from plangraph.spawner.runner import SpawnerRunner
    monkeypatch.setattr(f, 'read_only_tools_proven', lambda *a: False)
    async def factory(*a, **kw):
        pytest.fail('must refuse before launch')
    runner = SpawnerRunner(default_cwd=str(tmp_path), process_factory=factory)
    job = asyncio.run(runner.spawn('check', model_or_combo='opencode/glm-5.3-flash-free', role=f.ROLE, client='opencode'))
    assert job.status == 'failed'
    assert {'event': 'render_refused', 'reason': 'opencode_tools_not_read_only'} in job.events


@pytest.mark.parametrize('text,reason', [
    ('', 'empty_text'), ('plain', 'no_block'), (GOOD + '\n' + GOOD, 'multiple_blocks'),
    (GOOD.replace('C1 | verified | claim | plan:1 | file:1 quote', 'C1 | verified'), 'field_count'),
    (GOOD.replace('C1 | verified', 'C1 | maybe'), 'invalid_state'),
    (GOOD.replace('C1 |', 'bad |'), 'invalid_id'),
    (GOOD.replace('```\nFACTS', 'C1 | verified | claim | plan:1 | evidence\n```\nFACTS'), 'duplicate_id'),
    (GOOD.replace('claim |', '<claim> |'), 'template_field'),
    (GOOD.replace('FACTS:', 'COUNTS:'), 'invalid_summary'),
    (GOOD.replace('1 verified', '2 verified'), 'count_mismatch'),
    ('```fact-check\n```\nFACTS: 0 verified, 0 false, 0 unverifiable', 'no_claims'),
])
def test_parse_refusals(text, reason):
    assert f.parse_report(text).reason == reason


def test_tail_hygiene_and_echo():
    from test_review_prompt import FORBIDDEN
    tail = Path(f.__file__).parents[1] / 'review' / 'tail_fact.md'
    text = tail.read_text()
    assert text.startswith('\n')
    assert 'test-gaming' not in text.lower()
    assert all(word not in text for word in FORBIDDEN)
    assert not f.parse_report(text).ok


def test_good_report():
    report = parse_report('```fact-check\nC1 | verified | claim | plan:1 | file:1 quote | extra\n```\nFACTS: 1 verified, 0 false, 0 unverifiable')
    assert report.ok
    assert report.claims[0].evidence == 'file:1 quote | extra'
