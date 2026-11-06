"""Hermetic subprocess contract and plan-seat fall-through."""
import json
import os
from pathlib import Path
import subprocess

import pytest

from tools import plan_loop_spawn as spawn
from tools.plan_loop_bundle import Bundle
from tools.plan_loop_seats import State, load_policy
from plangraph.planner.legs import DEFAULT_LEGS


ACCEPT = '```plan-review\n```\nVERDICT: ACCEPT'
POLICY = load_policy(Path('tools/plan-loop.default.toml'))
LEGS = {leg.id: leg for leg in DEFAULT_LEGS}
LEG = LEGS['opencode-go-qwen3.8-max']


def record(text=ACCEPT, **extra):
    return dict(status='completed', attempts=[{'stdout_tail': text}],
                served_family=LEG.family, served_model=LEG.model,
                witness_kind='response', **extra)


def test_last_attempt_only():
    data = record()
    data['stdout_tail'] = ACCEPT
    data['attempts'] = [{'stdout_tail': ACCEPT}, {'stdout_tail': 'tool noise'}]
    assert spawn.read_record(data, LEG)['status'] == 'failed'


@pytest.mark.parametrize('text,reason', [
    (ACCEPT + '\n[... output truncated ...]', 'output_truncated'),
    ('', 'empty_reply'), ('tool noise', 'invalid_block'),
])
def test_bad_output(text, reason):
    assert spawn.read_record(record(text), LEG)['reason'] == reason


def test_witnesses():
    data = record()
    assert spawn.read_record(data, LEG)['witnessed']
    data['served_family'] = 'unwitnessed'
    assert not spawn.read_record(data, LEG)['witnessed']
    data.update(witness_kind='request-side-opencode-store', request_side_model=LEG.model)
    assert spawn.read_record(data, LEG)['witnessed']
    data['request_side_model'] = 'other'
    assert not spawn.read_record(data, LEG)['witnessed']
    data['served_family'] = 'gpt'
    assert spawn.read_record(data, LEG)['reason'] == 'served_family_mismatch'


@pytest.mark.parametrize('kind', ['confirm', 'r0'])
def test_exact_argv(tmp_path, monkeypatch, kind):
    monkeypatch.setenv('PLANGRAPH_CONTEXT_STRICT', '1')
    seen = {}
    def fake(argv, **kwargs):
        seen.update(argv=argv, **kwargs)
        return subprocess.CompletedProcess(argv, 0, json.dumps(record()))
    spawn.call_cli(tmp_path, kind, LEG, Bundle('secret text', 'sha', []), POLICY,
                   seat_id='seat', spawn=fake)
    role = 'fact-check' if kind == 'r0' else 'review'
    expected = [spawn.sys.executable, '-m', 'plangraph.spawner.cli', 'spawn',
                '--role', role, '--leg', LEG.id, '--override-reason', 'plan-loop.toml exact leg']
    if kind != 'r0':
        expected += ['--writer-family', POLICY.planner_family]
    expected += ['--prompt-file', 'seat.prompt.md', '--card', '@seat.card.json',
                 '--wait', str(POLICY.seat_max_min * 60), '--json']
    assert seen['argv'] == expected
    assert seen['cwd'] == tmp_path
    assert 'PLANGRAPH_CONTEXT_STRICT' not in seen['env']
    assert json.loads((tmp_path / 'seat.card.json').read_text()) == {
        'estimate_tokens': 4, 'max_min': POLICY.seat_max_min}


@pytest.mark.parametrize('reason', ['refused', 'limited', 'not_served_on_host',
                                  'harness_missing', 'opencode_tools_not_read_only'])
def test_prelaunch(reason):
    data = dict(status='failed', reason=reason, events=[
        {'event': 'render_refused', 'reason': reason}])
    if reason in {'refused', 'limited'}:
        data['status'] = reason
    assert not spawn.read_record(data, LEG)['attempt_used']


def test_third_failure_stalls(tmp_path):
    calls = []
    def fake(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess([], 0, json.dumps(record('noise')))
    result = spawn.run_seat('confirm', State(), POLICY, tmp_path,
                           lambda leg: Bundle('plan', 'sha', []), spawn=fake)
    assert result['status'] == 'stalled'
    assert result['failed_attempts'] == len(calls) == 3
    assert len({row['family'] for row in result['seats'] if row['attempt_used']}) == 3


def test_skip_and_fallthrough(tmp_path):
    calls = []
    def fake(argv, **kwargs):
        leg = LEGS[argv[argv.index('--leg') + 1]]
        calls.append(leg)
        data = record()
        data.update(served_family=leg.family, served_model=leg.model)
        return subprocess.CompletedProcess([], 0, json.dumps(data))
    def bundle(leg):
        return Bundle('plan', 'sha', [], 'over_budget' if not calls and leg.id == POLICY.leg[0].id else '')
    result = spawn.run_seat('confirm', State(), POLICY, tmp_path, bundle, spawn=fake)
    assert result['status'] == 'completed'
    assert result['failed_attempts'] == 0
    assert any(row['reason'] == 'over_budget' for row in result['seats'])


@pytest.mark.parametrize('material,hits,valid', [
    ('private', 0, False), ('public', None, True), ('scrubbed', 0, True),
    ('scrubbed', 1, False), ('scrubbed', None, False),
])
def test_training_exact_bytes(material, hits, valid):
    bundle = Bundle('plan', 'ignored', [], scrub_record={
        'bundle_sha': spawn.hashlib.sha256(b'plan').hexdigest(),
        'words_sha': 'words', 'hits': hits})
    assert spawn.training_allowed(bundle, material) == valid
    if material == 'scrubbed':
        bundle.text += 'changed'
        assert not spawn.training_allowed(bundle, material)


def test_fact_report():
    good = '```fact-check\nC1 | verified | claim | plan | source\n```\nFACTS: 1 verified, 0 false, 0 unverifiable'
    assert spawn.read_record(record(good), LEG, kind='r0')['status'] == 'completed'
    assert spawn.read_record(record('```fact-check\n```\nFACTS: 0 verified, 0 false, 0 unverifiable'), LEG, kind='r0')['reason'] == 'no_claims'


def test_real_cli_dry_run(tmp_path, monkeypatch):
    import plangraph
    root = str(Path(plangraph.__file__).resolve().parent.parent)
    monkeypatch.setenv('PYTHONPATH', root + os.pathsep + os.environ.get('PYTHONPATH', ''))
    monkeypatch.setenv('HOME', str(tmp_path))
    result = spawn.call_cli(tmp_path, 'confirm', LEG, Bundle('Review this plan.', 'sha', []),
                            POLICY, seat_id='dry', dry_run=True)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data['status'] == 'dry_run'
    assert 'diff_ref' not in json.loads((tmp_path / 'dry.card.json').read_text())


@pytest.mark.parametrize('status', ['failed', 'no_verdict', 'timed_out', 'content_blocked'])
def test_launched_failure(status):
    data = record()
    data['status'] = status
    row = spawn.read_record(data, LEG)
    assert row['status'] == 'failed' and row['attempt_used']


def test_exit_two_is_prelaunch():
    assert not spawn.read_record(record(), LEG, returncode=2)['attempt_used']


def test_high_discovery_never_launches_one_family(tmp_path):
    from dataclasses import replace
    policy = replace(POLICY, leg=tuple(item for item in POLICY.leg if item.id == LEG.id))
    result = spawn.run_seat('discovery', State(tier='high'), policy, tmp_path,
                           lambda leg: pytest.fail('must not build'),
                           spawn=lambda *a, **k: pytest.fail('must not launch'))
    assert result['status'] == 'stalled'
    assert result['failed_attempts'] == 0
