"""Hermetic SEAT-TOOLLESS-INLINE-GUARD admission and posting regressions."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from plangraph.routing_errors import SpawnRefused
from plangraph.spawner.seat_spawn import SeatSpawnMixin
from test_seat_verdict_record import record_dir, sv
from test_spawner_review_tools_inline import diff_ref  # noqa: F401

# Reproduction from the incident description, NOT an archived excerpt: the
# supplied run-0279165196ba directory is unavailable in this checkout's host.
INCIDENT_PROMPT = "Read the plan file plans/review.md and check src/module.py."
INCIDENT_REPLY = ('<invoke_tool name="Read">plans/review.md</invoke_tool>\n'
                  'error: Exception: No such file or directory\n'
                  'The plan could not be reviewed.\nVERDICT: PASS\nTEST_GAMING: no\n')
INLINE_PLAN = '\nPlan file plans/review.md:\n```markdown\n# Plan\nCheck the change.\n```\n'


def prepare(prompt, card=None, *, mode='prompt-only', role='review', harness='claude'):
    seat = SeatSpawnMixin()
    seat.runner = SimpleNamespace(default_cwd='.')
    leg = SimpleNamespace(harness=harness, tool_mode=mode, max_request_tokens=100000)
    return seat._leg_prompt(prompt, card or {}, role, leg, document_review=True)


@pytest.mark.parametrize('role', ['review', 'judge'])
@pytest.mark.parametrize('prompt', [INCIDENT_PROMPT, 'Read the plan file.',
                                   'Open `src/module.rs:12`.', 'Check config/settings.toml.'])
def test_toolless_file_request_refused(prompt, role):
    with pytest.raises(SpawnRefused, match='^toolless_needs_inline$'):
        prepare(prompt, role=role)


@pytest.mark.parametrize('material', ['\n```\n```\n', '\n=== INLINE PLAN ===\n=== END INLINE PLAN ==='])
def test_empty_inline_material_does_not_admit(material):
    with pytest.raises(SpawnRefused, match='^toolless_needs_inline$'):
        prepare(INCIDENT_PROMPT + material)


@pytest.mark.parametrize('role', ['review', 'judge'])
@pytest.mark.parametrize('material', [INLINE_PLAN, '\n## File: plans/review.md\n# Plan\nCheck the change.\n'])
def test_toolless_inline_plan_admitted(role, material):
    prompt = INCIDENT_PROMPT + material
    assert prepare(prompt, role=role) == prompt


@pytest.mark.parametrize('role', ['review', 'judge'])
def test_toolless_diff_ref_uses_existing_inliner(diff_ref, role):
    from plangraph.spawner.inline_diff import inline_diff_prompt
    assert prepare(INCIDENT_PROMPT, {'diff_ref': diff_ref}, role=role) == inline_diff_prompt(
        INCIDENT_PROMPT, diff_ref, max_chars=300000)


def test_toolless_bad_diff_ref_still_refused(diff_ref):
    from plangraph.spawner.inline_diff import InlineDiffError
    with pytest.raises(InlineDiffError, match='git diff failed'):
        prepare(INCIDENT_PROMPT, {'diff_ref': dict(diff_ref, head='missing-revision')})


def test_prompt_only_judge_builder_material_admitted():
    from judge_prompt import build_judge_prompt
    prompt = build_judge_prompt('Check src/module.py:1', 'diff --git a/a.py b/a.py', {}, {})
    assert prepare(prompt) == prompt


def test_tool_seat_and_non_review_unchanged():
    assert prepare(INCIDENT_PROMPT, mode='tools') == INCIDENT_PROMPT
    assert prepare(INCIDENT_PROMPT, role='implement') == INCIDENT_PROMPT
    assert prepare('Assess this supplied argument.') == 'Assess this supplied argument.'


def test_toolless_spawn_refuses_before_launch(tmp_path, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.routing import SpawnPlan
    from plangraph.spawner.context_pack import Pack
    from plangraph.spawner.service import SpawnService
    leg = next(l for l in DEFAULT_LEGS if l.harness == 'claude')
    runner = SimpleNamespace(default_cwd=str(tmp_path), spawn=AsyncMock())
    service = SpawnService(runner, state=tmp_path / 'state')
    monkeypatch.setattr(service, '_annotate_spawn', lambda *a: Pack())
    record = asyncio.run(service._spawn(card={'title': 'review'}, prompt=INCIDENT_PROMPT,
        role='review', _plan=SpawnPlan((leg,), (), 'test', (), ()), wait_seconds=None))
    assert record['status'] == 'refused' and record['exit_code'] == 2
    assert record['error'] == 'toolless_needs_inline'
    runner.spawn.assert_not_called()


def claude_record(tmp_path, text, tools, *, mode='tools', raw=True):
    leg = {'id': 'claude-seat', 'harness': 'claude', 'model': 'claude-haiku-5.5',
           'family': 'anthropic', 'tool_mode': mode}
    stream = json.dumps({'type': 'system', 'subtype': 'init', 'tools': tools}) + '\n'
    stream += json.dumps({'type': 'result', 'result': text}) + '\n'
    argv = record_dir(tmp_path, 'run-seat', live_log=stream if raw else text,
                      tail=text, chosen_leg=leg, served_model=leg['model'])
    directory = Path(argv[argv.index('--seat-dir') + 1])
    (directory / 'prompt.txt').write_text(INCIDENT_PROMPT + INLINE_PLAN)
    if tools is not None:
        (directory / 'claude-stream.jsonl').write_text(stream)
    return argv


@pytest.mark.parametrize('marker', [INCIDENT_REPLY, '<invoke name="Bash">fake</invoke>',
                                   '<function_calls>fake</function_calls>'])
@pytest.mark.parametrize('raw', [True, False])
def test_toolless_markup_refused_before_post(tmp_path, capsys, monkeypatch, marker, raw):
    monkeypatch.setattr(sv, 'send', lambda *a: pytest.fail('refused reply must not post'))
    text = marker if marker == INCIDENT_REPLY else marker + '\nVERDICT: FIX\n'
    assert sv.main(claude_record(tmp_path, text, [], raw=raw)) == 2
    output = capsys.readouterr()
    assert output.out == ''
    assert output.err == 'seat_verdict: refused, record: toolless_tool_markup\n'


@pytest.mark.parametrize('tools', [None, {}, []])
def test_claude_unknown_tools_fail_closed(tmp_path, capsys, tools):
    assert sv.main(claude_record(tmp_path, INCIDENT_REPLY, tools)) == 2
    assert 'toolless_tool_markup' in capsys.readouterr().err


def test_claude_live_init_tools_used_without_raw_file(tmp_path, capsys):
    argv = claude_record(tmp_path, INCIDENT_REPLY, [])
    directory = Path(argv[argv.index('--seat-dir') + 1])
    (directory / 'claude-stream.jsonl').unlink()
    assert sv.main(argv) == 2
    assert 'toolless_tool_markup' in capsys.readouterr().err


@pytest.mark.parametrize('text', ['Reviewed the inline diff.', 'The word "invoke" is prose.'])
@pytest.mark.parametrize('raw', [True, False])
def test_clean_toolless_reply_posts_dry_run(tmp_path, capsys, text, raw):
    reply = text + '\nVERDICT: PASS\nTEST_GAMING: no\n'
    assert sv.main(claude_record(tmp_path, reply, [], mode='prompt-only', raw=raw)) == 0
    output = capsys.readouterr()
    assert output.err == ''
    assert json.loads(output.out)['body']['ok'] is True


def test_real_claude_tools_not_misclassified(tmp_path, capsys):
    assert sv.main(claude_record(tmp_path, INCIDENT_REPLY, ['Read'])) == 0
    assert json.loads(capsys.readouterr().out)['body']['ok'] is True


def test_known_toolless_harness_refused(tmp_path, capsys):
    assert sv.main(record_dir(tmp_path, 'opencode-seat', live_log=INCIDENT_REPLY)) == 2
    assert 'toolless_tool_markup' in capsys.readouterr().err


def test_standalone_prompt_only_evidence_refused(tmp_path, capsys):
    (tmp_path / 'evidence.json').write_text(json.dumps({
        'served_model': 'claude-haiku-5.5', 'finish_reason': 'stop', 'test_gaming': 'no'}))
    (tmp_path / 'review.txt').write_text(INCIDENT_REPLY)
    assert sv.main(['--key', 'demo:A', '--seat-dir', str(tmp_path), '--writer-family',
                    'deepseek', '--token-dir', str(tmp_path), '--dry-run']) == 2
    assert capsys.readouterr().err == 'seat_verdict: refused, toolless_tool_markup\n'
