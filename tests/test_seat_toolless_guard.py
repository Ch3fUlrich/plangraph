"""Hermetic SEAT-TOOLLESS-INLINE-GUARD admission and posting regressions."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from plangraph.routing_errors import SpawnRefused
from plangraph.spawner.seat_spawn import SeatSpawnMixin
from test_seat_verdict_record import record_dir, sv
from test_spawner_review_tools_inline import diff_ref  # noqa: F401

# Round-3 operator excerpts; reply joins the first complete Bash call/error and tail.
# Omit later calls containing an operator home path; do not reconstruct their text.
INCIDENT = json.loads((Path(__file__).parent / 'fixtures/seat_toolless_incident.json').read_text())
INCIDENT_PROMPT, INCIDENT_PATH = INCIDENT['prompt'], INCIDENT['path']
INCIDENT_REPLY = INCIDENT['reply_excerpt'] + '\n' + INCIDENT['reply_tail']
INLINE_PLAN = f'\nPlan file {INCIDENT_PATH}:\n```markdown\n# Plan\nCheck the change.\n```\n'
RAW_DIFF = ('\ndiff --git a/src/module.py b/src/module.py\n'
            'index 1111111..2222222 100644\n--- a/src/module.py\n+++ b/src/module.py\n'
            '@@ -1 +1 @@\n-return False\n+return True\n')


def test_recorded_incident_fixture_shape():
    # Pin the operator's round-3 excerpt, not a reconstruction of the unavailable run.
    assert hashlib.sha256(INCIDENT_PROMPT.encode()).hexdigest() == '91cb809835d766f7b80ab72cb21357b6721095c5a7acc3fd7e961b6cd9d9bdc5'
    assert hashlib.sha256(INCIDENT['reply_excerpt'].encode()).hexdigest() == '472bd91fdd7836c487293f57af54480afd9de6858c9e3d8e557399a775ef48c8'
    assert hashlib.sha256(INCIDENT['reply_tail'].encode()).hexdigest() == '16dd36c596b8f90cd5f3dfc5146ea45d88ebc387d53b16b51aaaf43abc238d8a'


def test_recorded_incident_public_fixture_scrubbed():
    from test_scrub_check import sc
    root = Path(__file__).resolve().parents[1]
    cfg = sc.build_config(rules_files=[str(root / 'tools/split/manifest.toml'),
                                      str(root / 'tools/split/scrub_ci.toml')])
    path = 'tests/fixtures/seat_toolless_incident.json'
    for line in (root / path).read_text().splitlines():
        for rule, values in sc.scan_line_values(line, cfg, sc._shape_finder()).items():
            assert not sc.pending_value_shas(path, rule, values, cfg), rule


def prepare(prompt, card=None, *, mode='prompt-only', role='review', harness='claude', pack=None):
    seat = SeatSpawnMixin()
    seat.runner = SimpleNamespace(default_cwd='.')
    leg = SimpleNamespace(harness=harness, tool_mode=mode, max_request_tokens=100000)
    return seat._leg_prompt(prompt, card or {}, role, leg, pack, document_review=True)


@pytest.mark.parametrize('role', ['review', 'judge'])
@pytest.mark.parametrize('mode', ['prompt-only', 'inline-only'])
@pytest.mark.parametrize('prompt', [INCIDENT_PROMPT, 'Read the plan file.',
                                   'Open `src/module.rs:12`.', 'Check config/settings.toml.'])
def test_toolless_file_request_refused(prompt, role, mode):
    with pytest.raises(SpawnRefused, match='^toolless_needs_inline$'):
        prepare(prompt, role=role, mode=mode)


@pytest.mark.parametrize('mode', ['prompt-only', 'inline-only'])
@pytest.mark.parametrize('material', ['```\nVERDICT: ACCEPT\n```',
    '```markdown other.md\n# Unrelated plan\n```',
    '=== INLINE other.md ===\n# Unrelated plan\n=== END INLINE PLAN ===',
    '## File: other.md\n# Unrelated plan\n', '## PR base..head\nVERDICT: ACCEPT\n',
    '--- a/other.md\n+++ b/other.md\n@@ -1 +1 @@\n-old\n+new\n'])
def test_unrelated_inline_material_refused(mode, material):
    with pytest.raises(SpawnRefused, match='^toolless_needs_inline$'):
        prepare('Read the plan file plans/x.md.\n' + material, mode=mode)


@pytest.mark.parametrize('role', ['review', 'judge'])
@pytest.mark.parametrize('path', ['plans/x.md', 'other.md'])
def test_inline_only_checks_packed_prompt(role, path):
    from plangraph.spawner.context_pack import Pack
    pack = Pack(f'\n=== CONTEXT PACK ===\n--- {path}:1-2 ---\n```\n# Plan\nCheck it.\n```\n=== END CONTEXT PACK ===\n')
    prompt = 'Read the plan file plans/x.md.'
    if path == 'plans/x.md':
        assert prepare(prompt, mode='inline-only', role=role, pack=pack) == prompt + pack.text
    else:
        with pytest.raises(SpawnRefused, match='^toolless_needs_inline$'):
            prepare(prompt, mode='inline-only', role=role, pack=pack)


@pytest.mark.parametrize('material', ['\n```\n```\n', '\n=== INLINE PLAN ===\n=== END INLINE PLAN ===',
                                    '\n```\nVERDICT: ACCEPT\n```\n'])
def test_empty_inline_material_does_not_admit(material):
    with pytest.raises(SpawnRefused, match='^toolless_needs_inline$'):
        prepare(INCIDENT_PROMPT + material)


@pytest.mark.parametrize('material', ['## File: plans/x.md\n',
    '=== INLINE plans/x.md ===\n=== END INLINE PLAN ===',
    '```plans/x.md\n\n```'])
def test_empty_labelled_material_does_not_admit(material):
    with pytest.raises(SpawnRefused, match='^toolless_needs_inline$'):
        prepare(material)


@pytest.mark.parametrize('role', ['review', 'judge'])
@pytest.mark.parametrize('material', [INLINE_PLAN, f'\n## File: {INCIDENT_PATH}\n# Plan\nCheck the change.\n',
    f'\n```markdown {INCIDENT_PATH}\n# Plan\n```\n',
    f'\n=== INLINE {INCIDENT_PATH} ===\n# Plan\n=== END INLINE PLAN ===\n'])
def test_toolless_inline_plan_admitted(role, material):
    prompt = INCIDENT_PROMPT + material
    assert prepare(prompt, role=role) == prompt


@pytest.mark.parametrize('role', ['review', 'judge'])
@pytest.mark.parametrize('material', [RAW_DIFF, RAW_DIFF.replace('--- a/src/module.py', '--- /dev/null'),
                                    RAW_DIFF.replace('+++ b/src/module.py', '+++ /dev/null'),
                                    RAW_DIFF[RAW_DIFF.index('--- a/'):],
                                    RAW_DIFF[RAW_DIFF.index('--- a/'):].replace('a/src/', 'src/').replace('b/src/', 'src/'),
                                    RAW_DIFF[RAW_DIFF.index('--- a/'):].replace('module.py\n', 'module.py\t2026-11-13 00:00:00 +0000\n')])
def test_toolless_raw_unified_diff_admitted(role, material):
    # run-a5f807cb used this unfenced diff --git / --- / +++ / @@ shape.
    prompt = INCIDENT_PROMPT + '\n' + material.replace('src/module.py', INCIDENT_PATH)
    assert prepare(prompt, role=role) == prompt


@pytest.mark.parametrize('material', [RAW_DIFF.split('@@')[0], RAW_DIFF.split('-return')[0],
                                    RAW_DIFF.split('index')[0], RAW_DIFF.split('--- a/')[1]])
def test_incomplete_raw_diff_does_not_admit(material):
    with pytest.raises(SpawnRefused, match='^toolless_needs_inline$'):
        prepare(INCIDENT_PROMPT + material.replace('src/module.py', INCIDENT_PATH))


@pytest.mark.parametrize('role', ['review', 'judge'])
@pytest.mark.parametrize('mode', ['prompt-only', 'inline-only'])
@pytest.mark.parametrize('prompt', [None, 7, {}, [], b'Read the plan file.'])
@pytest.mark.parametrize('card', [{}, {'diff_ref': {'head': 'unused'}}])
def test_toolless_non_string_prompt_refused(prompt, card, role, mode):
    with pytest.raises(SpawnRefused, match='^toolless_prompt_not_string$'):
        prepare(prompt, card, role=role, mode=mode)


@pytest.mark.parametrize('role', ['review', 'judge'])
@pytest.mark.parametrize('mode', ['prompt-only', 'inline-only'])
def test_toolless_diff_ref_uses_existing_inliner(diff_ref, role, mode):
    from plangraph.spawner.inline_diff import inline_diff_prompt
    assert prepare(INCIDENT_PROMPT, {'diff_ref': diff_ref}, role=role, mode=mode) == inline_diff_prompt(
        INCIDENT_PROMPT, diff_ref, max_chars=300000)


def test_raw_diff_content_is_not_a_file_request():
    material = RAW_DIFF.replace('+return True', '+return "read other.py"')
    prompt = 'Review src/module.py.\n' + material
    assert prepare(prompt) == prompt


def test_toolless_bad_diff_ref_still_refused(diff_ref):
    from plangraph.spawner.inline_diff import InlineDiffError
    with pytest.raises(InlineDiffError, match='git diff failed'):
        prepare(INCIDENT_PROMPT, {'diff_ref': dict(diff_ref, head='missing-revision')})


def test_prompt_only_judge_builder_material_admitted():
    from judge_prompt import build_judge_prompt
    prompt = build_judge_prompt('Check src/module.py:1', RAW_DIFF, {}, {})
    assert prepare(prompt) == prompt


def test_judge_builder_incomplete_diff_refused():
    from judge_prompt import build_judge_prompt
    prompt = build_judge_prompt('Check src/module.py:1', 'diff --git a/a.py b/a.py', {}, {})
    with pytest.raises(SpawnRefused, match='^toolless_needs_inline$'):
        prepare(prompt)


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
    (directory / 'prompt.txt').write_text(INCIDENT_PROMPT)
    if tools is not None:
        (directory / 'claude-stream.jsonl').write_text(stream)
    return argv


@pytest.mark.parametrize('marker', [INCIDENT_REPLY, '<invoke name="Bash">fake</invoke>',
                                   '<function_calls>fake</function_calls>', '&lt;invoke name="Read"&gt;',
                                   '&lt;parameter name="command"&gt;', '&lt;function_call&gt;',
                                   '&lt;tool_use&gt;', '<antml:invoke name="Read">',
                                   '<antml:function_calls>', '&lt;antml:tool_use&gt;',
                                   '<parameter name="command">', '<function_call>', '<tool_use>'])
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


def test_real_claude_tools_not_misclassified(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sv, 'guard_toolless_reply', lambda *a: pytest.fail('standalone-only guard'))
    assert sv.main(claude_record(tmp_path, INCIDENT_REPLY, ['Read'])) == 0
    assert json.loads(capsys.readouterr().out)['body']['ok'] is True


def test_missing_harness_explicit_tools_not_misclassified(tmp_path, capsys):
    leg = {'tool_mode': 'tools', 'model': 'opencode/mimo-v2.6-free'}
    assert sv.main(record_dir(tmp_path, 'tools-seat', live_log=INCIDENT_REPLY, chosen_leg=leg)) == 0
    assert json.loads(capsys.readouterr().out)['body']['ok'] is True


def test_claude_markup_scan_is_final_result_only(tmp_path, capsys):
    argv = claude_record(tmp_path, 'VERDICT: PASS\nTEST_GAMING: no\n', [])
    directory = Path(argv[argv.index('--seat-dir') + 1])
    path = directory / 'live.log'
    lines = path.read_text().splitlines()
    lines.insert(1, json.dumps({'type': 'assistant', 'message': {'content': INCIDENT_REPLY}}))
    path.write_text('\n'.join(lines) + '\n')
    assert sv.main(argv) == 0
    assert json.loads(capsys.readouterr().out)['body']['ok'] is True


@pytest.mark.parametrize('mode', ['prompt-only', 'inline-only'])
def test_claude_toolless_mode_overrides_init_tools(tmp_path, capsys, mode):
    assert sv.main(claude_record(tmp_path, INCIDENT_REPLY, ['Read'], mode=mode)) == 2
    output = capsys.readouterr()
    assert output.out == ''
    assert output.err == 'seat_verdict: refused, record: toolless_tool_markup\n'


@pytest.mark.parametrize('mode', ['prompt-only', 'inline-only'])
def test_known_toolless_harness_refused(tmp_path, capsys, mode):
    leg = {'harness': 'opencode', 'tool_mode': mode, 'model': 'opencode/mimo-v2.6-free'}
    assert sv.main(record_dir(tmp_path, 'opencode-seat', live_log=INCIDENT_REPLY,
                              chosen_leg=leg)) == 2
    assert 'toolless_tool_markup' in capsys.readouterr().err


@pytest.mark.parametrize('mode', ['tools', None])
@pytest.mark.parametrize('text', ['The guard matches `<function_calls>`.\nVERDICT: PASS\nTEST_GAMING: no\n',
                                 '<function_calls>quoted transcript</function_calls>\nVERDICT: FIX\n'])
def test_opencode_tools_mode_quoted_markup_not_misclassified(tmp_path, capsys, mode, text):
    leg = {'harness': 'opencode', 'model': 'opencode/mimo-v2.6-free'}
    if mode is not None:
        leg['tool_mode'] = mode
    assert sv.main(record_dir(tmp_path, 'opencode-seat', live_log=text, chosen_leg=leg)) == 0
    output = capsys.readouterr()
    assert output.err == ''
    assert json.loads(output.out)['body']['ok'] is ('VERDICT: PASS' in text)


@pytest.mark.parametrize('leg', [None, {}, [], 'unknown', {'model': 'claude-haiku-5.5'},
                               {'harness': None}, {'harness': ''}])
def test_missing_harness_markup_fails_closed(tmp_path, capsys, monkeypatch, leg):
    monkeypatch.setattr(sv, 'send', lambda *a: pytest.fail('unknown harness must not post'))
    argv = record_dir(tmp_path, 'unknown-seat', live_log=INCIDENT_REPLY,
                      chosen_leg=leg, served_model='claude-haiku-5.5')
    if leg is None:
        path = Path(argv[argv.index('--seat-dir') + 1]) / 'record.json'
        record = json.loads(path.read_text())
        del record['chosen_leg']
        path.write_text(json.dumps(record))
    assert sv.main(argv) == 2
    output = capsys.readouterr()
    assert output.out == ''
    assert output.err == 'seat_verdict: refused, record: toolless_tool_markup\n'


@pytest.mark.parametrize('marker', ['`<function_calls>`', '> <invoke_tool name="Read">',
                                    '```xml\n<invoke name="Read">\n```'])
def test_quoted_toolless_markup_known_false_positive(tmp_path, capsys, marker):
    text = marker + '\nVERDICT: FIX\n'
    assert sv.main(claude_record(tmp_path, text, [], mode='prompt-only')) == 2
    assert 'toolless_tool_markup' in capsys.readouterr().err


def test_standalone_prompt_only_evidence_refused(tmp_path, capsys):
    (tmp_path / 'evidence.json').write_text(json.dumps({
        'served_model': 'claude-haiku-5.5', 'finish_reason': 'stop', 'test_gaming': 'no'}))
    (tmp_path / 'review.txt').write_text(INCIDENT_REPLY)
    assert sv.main(['--key', 'demo:A', '--seat-dir', str(tmp_path), '--writer-family',
                    'deepseek', '--token-dir', str(tmp_path), '--dry-run']) == 2
    assert capsys.readouterr().err == 'seat_verdict: refused, toolless_tool_markup\n'
