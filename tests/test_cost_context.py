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
    leg = SimpleNamespace(tool_mode='tools', max_request_tokens=200)
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
    assert cp.annotate({}, prompt, {}, root).warnings
    with pytest.raises(cp.PromptBoundError, match='prompt_too_large'):
        dispatch(seat, root, prompt, {}, SimpleNamespace(
            tool_mode='inline-only', max_request_tokens=200), calls)
    assert calls == []


@pytest.mark.parametrize('tool_mode', ['inline-only', 'tools'])
def test_launch_maps_strict_prompt_bound_before_runner(sandbox, monkeypatch, tool_mode):
    from plangraph.spawner import launch_spec
    root, seat, _ = sandbox
    monkeypatch.setenv('PLANGRAPH_CONTEXT_STRICT', '1')
    leg = SimpleNamespace(id='synthetic', roles=('implement',), tool_mode=tool_mode,
                          max_request_tokens=200, model='synthetic', harness='synthetic')
    monkeypatch.setattr(launch_spec, 'DEFAULT_LEGS', [leg])
    monkeypatch.setattr('plangraph.spawner.target.admit_leg', lambda leg: None)
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


@pytest.fixture
def review_payload(sandbox, monkeypatch):
    from plangraph.spawner import inline_diff
    root, seat, _ = sandbox
    diff = '+review change\n'
    monkeypatch.setattr(inline_diff.subprocess, 'run', lambda *a, **k:
                        subprocess.CompletedProcess(a, 0, stdout=diff, stderr=''))
    card = {'diff_ref': {'repo': str(root), 'base': 'base', 'head': 'head'}}
    framing = inline_diff.HEADER.format(base='base', head='head') + diff + inline_diff.FOOTER
    return root, seat, card, framing


@pytest.mark.parametrize('tool_mode', ['inline-only', 'tools'])
def test_strict_real_review_whole_prompt_refused(review_payload, monkeypatch, tool_mode):
    root, seat, card, framing = review_payload
    monkeypatch.setenv('PLANGRAPH_CONTEXT_STRICT', '1')
    leg = SimpleNamespace(tool_mode=tool_mode, max_request_tokens=200)
    prompt = 'private brief ' + 'x' * 601
    size = len(prompt) + (len(framing) if tool_mode == 'inline-only' else 0)
    calls = []
    with pytest.raises(cp.PromptBoundError) as exc:
        dispatch(seat, root, prompt, card, leg, calls, role='review')
    assert calls == []
    assert str(exc.value) == (f'prompt_too_large: inline_prompt_oversized: chars {size} '
                              f'estimated_tokens {(size + 2) // 3} max_request_tokens 200')


@pytest.mark.parametrize('tool_mode', ['inline-only', 'tools'])
def test_strict_real_review_at_bound_dispatched(review_payload, monkeypatch, tool_mode):
    root, seat, card, framing = review_payload
    monkeypatch.setenv('PLANGRAPH_CONTEXT_STRICT', '1')
    leg = SimpleNamespace(tool_mode=tool_mode, max_request_tokens=200)
    suffix = framing if tool_mode == 'inline-only' else ''
    prompt = 'x' * (600 - len(suffix))
    calls = []
    dispatch(seat, root, prompt, card, leg, calls, role='review')
    assert calls == [prompt + suffix]
    assert len(calls[0]) == 600


@pytest.mark.parametrize('enabled', [None, '0'])
@pytest.mark.parametrize('tool_mode', ['inline-only', 'tools'])
def test_disabled_real_oversized_review_matches_main(review_payload, monkeypatch, enabled, tool_mode):
    from plangraph.spawner.inline_diff import inline_diff_prompt
    root, seat, card, framing = review_payload
    if enabled is not None:
        monkeypatch.setenv('PLANGRAPH_CONTEXT_STRICT', enabled)
    prompt = 'x' * 1000
    leg = SimpleNamespace(tool_mode=tool_mode, max_request_tokens=200)
    suffix = framing if tool_mode == 'inline-only' else ''
    expected = (inline_diff_prompt(prompt, card['diff_ref'], max_chars=600)
                if tool_mode == 'inline-only' else prompt)
    calls = []
    assert expected == prompt + suffix
    with pytest.raises(cp.PromptBoundError, match='prompt_too_large'):
        dispatch(seat, root, prompt, card, leg, calls, role='review')
    assert calls == []


@pytest.mark.parametrize('enabled', [None, '0', '1'])
def test_real_oversized_diff_refusal(review_payload, monkeypatch, enabled):
    from plangraph.spawner import inline_diff
    root, seat, card, _ = review_payload
    if enabled is not None:
        monkeypatch.setenv('PLANGRAPH_CONTEXT_STRICT', enabled)
    monkeypatch.setattr(inline_diff.subprocess, 'run', lambda *a, **k:
                        subprocess.CompletedProcess(a, 0, stdout='private diff' * 100, stderr=''))
    leg = SimpleNamespace(tool_mode='inline-only', max_request_tokens=200)
    calls = []
    error = cp.PromptBoundError
    with pytest.raises(error) as exc:
        dispatch(seat, root, 'tiny', card, leg, calls, role='review')
    assert calls == []
    expected = 'prompt_too_large: inline_diff_oversized: chars 1200 estimated_tokens 400 max_request_tokens 200'
    assert str(exc.value) == expected


def test_launch_maps_real_oversized_diff_refusal(review_payload, monkeypatch):
    from plangraph.spawner import inline_diff, launch_spec
    root, seat, card, _ = review_payload
    monkeypatch.setenv('PLANGRAPH_CONTEXT_STRICT', '1')
    monkeypatch.setattr(inline_diff.subprocess, 'run', lambda *a, **k:
                        subprocess.CompletedProcess(a, 0, stdout='private diff' * 100, stderr=''))
    leg = SimpleNamespace(id='synthetic', roles=('review',), tool_mode='inline-only',
                          max_request_tokens=200, model='synthetic', harness='synthetic')
    monkeypatch.setattr(launch_spec, 'DEFAULT_LEGS', [leg])
    monkeypatch.setattr('plangraph.spawner.target.admit_leg', lambda leg: None)
    seat.state = root / 'state'
    seat.runner.spawn = lambda **kw: pytest.fail('runner invoked')
    monkeypatch.setattr(launch_spec, 'SpawnService', lambda **kw: seat)
    monkeypatch.setattr(launch_spec, '_sweep', lambda *a: None)
    request = dict(run_id='run-' + '0' * 32, job_id='synthetic', role='review',
                   dry_run=True, leg={'id': leg.id}, prompt='tiny', card=card,
                   cwd=str(root), state=str(seat.state))
    with pytest.raises(launch_spec.LaunchRefused) as exc:
        launch_spec.build(request)
    assert exc.value.kind == 'inline_diff_failed'
    assert str(exc.value) == (
        'prompt_too_large: inline_diff_oversized: chars 1200 estimated_tokens 400 max_request_tokens 200')


@pytest.mark.parametrize('tool_mode', ['tools', 'inline-only'])
@pytest.mark.parametrize('extra', [0, 1])
def test_strict_assembled_boundary(review_payload, monkeypatch, tool_mode, extra):
    root, seat, card, framing = review_payload
    monkeypatch.setenv('PLANGRAPH_CONTEXT_STRICT', '1')
    leg = SimpleNamespace(tool_mode=tool_mode, max_request_tokens=200)
    prompt = 'x' * (600 - (len(framing) if tool_mode == 'inline-only' else 0) + extra)
    calls = []
    if extra:
        with pytest.raises(cp.PromptBoundError) as exc:
            dispatch(seat, root, prompt, card, leg, calls, role='review')
        assert calls == []
        assert str(exc.value) == (
            'prompt_too_large: inline_prompt_oversized: chars 601 estimated_tokens 201 max_request_tokens 200')
    else:
        dispatch(seat, root, prompt, card, leg, calls, role='review')
        assert calls == [prompt + (framing if tool_mode == 'inline-only' else '')]


def test_structured_diff_refusal_ignores_wording(review_payload, monkeypatch):
    from plangraph.spawner import inline_diff
    root, seat, card, _ = review_payload
    monkeypatch.setenv('PLANGRAPH_CONTEXT_STRICT', '1')
    original = inline_diff.InlineDiffError.__init__

    def reword(self, message, **sizes):
        original(self, 'private changed diagnostic' if sizes else message, **sizes)

    monkeypatch.setattr(inline_diff.InlineDiffError, '__init__', reword)
    monkeypatch.setattr(inline_diff.subprocess, 'run', lambda *a, **k:
                        subprocess.CompletedProcess(a, 0, stdout='d' * 601, stderr=''))
    calls = []
    with pytest.raises(cp.PromptBoundError) as exc:
        dispatch(seat, root, 'tiny', card, SimpleNamespace(
            tool_mode='inline-only', max_request_tokens=200), calls, role='review')
    assert calls == []
    assert str(exc.value) == (
        'prompt_too_large: inline_diff_oversized: chars 601 estimated_tokens 201 max_request_tokens 200')
