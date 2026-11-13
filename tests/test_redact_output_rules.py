"""D-5279: prompt/source token parity, output-only hashes, real write boundaries."""
import asyncio
import hashlib
import json
import time
from types import SimpleNamespace

import pytest

from plangraph.spawner import cli, context_pack, doctor, env_redact, notify, runner, run_watchdog
from plangraph.spawner.redact_tokens import MAX_WRAPPED_BODY, SPECS, redact_wrapped
from plangraph.spawner.service import SpawnService
from test_redact_source_mode import capture, diff_prompt


FAMILIES = [
    (0, 's', 'k-'), (0, 'xs', 'k-'),
    *((1, 'gh', c + '_') for c in 'pousr'),
    (2, 'github', '_pat_'),
    *((3, 'xo', 'x' + c + '-') for c in 'abprs'),
    (4, 'AK', 'IA'), (5, 'AI', 'za'),
]


def token(index, left, right):
    return left + right + ('A9' * SPECS[index].floor)[:SPECS[index].floor]


SECRETS = [token(*family) for family in FAMILIES]
SECRETS.append('ey' + 'JAb1Ab1Ab' + '.Cd2Cd2Cd' + '.Ef3Ef3Ef')


async def collect(text, tmp_path, monkeypatch, *, monitor=False, env=None):
    """Real child stream collector (and optionally the final runner monitor)."""
    child = runner.SpawnerRunner(default_cwd=str(tmp_path))
    job = runner.JobInfo('j', 'running', '', 'synthetic', role='review',
                         command=['synthetic-child'], cwd=str(tmp_path))
    job._live_log = tmp_path / 'live.log'
    job._watchdog_baseline = None
    job._watchdog_limits = dict(interval=60, max_s=60, stall_s=60, first_edit_s=60)
    child.jobs['j'] = job
    child._child_envs['j'] = env or {}
    streams = [asyncio.StreamReader(), asyncio.StreamReader()]
    for stream in streams:
        stream.feed_data(text.encode())
        stream.feed_eof()
    proc = SimpleNamespace(stdout=streams[0], stderr=streams[1], returncode=0)
    if monitor:
        async def noop(*args):
            pass
        monkeypatch.setattr(child, '_reap_process', noop)
        monkeypatch.setattr(child, '_check_git_config_guard', lambda *args: None)
        monkeypatch.setattr(runner, 'witness_native', noop)
        await child._monitor_process('j', proc, 'synthetic')
        assert job.status == 'completed'
    else:
        await run_watchdog.collect(child, 'j', proc)
    return child, job


@pytest.mark.parametrize('length', [40, 64])
@pytest.mark.parametrize('monitor', [False, True])
def test_hashes_survive_real_prompt_but_not_child_tails(monkeypatch, tmp_path, length, monitor):
    value = 'ab12' * (length // 4)
    text = 'inspect commit ' + value + '\n'
    # capture checks both the final command prompt and on-disk prompt record.
    assert capture(monkeypatch, tmp_path, text) == text
    child, job = asyncio.run(collect(text, tmp_path, monkeypatch, monitor=monitor))
    assert job.stdout == job.stderr == 'inspect commit [REDACTED]\n'
    assert value not in (tmp_path / 'live.log').read_text()
    svc = SpawnService(runner=child, state=tmp_path)
    record = dict(run_id='run-' + 'ab12' * 8, host='synthetic', job_id='j',
                  stdout_tail=job.stdout, stderr_tail=job.stderr,
                  attempts=[dict(stdout_tail=job.stdout, stderr_tail=job.stderr)])
    svc._save(record)
    stored = json.loads(svc._path(record['run_id']).read_text())
    for target in (stored, *stored['attempts']):
        assert target['stdout_tail'] == target['stderr_tail'] == job.stdout
        assert value not in target['stdout_tail'] + target['stderr_tail']


def test_record_hash_fields_and_notify_ids_survive(monkeypatch, tmp_path):
    record = dict(run_id='run-' + 'ab12' * 8, job_id='job-' + 'cd34' * 8,
                  card_hash='ef56' * 16, prompt_sha256=hashlib.sha256(b'prompt').hexdigest(),
                  head_sha='ab12' * 10, status='completed', exit_code=0,
                  host='synthetic', stdout_tail='[REDACTED]', stderr_tail='[REDACTED]')
    original = dict(record)
    svc = SpawnService(runner=SimpleNamespace(_child_envs={}, jobs={}), state=tmp_path)
    svc._save(record)
    stored = json.loads(svc._path(original['run_id']).read_text())
    for name in ('run_id', 'job_id', 'card_hash', 'prompt_sha256', 'head_sha'):
        assert stored[name] == original[name]
    monkeypatch.setattr(notify.subprocess, 'check_output', lambda *a, **k: original['head_sha'])
    done = notify.completion_event(stored, str(tmp_path))
    refused = notify.leg_refused_event(stored, original['job_id'], 'synthetic refusal')
    assert done['run_id'] == refused['run_id'] == original['run_id']
    assert done['event_id'] == original['run_id'] + ':completion'
    assert done['head_sha'] == original['head_sha']
    assert original['head_sha'] in done['take_cmd']
    assert refused['event_id'] == original['run_id'] + ':leg_refused:' + original['job_id']
    assert stored['card_hash'] == original['card_hash']
    assert stored['prompt_sha256'] == original['prompt_sha256']


@pytest.mark.parametrize('secret', SECRETS, ids=[*range(len(FAMILIES)), 'jwt'])
def test_each_family_in_caller_diff_context_and_stdout(monkeypatch, tmp_path, secret):
    caller = capture(monkeypatch, tmp_path, 'caller ' + secret + '; retained\n')
    assert secret not in caller and '[REDACTED]' in caller
    diff = capture(monkeypatch, tmp_path, diff_prompt(monkeypatch, tmp_path, extra=secret))
    assert secret not in diff and '+[REDACTED]' in diff
    (tmp_path / 'sample.py').write_text('value = "' + secret + '"\n')
    monkeypatch.setattr(context_pack, 'tracked', lambda root: frozenset({'sample.py'}))
    card = {'context': ['sample.py:1-1']}
    pack = context_pack.build('', card, tmp_path)
    assert secret not in pack.text and '[REDACTED]' in pack.text
    packed = capture(monkeypatch, tmp_path, context_pack.pack_prompt('', card, tmp_path, pack))
    assert secret not in packed and '[REDACTED]' in packed
    _, job = asyncio.run(collect('child ' + secret + '; retained\n', tmp_path, monkeypatch))
    assert secret not in job.stdout and '[REDACTED]' in job.stdout


@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_wrapped_token_in_prompt_and_tail(monkeypatch, tmp_path, newline):
    secret = 'gh' + 'p_' + 'A9' * 4 + newline + 'A9' * 6
    text = secret + '; retained\n'
    assert capture(monkeypatch, tmp_path, text) == '[REDACTED]; retained\n'
    _, job = asyncio.run(collect(text, tmp_path, monkeypatch))
    assert job.stdout == job.stderr == '[REDACTED]; retained\n'


@pytest.mark.parametrize('name', ['MY_SECRET', 'DB_PASSWORD', 'AUTH_TOKEN',
                                  'max_tokens', 'token_count'])
def test_widened_names_at_prompt_output_record_and_notification_boundaries(monkeypatch, tmp_path, name):
    text, expected = name + '=v\n', name + '=[REDACTED]\n'
    assert capture(monkeypatch, tmp_path, text) == expected
    child, job = asyncio.run(collect(text, tmp_path, monkeypatch))
    assert job.stdout == job.stderr == expected
    assert (tmp_path / 'live.log').read_text().count(expected) == 2
    svc = SpawnService(runner=child, state=tmp_path)
    record = dict(run_id='run-names', status='completed', exit_code=0, reason=text,
                  stdout_tail=text, stderr_tail=text)
    svc._save(record)
    stored = json.loads(svc._path(record['run_id']).read_text())
    assert stored['stdout_tail'] == stored['stderr_tail'] == stored['reason'] == expected
    assert notify.completion_event(stored, str(tmp_path))['reason'] == expected
    assert notify.leg_refused_event(stored, 'synthetic', text)['reason'] == expected


@pytest.mark.parametrize('newline', ['\n', '\r\n'])
@pytest.mark.parametrize('prefix, suffix', [
    ('API_KEY="', '"'), ('export API_KEY=', ''), ('token: ', ''),
])
@pytest.mark.parametrize('scrub', [env_redact.redact, env_redact.redact_source, env_redact.redact_output])
def test_wrapped_assignment_is_masked_whole(newline, prefix, suffix, scrub):
    secret = 'gh' + 'p_' + 'A9' * 7 + newline + 'A9' * 8
    text = prefix + secret + suffix + newline + 'next retained'
    # D-5279 R3: Python keeps value quotes; Rust's existing assignment rule drops them.
    expected = prefix + '[REDACTED]' + suffix + newline + 'next retained'
    assert scrub(text) == expected
    assert scrub(expected) == expected


def test_split_token_at_source_boundary_fails_closed(monkeypatch, tmp_path):
    prompt = 'sk-' + env_redact._source_block('synthetic123')
    assert str(prompt) == 'sk-' + 'synthetic123'
    out = capture(monkeypatch, tmp_path, prompt)
    assert out == '[autoos:redaction failed; output withheld]\n'


def test_source_span_cannot_end_midline(monkeypatch, tmp_path):
    prompt = env_redact._source_block('s') + 'k-' + 'synthetic123'
    assert capture(monkeypatch, tmp_path, prompt) == '[autoos:redaction failed; output withheld]\n'


@pytest.mark.parametrize('at_end', [False, True])
def test_empty_source_span_at_prompt_boundary_fails_closed(monkeypatch, tmp_path, at_end):
    text = 'caller text without boundary newlines'
    offset = len(text) if at_end else 0
    prompt = env_redact._SourcePrompt(text, ((offset, offset),))
    assert capture(monkeypatch, tmp_path, prompt) == '[autoos:redaction failed; output withheld]\n'


@pytest.mark.parametrize('scrub', [env_redact.redact, env_redact.redact_source, env_redact.redact_output])
def test_unterminated_pem_scan_is_linear(scrub):
    line = '-----BEGIN ' + 'X PRIVATE KEY-----\n' + 'filler ' * 17 + '\n'
    text = (line * 2000).ljust(300_000, 'x')
    assert len(text) == 300_000
    start = time.perf_counter()
    assert scrub(text) == '[REDACTED]'
    assert time.perf_counter() - start < 2


@pytest.mark.parametrize('newline', ['\n', '\r\n'])
@pytest.mark.parametrize('family', FAMILIES)
def test_wrapped_floors_whole_lines_and_budget(family, newline):
    index, left, right = family
    prefix, floor = left + right, SPECS[index].floor
    for length in (floor - 1, floor):
        text = prefix + newline + 'A' * length + '; retained'
        expected = '[REDACTED]; retained' if length == floor else text
        assert env_redact.redact(text) == env_redact.redact_source(text) == expected
    text = token(*family) + newline + 'A' + newline + 'B' + newline + 'kept prose'
    assert redact_wrapped(text) == '[REDACTED]' + newline + 'kept prose'
    text = prefix + 'A' * (MAX_WRAPPED_BODY - 1) + newline + 'A' + newline + 'B'
    assert redact_wrapped(text) == '[REDACTED]' + newline + 'B'
    text = prefix + 'A' * floor + newline + 'a word'
    assert redact_wrapped(text) == text
    text = prefix + 'A' * floor + '\rA'
    assert redact_wrapped(text) == text  # bare CR never continues a body


def test_exact_length_and_word_boundaries():
    aws = 'AK' + 'IA' + 'A9' * 8
    assert env_redact.redact(aws) == '[REDACTED]'
    for text in (aws + 'A', aws + '_', 'a' + aws, 'a' + SECRETS[2]):
        assert env_redact.redact(text) == text
    assert env_redact.redact('é' + SECRETS[2]) == 'é' + SECRETS[2]


def test_output_only_blob_and_plain_text_survivors():
    blob = 'Zq9_Ab/2' * 5 + '=='
    assert env_redact.redact(blob) == env_redact.redact_source(blob) == blob
    assert env_redact.redact_output(blob) == '[REDACTED]'
    for text in ('a' * 31, 'z' * 60, '1234_/' * 10, 'src/' + 'word/' * 12):
        assert env_redact.redact_output(text) == text
    # Numeric-only non-hex blobs must survive rule 8 (rule 7 runs first).
    assert env_redact.redact_output('1234_/' * 10 + '==') == '1234_/' * 10 + '=='
    with pytest.raises(TypeError, match='not a record'):
        env_redact.redact_output({'run_id': 'run-' + 'ab12' * 8})


def test_cli_stream_doctor_and_env_value_results_use_output_rules(monkeypatch):
    value, secret = 'ab12' * 10, 'synthetic-env-value'
    text = value + ' ' + secret + '\n'
    monkeypatch.setenv('SYNTHETIC_API_KEY', secret)
    written = []
    cli._drain_chunks([text[:10].encode(), text[10:].encode()], written.append)
    assert b''.join(written) == b'[REDACTED] [autoos:redacted]\n'
    assert doctor.clean(text) == '[REDACTED] [autoos:redacted]'
