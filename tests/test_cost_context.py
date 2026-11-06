"""Synthetic admission replay; character/3 estimates are not measured token use."""
from types import SimpleNamespace
import builtins
import io
import socket
import subprocess
import time

import pytest

from plangraph.spawner import context_pack as cp
from plangraph.spawner.seat_spawn import SeatSpawnMixin


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, 'socket', lambda *a, **k: pytest.fail('network denied'))
    monkeypatch.setattr(time, 'time', lambda: 0)
    monkeypatch.setattr(time, 'monotonic', lambda: 0)
    monkeypatch.setattr(cp.os, 'environ', {})
    real_open = builtins.open

    def guarded_open(path, *args, **kwargs):
        from pathlib import Path
        assert Path(path).resolve().is_relative_to(tmp_path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, 'open', guarded_open)
    monkeypatch.setattr(io, 'open', guarded_open)
    monkeypatch.setattr(cp.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(
        a, 0, stdout=b'large.py\0', stderr=b''))
    lines = ['# filler'] * 1000
    lines[100:130] = ['def requested():'] + ['    pass'] * 29
    (tmp_path / 'large.py').write_text('\n'.join(lines) + '\n')
    seat = SeatSpawnMixin()
    seat.runner = SimpleNamespace(default_cwd=str(tmp_path))
    return tmp_path, seat, lines


def dispatch(seat, root, prompt, card, leg, calls, role='implement'):
    record = {}
    pack = cp.annotate(record, prompt, card, root)
    calls.append(seat._leg_prompt(prompt, card, role, leg, pack))
    return record


@pytest.mark.parametrize('enabled', [None, '0', '1'])
def test_strict_before_runner_and_disabled_unchanged(sandbox, monkeypatch, enabled):
    root, seat, _ = sandbox
    if enabled is not None:
        monkeypatch.setenv('PLANGRAPH_CONTEXT_STRICT', enabled)
    calls = []
    leg = SimpleNamespace(tool_mode='tools')
    if enabled == '1':
        with pytest.raises(cp.ContextError, match='^big_file_unsliced: count 1$'):
            dispatch(seat, root, 'edit large.py', {}, leg, calls)
        assert calls == []
    else:
        record = dispatch(seat, root, 'edit large.py', {}, leg, calls)
        assert calls == ['edit large.py']
        assert record['warnings'] == [
            'big_file_unsliced: large.py 1000 lines; name one function per slice in card.context']


def test_total_bound_including_original_and_review_framing(sandbox, monkeypatch):
    root, seat, _ = sandbox
    monkeypatch.setenv('PLANGRAPH_CONTEXT_STRICT', '1')
    card = {'context': ['large.py:101-130']}
    pack = cp.annotate({}, 'edit large.py', card, root)
    leg = SimpleNamespace(tool_mode='inline-only', max_request_tokens=200)
    out = seat._leg_prompt('edit large.py', card, 'implement', leg, pack)
    assert len(out) <= 600 and 'def requested():' in out
    with pytest.raises(cp.ContextError, match='inline_prompt_oversized: chars 1000 estimated_tokens 334 max_request_tokens 200'):
        seat._leg_prompt('x' * 1000, {}, 'implement', leg, cp.Pack())
    from plangraph.spawner import inline_diff
    monkeypatch.setattr(inline_diff, 'inline_diff_prompt', lambda p, *a, **k: p + 'd' * 600)
    with pytest.raises(cp.ContextError, match='inline_prompt_oversized'):
        seat._leg_prompt('x', {}, 'review', leg, cp.Pack())


@pytest.mark.parametrize('enabled', [None, '0'])
def test_inline_bound_disabled_preserves_prompt_and_warning(sandbox, monkeypatch, enabled):
    root, seat, _ = sandbox
    if enabled is not None:
        monkeypatch.setenv('PLANGRAPH_CONTEXT_STRICT', enabled)
    prompt = 'edit large.py\n' + 'x' * 1000
    calls = []
    record = dispatch(seat, root, prompt, {}, SimpleNamespace(
        tool_mode='inline-only', max_request_tokens=200), calls)
    assert calls == [prompt]
    assert record['warnings'] == [
        'big_file_unsliced: large.py 1000 lines; name one function per slice in card.context']


def test_launch_maps_strict_prompt_bound_before_runner(sandbox, monkeypatch):
    from plangraph.spawner import launch_spec
    root, seat, _ = sandbox
    monkeypatch.setenv('PLANGRAPH_CONTEXT_STRICT', '1')
    leg = SimpleNamespace(id='synthetic', roles=('implement',), tool_mode='inline-only',
                          max_request_tokens=200, model='synthetic', harness='synthetic')
    monkeypatch.setattr(launch_spec, 'DEFAULT_LEGS', [leg])
    monkeypatch.setattr(launch_spec, 'leg_active', lambda leg: True)
    seat.state = root / 'state'
    seat.runner.spawn = lambda **kw: pytest.fail('runner invoked')
    monkeypatch.setattr(launch_spec, 'SpawnService', lambda **kw: seat)
    monkeypatch.setattr(launch_spec, '_sweep', lambda *a: None)
    monkeypatch.setattr(launch_spec, 'writer_launch', lambda p, *a: (p, {}))
    request = dict(run_id='run-' + '0' * 32, job_id='synthetic', role='implement',
                   dry_run=True, leg={'id': leg.id}, prompt='x' * 1000, card={},
                   cwd=str(root), state=str(seat.state))
    with pytest.raises(launch_spec.LaunchRefused, match='inline_prompt_oversized') as exc:
        launch_spec.build(request)
    assert exc.value.kind == 'inline_diff_failed'
    request.update(prompt='edit large.py')
    with pytest.raises(launch_spec.LaunchRefused, match='big_file_unsliced: count 1'):
        launch_spec.build(request)


@pytest.mark.parametrize('enabled', [None, '0', '1'])
def test_listing_failure_stays_advisory_without_slices(sandbox, monkeypatch, enabled):
    root, _, _ = sandbox
    if enabled is not None:
        monkeypatch.setenv('PLANGRAPH_CONTEXT_STRICT', enabled)

    def unavailable(root):
        raise cp.ContextError('tracked_listing_unavailable')

    monkeypatch.setattr(cp, 'tracked', unavailable)
    pack = cp.build('edit large.py', {}, root)
    assert pack.text == ''
    assert pack.warnings == ('big_file_scan_skipped: tracked_listing_unavailable',)
    with pytest.raises(cp.ContextError, match='tracked_listing_unavailable'):
        cp.build('edit large.py', {'context': ['large.py:101-130']}, root)


def test_cost_context_changelog_is_one_line():
    from pathlib import Path
    fragment = Path(__file__).resolve().parents[1] / 'changelog.d' / (
        'SPAWNER-COST-TIME-D3605-' + 'cost-context.md')
    lines = fragment.read_text().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith('- **SPAWNER-COST-TIME-D3605:**')
    assert 'Whole-inline-prompt bounds' in lines[0]
    assert 'refused dispatch (unsliced and oversized)' in lines[0]


def test_synthetic_replay_exact_reductions(sandbox, monkeypatch):
    root, seat, lines = sandbox
    monkeypatch.setenv('PLANGRAPH_CONTEXT_STRICT', '1')
    leg = SimpleNamespace(tool_mode='inline-only', max_request_tokens=200)
    before = 'edit large.py\n' + '\n'.join(lines) + '\n'
    calls = []
    with pytest.raises(cp.ContextError, match='big_file_unsliced'):
        dispatch(seat, root, before, {}, leg, calls)
    assert calls == []  # one refused dispatch (unsliced and oversized)
    dispatch(seat, root, 'edit large.py', {'context': ['large.py:101-130']}, leg, calls)
    assert len(calls) == 1  # zero refusals with the explicit function range
    after = calls[0]
    assert '\n'.join(lines[100:130]) in after
    assert '# filler' not in after
    assert (len(before), len(after)) == (9022, 372)
    assert (len(before.encode()), len(after.encode())) == (9022, 372)
    assert len(before) - len(after) == 8650
    assert ((len(before) + 2) // 3, (len(after) + 2) // 3) == (3008, 124)
