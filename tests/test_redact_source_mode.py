"""D-5250: only spawner-read source gets the assignment-rule exception."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from plangraph.spawner import context_pack, env_redact, inline_diff, runner, run_watchdog
from plangraph.spawner.seat_spawn import SeatSpawnMixin
from plangraph.spawner.service import SpawnService


CODE = '    pair_' + 'key = tuple(sorted((left, right)))\n    lookup_' + 'key = (scope, name)\n'


def tokens():
    return ['sk' + '-synthetic123', 'xsk' + '-synthetic456',
            'Bear' + 'er synthetic789', 'db' + '://u:synthetic-pass@example.invalid/x']


def pem_block(complete):
    text = '-----BEGIN ' + 'TEST PRIVATE KEY-----\nsynthetic-pem-body\n'
    return text + ('-----END ' + 'TEST PRIVATE KEY-----\n' if complete else '')


class Seat(SeatSpawnMixin):
    def __init__(self, cwd):
        self.runner = SimpleNamespace(default_cwd=str(cwd))


def diff_prompt(monkeypatch, tmp_path, caller='review', pack=None, inline=False, extra=''):
    diff = '+change\n' + ''.join('+' + line + '\n' for line in CODE.splitlines())
    diff += ''.join('+' + value + '\n' for value in tokens())
    diff += ''.join('+' + line + '\n' for line in extra.splitlines())
    monkeypatch.setattr(inline_diff.subprocess, 'run', lambda *a, **k:
                        SimpleNamespace(stdout=diff, stderr='', returncode=0))
    leg = SimpleNamespace(tool_mode='inline-only' if inline else 'tools',
                          harness='opencode', max_request_tokens=100000)
    card = {'diff_ref': {'repo': str(tmp_path), 'base': 'main', 'head': 'topic'}}
    return Seat(tmp_path)._leg_prompt(caller, card, 'review', leg, pack)


def capture(monkeypatch, tmp_path, prompt, role='review'):
    sent = []
    def dispatch(**kwargs):
        sent.append(kwargs['prompt'])
        raise RuntimeError('captured')
    monkeypatch.setattr(runner, 'build_command', dispatch)
    path = tmp_path / 'prompt.txt'
    with pytest.raises(RuntimeError, match='captured'):
        asyncio.run(runner.SpawnerRunner().spawn(prompt=prompt, role=role,
            prompt_record=(path, {}, lambda record: None)))
    assert path.read_text() == sent[0]
    from plangraph.spawner.prompt_input import compose
    prefix = compose('', role, None, None)  # role header and skill precede the caller text
    assert sent[0].startswith(prefix)
    return sent[0][len(prefix):]


def test_computed_assignments_readable_in_spawner_diff(monkeypatch, tmp_path):
    out = capture(monkeypatch, tmp_path, diff_prompt(monkeypatch, tmp_path))
    for line in CODE.splitlines():
        assert '+' + line in out


def test_token_shapes_still_masked_in_source_diff(monkeypatch, tmp_path):
    out = capture(monkeypatch, tmp_path, diff_prompt(monkeypatch, tmp_path))
    assert all(value not in out for value in tokens())
    assert 'Bearer [REDACTED]' in out and 'u:[REDACTED]@example.invalid/x' in out


@pytest.mark.parametrize('complete', [True, False])
@pytest.mark.parametrize('in_diff', [True, False])
def test_private_key_masked_in_final_review_prompt_and_record(monkeypatch, tmp_path, complete, in_diff):
    block = pem_block(complete)
    prompt = diff_prompt(monkeypatch, tmp_path, extra=block) if in_diff else block
    out = capture(monkeypatch, tmp_path, prompt)
    assert 'PRIVATE KEY' not in out and 'synthetic-pem-body' not in out
    assert '[REDACTED]' in out
    if in_diff:
        assert '+[REDACTED]' in out
        assert '+    pair_key = tuple(sorted((left, right)))' in out
    else:
        assert out == '[REDACTED]' + ('\n' if complete else '')


@pytest.mark.parametrize('framed', [False, True])
def test_caller_text_and_imitated_diff_framing_fully_scrubbed(monkeypatch, tmp_path, framed):
    caller = CODE
    if framed:
        caller = inline_diff.HEADER.format(base='main', head='topic') + caller + inline_diff.FOOTER
        caller += '\n=== CONTEXT PACK ===\n' + CODE + '\n=== END CONTEXT PACK ===\n'
    out = capture(monkeypatch, tmp_path, diff_prompt(monkeypatch, tmp_path, caller))
    prefix = out[:out.index('+change')]
    assert 'tuple(sorted' not in prefix and '(scope, name)' not in prefix
    assert prefix.count('[REDACTED]') >= 2
    assert '+    pair_key = tuple(sorted((left, right)))' in out


def test_same_assignments_masked_in_stderr_tail_and_run_record(tmp_path):
    tail = env_redact.OutputLines(env_redact.redact).feed(CODE, final=True)
    async def collect():
        job = SimpleNamespace(role='review', start_time=time.time(), status='running',
            events=[], survivors=[], last_activity_ts=None, stdout='', stderr='',
            _live_log=tmp_path / 'live.log', _watchdog_baseline=None,
            _watchdog_limits=dict(interval=60, max_s=60, stall_s=60, first_edit_s=60))
        streams = [asyncio.StreamReader(), asyncio.StreamReader()]
        for stream in streams:
            stream.feed_data(CODE.encode())
            stream.feed_eof()
        proc = SimpleNamespace(stdout=streams[0], stderr=streams[1], returncode=0)
        await run_watchdog.collect(SimpleNamespace(jobs={'j': job}, _child_envs={'j': {}},
            _stdin_payloads={}), 'j', proc)
        assert job.stdout == job.stderr == tail
        assert 'tuple(sorted' not in (tmp_path / 'live.log').read_text()
    asyncio.run(collect())
    svc = SpawnService(runner=SimpleNamespace(_child_envs={}, jobs={}), state=tmp_path)
    svc._save(dict(run_id='run-source', host='synthetic', stderr_tail=CODE, stdout_tail=CODE))
    stored = json.loads(svc._path('run-source').read_text())
    assert stored['stderr_tail'] == stored['stdout_tail'] == tail
    assert 'tuple(sorted' not in tail and '(scope, name)' not in tail
    assert tail.count('[REDACTED]') == 2


@pytest.mark.parametrize('role', ['review', 'implement'])
def test_packed_source_assignments_readable_tokens_masked(monkeypatch, tmp_path, role):
    (tmp_path / 'sample.py').write_text(CODE + '\n'.join(tokens()))
    monkeypatch.setattr(context_pack, 'tracked', lambda root: frozenset({'sample.py'}))
    card = {'context': ['sample.py:1-6']}
    pack = context_pack.build('', card, tmp_path)
    assert CODE.rstrip() in pack.text
    if role == 'review':
        prompt = diff_prompt(monkeypatch, tmp_path, CODE, pack, inline=True)
    else:
        prompt = context_pack.pack_prompt(CODE, card, tmp_path, pack)
    out = capture(monkeypatch, tmp_path, prompt, role)
    assert CODE.rstrip() in out
    assert all(value not in out for value in tokens())
    assert 'tuple(sorted' not in out.split('=== CONTEXT PACK ===')[0]


@pytest.mark.parametrize('complete', [True, False])
def test_packed_private_key_still_refused(monkeypatch, tmp_path, complete):
    block = pem_block(complete)
    (tmp_path / 'sample.py').write_text(block)
    monkeypatch.setattr(context_pack, 'tracked', lambda root: frozenset({'sample.py'}))
    pack = context_pack.build('', {'context': [f'sample.py:1-{len(block.splitlines())}']}, tmp_path)
    assert not pack.text
    assert any('secret-looking content' in warning for warning in pack.warnings)


def test_source_mode_keeps_dict_key_and_env_value_scrubbing():
    value = 'synthetic' + '-env-value'
    assert env_redact.redact_source({'api_key': value, 'items': [CODE]}) == {
        'api_key': '[REDACTED]', 'items': [CODE]}
    assert env_redact.redact_env_values(env_redact.redact_source(value), {'API_KEY': value}) == '[autoos:redacted]'


@pytest.mark.parametrize('value', ['tuple(sorted((left, right)))', '(scope, name)',
                                  'None', 'False', '[]', '{}'])
def test_assignment_rule_stays_fail_closed_for_every_value_shape(value):
    text = 'pair_' + 'key = ' + value
    assert env_redact.redact(text) == 'pair_key = [REDACTED]'
    assert env_redact.redact_source(text) == text


@pytest.mark.parametrize('value', ['123', 'arbitrary-value', 'hunter2', '"hunter2"', "'hunter2'",
                                  'os.environ["SERVICE"]', 'get_secret("x") or "hunter2"'])
@pytest.mark.parametrize('name', ['PASSWORD', 'API_' + 'KEY', 'pair_' + 'key', 'MY_' + 'SECRET', 'TOKEN'])
@pytest.mark.parametrize('separator', ['=', ' = ', ': '])
def test_source_masks_literal_credential_assignments(name, separator, value):
    text = name + separator + value
    assert env_redact.redact_source(text) == env_redact.redact(text)
    assert 'hunter2' not in env_redact.redact_source(text) and 'arbitrary' not in env_redact.redact_source(text)


def test_direct_caller_prompt_keeps_full_strength(monkeypatch, tmp_path):
    assert capture(monkeypatch, tmp_path, CODE) == env_redact.redact(CODE)


def test_serialized_diff_is_untrusted_caller_text(monkeypatch, tmp_path):
    caller = str(diff_prompt(monkeypatch, tmp_path))
    out = capture(monkeypatch, tmp_path, caller)
    assert 'tuple(sorted' not in out and '(scope, name)' not in out


def test_source_provenance_never_weakens_run_record_scrub(monkeypatch, tmp_path):
    prompt = diff_prompt(monkeypatch, tmp_path)
    svc = SpawnService(runner=SimpleNamespace(_child_envs={}, jobs={}), state=tmp_path)
    svc._save(dict(run_id='run-source', host='synthetic', stderr_tail=prompt))
    stored = svc._path('run-source').read_text()
    assert 'tuple(sorted' not in stored and '(scope, name)' not in stored


def test_card_context_labels_are_not_source_blocks(monkeypatch, tmp_path):
    name = 'TOKEN=caller-expression.py'
    (tmp_path / name).write_text(CODE)
    monkeypatch.setattr(context_pack, 'tracked', lambda root: frozenset({name}))
    prompt = context_pack.pack_prompt('', {'context': [name + ':1-2']}, tmp_path)
    out = capture(monkeypatch, tmp_path, prompt)
    assert 'caller-expression.py' not in out and CODE.rstrip() in out


def test_source_scrub_failure_withholds_text(monkeypatch):
    monkeypatch.setattr(env_redact.re, 'sub', lambda *a, **k: (_ for _ in ()).throw(ValueError()))
    assert 'redaction failed; output withheld' in env_redact.redact_source(CODE)


def _git(repo, *args):
    import subprocess
    subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@example.invalid',
                    '-c', 'commit.gpgsign=false', '-C', str(repo), *args],
                   check=True, capture_output=True, stdin=subprocess.DEVNULL)


@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_wrapped_token_in_real_git_diff_is_masked_in_final_prompt(monkeypatch, tmp_path, newline):
    first, second = 'gh' + 'p_' + 'A9' * 10, 'A9' * 4
    _git(tmp_path, 'init', '-q', '-b', 'main')
    (tmp_path / 'a.txt').write_bytes(b'context\n')
    _git(tmp_path, 'add', '.')
    _git(tmp_path, 'commit', '-q', '-m', 'one')
    _git(tmp_path, 'checkout', '-q', '-b', 'topic')
    (tmp_path / 'a.txt').write_bytes(('context\n' + first + newline + second + newline
                                      + 'next retained\n').encode())
    _git(tmp_path, 'commit', '-q', '-am', 'two')
    ref = {'repo': str(tmp_path), 'base': 'main', 'head': 'topic'}
    prompt = inline_diff.inline_diff_prompt('review', ref, max_chars=100000)
    out = capture(monkeypatch, tmp_path, prompt)
    assert first not in out and second not in out
    assert '+[REDACTED]' in out and '+next retained' in out


@pytest.mark.parametrize('line', ['PASSWORD = "hunter2value"', 'API_' + 'KEY=hunter2value', 'MY_' + 'SECRET: hunter2value'])
def test_literal_credential_masked_at_the_dispatch_boundary(monkeypatch, tmp_path, line):
    out = capture(monkeypatch, tmp_path, diff_prompt(monkeypatch, tmp_path, extra=line))
    assert 'hunter2value' not in out and '[REDACTED]' in out
    for code_line in CODE.splitlines():   # computed assignments stay readable beside it
        assert '+' + code_line in out
    (tmp_path / 'sample.py').write_text(line + '\n')
    monkeypatch.setattr(context_pack, 'tracked', lambda root: frozenset({'sample.py'}))
    card = {'context': ['sample.py:1-1']}
    pack = context_pack.build('', card, tmp_path)
    packed = capture(monkeypatch, tmp_path, context_pack.pack_prompt('', card, tmp_path, pack))
    assert 'hunter2value' not in pack.text + packed


@pytest.mark.parametrize('complete', [True, False])
def test_git_hunk_inside_a_private_key_block_is_withheld(monkeypatch, tmp_path, complete):
    header = '-----BEGIN ' + 'TEST PRIVATE KEY-----'
    footer = '-----END ' + 'TEST PRIVATE KEY-----'
    body = ['synthetic-pem-%02d' % i for i in range(30)]
    old = [header, *body] + ([footer] if complete else [])
    _git(tmp_path, 'init', '-q', '-b', 'main')
    (tmp_path / 'k.txt').write_text('\n'.join(['keep'] * 3 + old + ['tail']) + '\n')
    _git(tmp_path, 'add', '.')
    _git(tmp_path, 'commit', '-q', '-m', 'one')
    _git(tmp_path, 'checkout', '-q', '-b', 'topic')
    new = list(old)
    new[20] = 'synthetic-pem-changed'   # a hunk far below the BEGIN marker
    (tmp_path / 'k.txt').write_text('\n'.join(['keep'] * 3 + new + ['tail']) + '\n')
    (tmp_path / 'plain.txt').write_text('plain-change\n')
    _git(tmp_path, 'add', '.')
    _git(tmp_path, 'commit', '-q', '-m', 'two')
    ref = {'repo': str(tmp_path), 'base': 'main', 'head': 'topic'}
    out = capture(monkeypatch, tmp_path, inline_diff.inline_diff_prompt('review', ref, max_chars=100000))
    assert 'synthetic-pem' not in out and 'PRIVATE KEY' not in out
    assert '+plain-change' in out and 'diff --git a/k.txt b/k.txt' in out


def test_hunk_is_withheld_when_a_side_cannot_be_read(monkeypatch, tmp_path):
    diff = 'diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@ section\n-old\n+new\n'
    monkeypatch.setattr(inline_diff, '_git_text', lambda *args, **kw: None)
    shown = inline_diff._withhold_key_hunks(diff.replace('--- a', 'index 1111111..2222222 100644\n--- a'), str(tmp_path), 5)
    assert '-old' not in shown and '+new' not in shown and inline_diff.WITHHELD in shown
    assert '@@ -1 +1 @@' in shown and 'section' not in shown


def test_removed_credential_continued_by_a_context_line_is_masked_in_a_diff():
    from plangraph.spawner.redact_tokens import redact_diff_wrapped
    first, rest = 'gh' + 'p_' + 'A9' * 10, 'A9' * 4
    diff = ('diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,3 +1,3 @@\n keep\n-' + first
            + '\n+replacement words here\n ' + rest + '\n tail line\n')
    out = redact_diff_wrapped(diff)
    assert first not in out and rest not in out
    assert '+replacement words here' in out and ' tail line' in out and '-[REDACTED]' in out


def test_added_credential_continued_by_a_context_line_is_masked_in_a_diff():
    from plangraph.spawner.redact_tokens import redact_diff_wrapped
    first, rest = 'gh' + 'p_' + 'A9' * 10, 'A9' * 4
    diff = ('diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,2 +1,3 @@\n-old words here\n+' + first
            + '\n ' + rest + '\n tail line\n')
    out = redact_diff_wrapped(diff)
    assert first not in out and rest not in out and '-old words here' in out


def test_git_quoted_file_names_do_not_withhold_clean_hunks(monkeypatch, tmp_path):
    _git(tmp_path, 'init', '-q', '-b', 'main')
    name = 'café über.txt'
    (tmp_path / name).write_text('one\ntwo\n')
    _git(tmp_path, 'add', '.')
    _git(tmp_path, 'commit', '-q', '-m', 'one')
    _git(tmp_path, 'checkout', '-q', '-b', 'topic')
    (tmp_path / name).write_text('one\nclean-change\n')
    _git(tmp_path, 'commit', '-q', '-am', 'two')
    ref = {'repo': str(tmp_path), 'base': 'main', 'head': 'topic'}
    out = capture(monkeypatch, tmp_path, inline_diff.inline_diff_prompt('review', ref, max_chars=100000))
    assert '+clean-change' in out and inline_diff.WITHHELD not in out


def test_hunk_without_an_index_line_is_withheld(monkeypatch, tmp_path):
    diff = 'diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n'
    monkeypatch.setattr(inline_diff, '_git_text', lambda *args, **kw: 'plain\n')
    shown = inline_diff._withhold_key_hunks(diff, str(tmp_path), 5)
    assert '+new' not in shown and inline_diff.WITHHELD in shown


@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_masked_removal_keeps_its_line_ending_before_the_addition(newline):
    from plangraph.spawner.redact_tokens import redact_diff_wrapped
    first, rest = 'gh' + 'p_' + 'A9' * 10, 'A9' * 4
    diff = newline.join(['diff --git a/x b/x', '--- a/x', '+++ b/x', '@@ -1,3 +1,3 @@', ' keep',
                         '-' + first, '+added words here', ' ' + rest, ' tail line', ''])
    out = redact_diff_wrapped(diff)
    assert out.split(newline) == ['diff --git a/x b/x', '--- a/x', '+++ b/x', '@@ -1,3 +1,3 @@', ' keep',
                                  '-[REDACTED]', '+added words here', ' [REDACTED]', ' tail line', '']


@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_masked_wrapped_removal_stays_one_line_and_the_addition_a_separate_one(newline):
    from plangraph.spawner.redact_tokens import redact_diff_wrapped
    first, rest = 'gh' + 'p_' + 'A9' * 10, 'A9' * 4
    diff = newline.join(['@@ -1,2 +1,1 @@', '-' + first, '-' + rest, '+added words here', ''])
    assert redact_diff_wrapped(diff).split(newline) == ['@@ -1,2 +1,1 @@', '-[REDACTED]', '+added words here', '']


@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_independently_masked_addition_after_a_masked_removal_keeps_its_marker(newline):
    from plangraph.spawner.redact_tokens import redact_diff_wrapped
    old, new = 'gh' + 'p_' + 'A9' * 10, 'gh' + 'p_' + 'B8' * 10
    diff = newline.join(['@@ -1,4 +1,4 @@', '-' + old, '+' + new, ' A9A9', ' tail words', ''])
    lines = redact_diff_wrapped(diff).split(newline)
    assert lines[:3] == ['@@ -1,4 +1,4 @@', '-[REDACTED]', '+[REDACTED]']
    assert 'A9' not in ''.join(lines) and 'B8' not in ''.join(lines) and ' tail words' in lines


def test_real_git_diff_keeps_both_markers_of_two_masked_credentials(monkeypatch, tmp_path):
    old, new = 'gh' + 'p_' + 'A9' * 10, 'gh' + 'p_' + 'B8' * 10
    _git(tmp_path, 'init', '-q', '-b', 'main')
    (tmp_path / 'a.txt').write_text('keep\n' + old + '\nA9A9\ntail words\n')
    _git(tmp_path, 'add', '.')
    _git(tmp_path, 'commit', '-q', '-m', 'one')
    _git(tmp_path, 'checkout', '-q', '-b', 'topic')
    (tmp_path / 'a.txt').write_text('keep\n' + new + '\nA9A9\ntail words\n')
    _git(tmp_path, 'commit', '-q', '-am', 'two')
    ref = {'repo': str(tmp_path), 'base': 'main', 'head': 'topic'}
    out = capture(monkeypatch, tmp_path, inline_diff.inline_diff_prompt('review', ref, max_chars=100000))
    assert old not in out and new not in out and 'A9A9' not in out
    assert '\n-[REDACTED]\n+[REDACTED]\n' in out and '\n tail words\n' in out


@pytest.mark.parametrize('value', ['f(a, PASSWORD=hunter2value)', 'g(x)[TOKEN: hunter2value]',
                                  'f(a, MY_' + 'SECRET=hunter2value)', '(PASSWORD = hunter2value)'])
def test_nested_credential_literal_in_a_computed_value_is_masked_in_source(value):
    out = env_redact.redact_source('API_' + 'KEY = ' + value)
    assert 'hunter2value' not in out and out == env_redact.redact('API_' + 'KEY = ' + value)
