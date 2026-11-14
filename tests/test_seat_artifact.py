"""Synthetic runtime boundary checks; no keeper or credential IO."""
import hashlib
import json
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import seat_verdict as sv
from plangraph.spawner import seat_artifact as sa


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('external IO forbidden')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket, 'create_connection', deny)
    monkeypatch.setattr(sv, 'send', deny)
    monkeypatch.setattr(Path, 'home', deny)


def completed(model='synthetic/gpt-5'):
    prompt = 'exact dispatched synthetic prompt\n'
    job = SimpleNamespace(job_id='job-final', status='completed', exit_code=0,
        served_model=model, response_finish_reason='stop',
        prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
        stdout='VERDICT: PASS\nTEST-GAMING: no - searched synthetic diff: 0 hits')
    record = dict(run_id='run-synthetic', job_id=job.job_id, role='review',
        ended=10, status='completed', exit_code=0, stdout_tail='VERDICT: FIX')
    return record, job


def dry(directory):
    return sv.main(['--key', 'demo:A', '--seat-dir', str(directory),
        '--writer-family', 'meta', '--token-dir', str(directory / 'absent'), '--dry-run'])


@pytest.mark.parametrize('model', ['synthetic/mimo-v2', 'synthetic/gpt-5'])
@pytest.mark.parametrize('legacy', [False, True])
def test_witnessed_pair(tmp_path, capsys, model, legacy):
    record, job = completed(model)
    if legacy:
        record.pop('job_id')
    sa.publish(tmp_path, record, job)
    evidence = json.loads((tmp_path / 'evidence.json').read_text())
    assert evidence['prompt_sha256'] == hashlib.sha256(b'exact dispatched synthetic prompt\n').hexdigest()
    assert evidence['correlation_id'] == job.job_id
    assert evidence['run_id'] == record['run_id']
    assert evidence['finish_reason'] == 'stop'
    assert evidence['verdict'] == 'pass' and evidence['test_gaming'] == 'no'
    assert (tmp_path / 'review.txt').read_text() == job.stdout
    assert all((tmp_path / name).stat().st_mode & 0o777 == 0o600
               for name in ('review.txt', 'evidence.json'))
    assert dry(tmp_path) == 0
    assert json.loads(capsys.readouterr().out)['body']['ok'] is True


@pytest.mark.parametrize('field,value', [('served_model', 'unwitnessed'),
    ('response_finish_reason', None), ('response_finish_reason', 'length'),
    ('prompt_sha256', None), ('status', 'failed'), ('status', 'cancelled'),
    ('stdout', 'VERDICT: PASS'),
    ('stdout', 'VERDICT: PASS\nTEST-GAMING: yes - "synthetic"')])
def test_failed_replacement_clears_earlier_pass(tmp_path, field, value):
    record, job = completed()
    sa.publish(tmp_path, record, job)
    setattr(job, field, value)
    sa.publish(tmp_path, record, job)
    assert not (tmp_path / 'evidence.json').exists()
    assert not (tmp_path / 'review.txt').exists()
    assert dry(tmp_path) != 0


def test_atomic_order_failure_and_symlink(tmp_path, monkeypatch):
    record, job = completed()
    outside = tmp_path / 'untouched'
    outside.write_text('original')
    (tmp_path / 'review.txt').symlink_to(outside)
    writes = []
    real = sa.atomic_write_private
    def write(path, text):
        writes.append(path.name)
        assert not (tmp_path / 'evidence.json').exists()
        if path.name == 'evidence.json':
            raise OSError('injected publication failure')
        real(path, text)
    monkeypatch.setattr(sa, 'atomic_write_private', write)
    with pytest.raises(OSError):
        sa.publish(tmp_path, record, job)
    assert writes == ['review.txt', 'evidence.json']
    assert outside.read_text() == 'original'
    assert not (tmp_path / 'review.txt').exists()


def test_non_review_unchanged(tmp_path):
    record, job = completed()
    record['role'] = 'implement'
    sa.publish(tmp_path, record, job)
    assert list(tmp_path.iterdir()) == []


def test_false_reason_labels_fix_even_with_pass_text(tmp_path, capsys):
    record, job = completed()
    job.stdout = 'VERDICT: FIX\nPASS is only echoed\nTEST-GAMING: no - searched diff: 0 hits'
    sa.publish(tmp_path, record, job)
    assert dry(tmp_path) == 0
    body = json.loads(capsys.readouterr().out)['body']
    assert body['ok'] is False
    assert body['reason'].startswith('seat synthetic/gpt-5 FIX corr ')
    evidence = json.loads((tmp_path / 'evidence.json').read_text())
    reason = sv.build_reason(evidence, False, job.stdout, 0)
    assert reason.startswith('seat synthetic/gpt-5 FIX corr ')
    assert body['reason'] == reason + '\ncovers: meta'


def test_finish_reason_is_response_side(tmp_path, monkeypatch):
    record, job = completed()
    verdict = sa.judge(job.stdout, job.response_finish_reason)
    fields = verdict.to_evidence()
    fields.pop('finish_reason')
    monkeypatch.setattr(sa, 'judge', lambda *args: SimpleNamespace(
        verdict=verdict.verdict, test_gaming=verdict.test_gaming,
        to_evidence=lambda: fields))
    sa.publish(tmp_path, record, job)
    evidence = json.loads((tmp_path / 'evidence.json').read_text())
    assert evidence['finish_reason'] == 'stop'


@pytest.mark.parametrize('dispatch', [False, True])
def test_both_completion_saves(tmp_path, monkeypatch, dispatch):
    from plangraph.spawner.service import SpawnService
    from plangraph.spawner.runner import spawn_service
    from plangraph.spawner import notify
    record, job = completed()
    runner = SimpleNamespace(jobs={job.job_id: job}, default_cwd=str(tmp_path))
    monkeypatch.setattr(notify, 'completion_event', lambda *a: {})
    sink = SimpleNamespace(deliver=lambda *a: None)
    service = (spawn_service(runner=runner, state=tmp_path,
        completion_sink=sink, keeper_sink=sink) if dispatch else
        SpawnService(runner=runner, state=tmp_path))
    service._save(record)
    assert dry(service._path(record['run_id']).parent) == 0


@pytest.mark.parametrize('dry_run', [True, False])
def test_dispatch_hash_is_in_memory(tmp_path, monkeypatch, dry_run):
    import asyncio
    from plangraph.spawner import runner as module
    received = []
    monkeypatch.setattr(module, 'build_command', lambda **kw:
        (received.append(kw['prompt']), ['synthetic'])[1])
    from plangraph.spawner import resolver
    async def available(*args, **kwargs):
        return None
    async def process(*args, **kwargs):
        assert args == ('synthetic',)
        return SimpleNamespace()
    async def monitor(*args):
        pass
    monkeypatch.setattr(resolver, 'host_skip_reason_async', available)
    monkeypatch.setattr(module, '_resolve_launch', lambda *a, **kw: ('synthetic', None))
    instance = module.SpawnerRunner(process_factory=process)
    monkeypatch.setattr(instance, '_monitor_process', monitor)
    job = asyncio.run(instance.spawn(prompt='synthetic é\n', role='review',
        dry_run=dry_run, cwd=str(tmp_path), wait_seconds=1))
    assert received == [job.task_prompt]
    assert job.prompt_sha256 == hashlib.sha256('synthetic é\n'.encode()).hexdigest()


def test_authenticated_finish_not_inferred(tmp_path):
    from plangraph.spawner.runner import SpawnerRunner
    instance = SpawnerRunner()
    record, job = completed()
    job.response_finish_reason = None
    instance.jobs[job.job_id] = job
    capability = object()
    instance._adapter_tokens[job.job_id] = capability
    with pytest.raises(ValueError):
        instance.served_witness(job.job_id, 'synthetic/gpt-5',
            adapter_token=object(), finish_reason='stop')
    instance.served_witness(job.job_id, 'synthetic/gpt-5', adapter_token=capability)
    assert job.response_finish_reason is None
    instance.served_witness(job.job_id, 'synthetic/gpt-5',
        adapter_token=capability, finish_reason='stop')
    sa.publish(tmp_path, record, job)
    assert (tmp_path / 'evidence.json').exists()
    instance.served_witness(job.job_id, 'synthetic/gpt-5', adapter_token=capability)
    assert job.response_finish_reason is None
    sa.publish(tmp_path, record, job)
    assert not (tmp_path / 'evidence.json').exists()
    assert dry(tmp_path) != 0


def test_prompt_only_verdict_without_diff_is_void(tmp_path):
    record, job = completed()
    record['chosen_leg'] = {'tool_mode': 'prompt-only'}
    (tmp_path / 'prompt.txt').write_text('Review an unspecified change')
    sa.publish(tmp_path, record, job)
    assert record['verdict_status'] == 'void'
    assert record['verdict_void_reason'] == 'prompt_only_no_diff'
    assert not (tmp_path / 'evidence.json').exists()


def test_prompt_only_verdict_with_diff_publishes(tmp_path):
    record, job = completed()
    record['chosen_leg'] = {'tool_mode': 'prompt-only'}
    (tmp_path / 'prompt.txt').write_text('diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n')
    sa.publish(tmp_path, record, job)
    assert 'verdict_status' not in record
    assert (tmp_path / 'evidence.json').exists()


def test_companion_evidence_cannot_bypass_prompt_only_void(tmp_path):
    record, job = completed()
    sa.publish(tmp_path, record, job)
    record['chosen_leg'] = {'tool_mode': 'prompt-only'}
    (tmp_path / 'record.json').write_text(json.dumps(record))
    (tmp_path / 'prompt.txt').write_text('no patch supplied')
    with pytest.raises(ValueError, match='prompt_only_no_diff'):
        sv.read_record_identity(tmp_path)
