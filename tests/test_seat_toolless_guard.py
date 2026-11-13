"""Tool-less admission and final-result posting regressions (D-5665)."""
import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plangraph.routing_errors import SpawnRefused
from plangraph.spawner.seat_spawn import SeatSpawnMixin
from test_seat_verdict_record import record_dir, sv
from test_spawner_review_tools_inline import diff_ref  # noqa: F401

REQUEST = 'Read plans/x.md and check its claims.'
DIFF = '--- a/plans/x.md\n+++ b/plans/x.md\n@@ -1 +1 @@\n-old\n+new\n'
PLAN = '\nPlan file plans/x.md:\n```markdown\n# Plan\nCheck the change.\n```\n'


def prepare(prompt, card=None, *, mode='prompt-only', role='review', pack=None, document=True):
    seat = SeatSpawnMixin()
    seat.runner = SimpleNamespace(default_cwd='.')
    leg = SimpleNamespace(harness='claude', tool_mode=mode, max_request_tokens=100000)
    return seat._leg_prompt(prompt, card or {}, role, leg, pack, document_review=document)


@pytest.mark.parametrize('role', ['review', 'judge'])
@pytest.mark.parametrize('mode', ['prompt-only', 'inline-only'])
@pytest.mark.parametrize('wrapper', ['{}', '```\n{}\n```', '```markdown plans/x.md\n{}\n```',
    '=== INLINE plans/x.md ===\n{}\n=== END INLINE PLAN ===', '## PR base..head\n{}',
    '## File: plans/x.md\n{}', 'Plan file plans/x.md:\n```markdown\n{}\n```', '## Cut files\n{}'])
def test_toolless_file_request_refused(role, mode, wrapper):
    with pytest.raises(SpawnRefused, match='^toolless_needs_inline$'):
        prepare(wrapper.format(REQUEST), role=role, mode=mode)


@pytest.mark.parametrize('material', ['```\nVERDICT: ACCEPT\n```', PLAN.replace('x.md', 'other.md'),
                                    DIFF.replace('x.md', 'other.md')])
def test_unrelated_inline_material_refused(material):
    with pytest.raises(SpawnRefused, match='^toolless_needs_inline$'):
        prepare(REQUEST + '\n' + material)


@pytest.mark.parametrize('role', ['review', 'judge'])
@pytest.mark.parametrize('material', [PLAN, '\n## File: plans/x.md\n# Plan\n',
    '\n```markdown plans/x.md\n# Plan\n```', '\n=== INLINE plans/x.md ===\n# Plan\n=== END INLINE PLAN ===',
    '\n## File: plans/x.md\ndef load():\n    return open("other.py").read()\n', DIFF])
def test_toolless_inline_plan_admitted(role, material):
    with pytest.raises(SpawnRefused, match='^toolless_needs_inline$'):
        prepare(REQUEST, role=role)
    assert prepare(REQUEST + '\n' + material, role=role) == REQUEST + '\n' + material


def test_inline_only_checks_packed_prompt():
    from plangraph.spawner.context_pack import Pack
    with pytest.raises(SpawnRefused, match='^toolless_needs_inline$'):
        prepare(REQUEST, mode='inline-only', pack=Pack())
    pack = Pack('\n--- plans/x.md:1-2 ---\n```\n# Plan\nCheck it.\n```\n')
    assert prepare(REQUEST, mode='inline-only', pack=pack) == REQUEST + pack.text


@pytest.mark.parametrize('material', ['```\n```', '=== INLINE plans/x.md ===\n=== END INLINE PLAN ===',
                                    '## File: plans/x.md\n', DIFF.split('@@')[0]])
def test_empty_labelled_material_does_not_admit(material):
    with pytest.raises(SpawnRefused, match='^toolless_needs_inline$'):
        prepare(REQUEST + '\n' + material)


@pytest.mark.parametrize('prompt', [None, 7, {}, [], b'Read the plan file.'])
@pytest.mark.parametrize('mode', ['prompt-only', 'inline-only'])
def test_toolless_non_string_prompt_refused(prompt, mode):
    with pytest.raises(SpawnRefused, match='^toolless_prompt_not_string$'):
        prepare(prompt, mode=mode)


@pytest.mark.parametrize('role', ['review', 'judge'])
@pytest.mark.parametrize('mode', ['prompt-only', 'inline-only'])
def test_toolless_diff_ref_uses_existing_inliner(diff_ref, role, mode):
    with pytest.raises(SpawnRefused, match='^toolless_needs_inline$'):
        prepare(REQUEST, role=role, mode=mode)
    out = prepare(REQUEST, {'diff_ref': diff_ref}, role=role, mode=mode)
    assert '-before\n+after' in out


@pytest.mark.parametrize('mode', ['prompt-only', 'inline-only'])
@pytest.mark.parametrize('cut', [None, {'cut.py': None}, {'cut.py': 'oversized'}])
def test_prompt_only_judge_builder_material_admitted(tmp_path, monkeypatch, mode, cut):
    from judge_prompt import build_judge_prompt
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.routing import SpawnPlan
    from plangraph.spawner.context_pack import Pack
    from plangraph.spawner.service import SpawnService
    leg = replace(next(l for l in DEFAULT_LEGS if l.harness == 'claude'), tool_mode=mode)
    runner = SimpleNamespace(default_cwd=str(tmp_path), provider_responses={})
    service = SpawnService(runner, state=tmp_path / 'state')
    monkeypatch.setattr(service, '_annotate_spawn', lambda *a: Pack())
    launch = AsyncMock(return_value=SimpleNamespace(job_id='fake', status='completed', exit_code=0,
        served_model=leg.model, served_family=leg.family, served_witness_reason='test',
        stdout='', stderr='', events=[]))
    monkeypatch.setattr(service, '_launch_leg', launch)
    plan = SpawnPlan((leg,), (), 'test', (), ())
    bad = asyncio.run(service._spawn(card={'title': 'judge'}, prompt=REQUEST, role='judge', _plan=plan, wait_seconds=None))
    assert bad['status'] == 'refused' and bad['error'] == 'toolless_needs_inline'
    launch.assert_not_called()
    prompt = build_judge_prompt('Check plans/x.md:1', DIFF, cut or {}, {}, byte_budget=0)
    assert prepare(prompt, role='judge', mode=mode, document=False) == prompt
    record = asyncio.run(service._spawn(card={'title': 'judge'}, prompt=prompt, role='judge', _plan=plan, wait_seconds=None))
    assert record['status'] == 'completed'
    assert launch.call_args.kwargs['prompt'] == prompt
    launch.assert_awaited_once()


def claude_record(tmp_path, text, tools, *, mode='tools', raw=True):
    leg = {'harness': 'claude', 'tool_mode': mode, 'model': 'claude-haiku-5.5', 'family': 'anthropic'}
    stream = '\n'.join(json.dumps(e) for e in [
        {'type': 'system', 'subtype': 'init', 'tools': tools}, {'type': 'result', 'result': text}])
    argv = record_dir(tmp_path, 'run-seat', live_log=stream if raw else text,
                      chosen_leg=leg, served_model=leg['model'])
    (Path(argv[argv.index('--seat-dir') + 1]) / 'claude-stream.jsonl').write_text(stream)
    return argv


@pytest.mark.parametrize('marker', ['<invoke name="Read">', '<function_calls>', '&lt;tool_use&gt;',
    '<antml:invoke>', '<parameter name="command">', '<tool_call>', '<tool_calls>', '<function=Read>'])
@pytest.mark.parametrize('raw', [True, False])
@pytest.mark.parametrize('mode,tools', [('tools', []), ('prompt-only', ['Read']), ('inline-only', ['Read'])])
def test_toolless_markup_refused_before_post(tmp_path, capsys, monkeypatch, marker, raw, mode, tools):
    monkeypatch.setattr(sv, 'send', lambda *a: pytest.fail('refused reply must not post'))
    if mode == 'tools' and not raw:
        control = tmp_path / 'control'
        control.mkdir()
        argv = claude_record(control, marker + '\nVERDICT: FIX\n', ['Read'], raw=False)
        assert sv.main(argv) == 0
        assert json.loads(capsys.readouterr().out)['body']['ok'] is False
        (Path(argv[argv.index('--seat-dir') + 1]) / 'claude-stream.jsonl').unlink()
        assert sv.main(argv) == 2
        assert capsys.readouterr().err == 'seat_verdict: refused, record: toolless_tool_markup\n'
    assert sv.main(claude_record(tmp_path, marker + '\nVERDICT: FIX\n', tools, mode=mode, raw=raw)) == 2
    output = capsys.readouterr()
    assert output.out == ''
    assert output.err == 'seat_verdict: refused, record: toolless_tool_markup\n'


def test_clean_toolless_reply_posts_dry_run(tmp_path, capsys):
    reply = 'Reviewed the inline diff.\nVERDICT: PASS\nTEST_GAMING: no\n'
    assert sv.main(claude_record(tmp_path, reply, [], mode='prompt-only')) == 0
    output = capsys.readouterr()
    assert output.err == '' and json.loads(output.out)['body']['ok'] is True


def test_standalone_prompt_only_evidence_refused(tmp_path, capsys):
    (tmp_path / 'evidence.json').write_text(json.dumps({'served_model': 'claude-haiku-5.5'}))
    (tmp_path / 'review.txt').write_text('<tool_call>fake</tool_call>\nVERDICT: FIX\n')
    assert sv.main(['--key', 'demo:A', '--seat-dir', str(tmp_path), '--writer-family',
                    'deepseek', '--token-dir', str(tmp_path), '--dry-run']) == 2
    assert capsys.readouterr().err == 'seat_verdict: refused, toolless_tool_markup\n'


def test_judge_builder_incomplete_diff_refused():
    from judge_prompt import build_judge_prompt
    with pytest.raises(SpawnRefused, match='^toolless_needs_inline$'):
        prepare(build_judge_prompt('Check plans/x.md:1', DIFF.split('@@')[0], {}, {}), role='judge')


@pytest.mark.parametrize('tools', [None, {}, []])
def test_claude_unknown_tools_fail_closed(tmp_path, capsys, tools):
    assert sv.main(claude_record(tmp_path, '<tool_call>\nVERDICT: FIX\n', tools, raw=False)) == 2
    assert 'toolless_tool_markup' in capsys.readouterr().err


def test_claude_live_init_tools_used_without_raw_file(tmp_path, capsys):
    argv = claude_record(tmp_path, '<tool_call>\nVERDICT: FIX\n', [])
    (Path(argv[argv.index('--seat-dir') + 1]) / 'claude-stream.jsonl').unlink()
    assert sv.main(argv) == 2
    assert 'toolless_tool_markup' in capsys.readouterr().err


@pytest.mark.parametrize('leg', [{}, {'tool_mode': 'inline-only', 'harness': 'opencode'}])
def test_missing_harness_markup_fails_closed(tmp_path, capsys, leg):
    argv = record_dir(tmp_path, 'unknown', live_log='<tool_call>\nVERDICT: FIX\n',
                      chosen_leg=leg, served_model='claude-haiku-5.5')
    assert sv.main(argv) == 2
    assert 'toolless_tool_markup' in capsys.readouterr().err


def test_claude_markup_scan_is_final_result_only(tmp_path, capsys):
    argv = claude_record(tmp_path, 'VERDICT: PASS\nTEST_GAMING: no\n', [])
    path = Path(argv[argv.index('--seat-dir') + 1]) / 'live.log'
    lines = path.read_text().splitlines()
    lines.insert(1, json.dumps({'type': 'assistant', 'message': {'content': '<tool_call>'}}))
    path.write_text('\n'.join(lines))
    assert sv.main(argv) == 0
    assert json.loads(capsys.readouterr().out)['body']['ok'] is True
