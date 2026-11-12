"""D-4430: denied-tool review verdicts survive opencode's rc 1 termination."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from plangraph.spawner.runner import JobInfo, SpawnerRunner


def test_verdict_has_no_completion_hook_marker(tmp_path, monkeypatch):
    from plangraph.spawner.runner import spawn_service
    service = spawn_service(state=tmp_path)
    record = dict(run_id='run-review', role='review', ended=1, status='completed', completion={})
    directory = service._path(record['run_id']).parent
    directory.mkdir(parents=True, exist_ok=True)
    for name in ('parent.delivered', 'keeper.delivered', 'verdict.delivered',
                 'parent.hook.delivered', 'keeper.hook.delivered'):
        (directory / name).touch()
    monkeypatch.setenv('PLANGRAPH_SPAWN_NOTIFY_CMD', 'unused')
    monkeypatch.setattr(service, 'ps', lambda: {'runs': [record]})
    saves = []
    monkeypatch.setattr(service, '_save', lambda *a, **kw: saves.append(a))
    service.retry_completions()
    assert saves == []
    record.pop('completion')
    service.retry_completions()
    assert saves == []


def test_missing_verdict_marker_redrives_review(tmp_path, monkeypatch):
    from plangraph.spawner.runner import spawn_service
    service = spawn_service(state=tmp_path)
    record = dict(run_id='run-review', role='review', ended=1, status='completed', completion={})
    directory = service._path(record['run_id']).parent
    directory.mkdir(parents=True)
    for name in ('parent.delivered', 'keeper.delivered',
                 'parent.hook.delivered', 'keeper.hook.delivered'):
        (directory / name).touch()
    monkeypatch.setenv('PLANGRAPH_SPAWN_NOTIFY_CMD', 'unused')
    monkeypatch.setattr(service, 'ps', lambda: {'runs': [record]})
    saves = []
    monkeypatch.setattr(service, '_save', lambda *a, **kw: saves.append(a))
    service.retry_completions()
    assert len(saves) == 1
    assert saves[0][0] is record


@pytest.mark.parametrize('failure', ['import', 'lock', 'missing-head', 'null-ref', 'reject', 'post', 'post-value'])
def test_forward_failure_boundaries(tmp_path, monkeypatch, failure):
    import builtins
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'tools'))
    import seat_verdict as seat
    from plangraph.spawner import notify
    record = {'role': 'review', 'item_key': 'demo:A', 'given_writer_families': ['deepseek']}
    evidence = {'served_model': 'opencode/mimo-v2.6-free', 'head_sha': 'd' * 40}
    if failure == 'missing-head':
        evidence.pop('head_sha')
    monkeypatch.setattr(seat, 'evidence_from_record', lambda *a: (evidence, 'VERDICT: ACCEPT'))
    monkeypatch.setenv('PG_REVIEWER_TOKEN_DIR', str(tmp_path))
    calls = []
    monkeypatch.setattr(seat, 'main', lambda *a: calls.append(a) or seat.EXIT_PASS_REFUSED)
    if failure in ('post', 'post-value'):
        def post(*args):
            calls.append(args)
            raise (ValueError if failure == 'post-value' else RuntimeError)('ambiguous acknowledgement')
        monkeypatch.setattr(seat, 'main', post)
    endpoint = SimpleNamespace(url='unused', item=lambda key: {'key': key, 'evidence': [
        {'kind': 'commit+ci', 'verified': True,
         'ref': None if failure == 'null-ref' else {'commit': 'd' * 40}}]})
    if failure == 'import':
        original = builtins.__import__
        def importing(name, *args, **kwargs):
            if name == 'seat_verdict':
                raise ImportError('unavailable')
            return original(name, *args, **kwargs)
        monkeypatch.setattr(builtins, '__import__', importing)
    if failure == 'lock':
        monkeypatch.setattr(notify, 'inbox_lock', lambda *a: (_ for _ in ()).throw(OSError('locked')))
    notify.deliver_verdict(tmp_path, record, endpoint)
    if failure in ('import', 'lock'):
        assert not (tmp_path / 'verdict.delivered').exists()
        if failure == 'lock':
            assert not (tmp_path / 'verdict.json').exists()
    else:
        assert json.loads((tmp_path / 'verdict.json').read_text())['status'] == 'skipped'
        assert (tmp_path / 'verdict.delivered').exists()
        notify.deliver_verdict(tmp_path, record, endpoint)
        assert len(calls) == (1 if failure in ('reject', 'post', 'post-value') else 0)


@pytest.mark.parametrize('commits, forwarded', [(['a' * 40, 'd' * 40], 1),
    (['d' * 40, 'b' * 40], 0), (['a' * 40, 'b' * 40], 0)])
def test_verdict_head_must_be_latest_verified_head(tmp_path, monkeypatch, commits, forwarded):
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'tools'))
    import seat_verdict as seat
    from plangraph.spawner import notify
    record = {'role': 'review', 'item_key': 'demo:A', 'given_writer_families': ['deepseek']}
    monkeypatch.setattr(seat, 'evidence_from_record', lambda *a: (
        {'served_model': 'opencode/mimo-v2.6-free', 'head_sha': 'd' * 40}, 'VERDICT: ACCEPT'))
    monkeypatch.setenv('PG_REVIEWER_TOKEN_DIR', str(tmp_path))
    calls = []
    monkeypatch.setattr(seat, 'main', lambda *a: calls.append(a) or seat.EXIT_PASS_REFUSED)
    endpoint = SimpleNamespace(url='unused', item=lambda key: {'key': key, 'evidence': [
        {'kind': 'commit+ci', 'verified': True, 'ref': {'commit': c}} for c in commits]})
    notify.deliver_verdict(tmp_path, record, endpoint)
    assert len(calls) == forwarded
    if commits == ['d' * 40, 'b' * 40]:
        assert json.loads((tmp_path / 'verdict.json').read_text())['reason'] == 'superseded head'


@pytest.mark.parametrize('writers,forwarded', [(['deepseek'], True),
    (['deepseek', 'anthropic'], False)])
def test_haiku_forward_gate_and_crash_intent(tmp_path, monkeypatch, writers, forwarded):
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'tools'))
    import seat_verdict as seat
    from plangraph.spawner import notify
    record = dict(role='review', item_key='demo:A', given_writer_families=writers)
    monkeypatch.setattr(seat, 'evidence_from_record', lambda *a: (
        dict(served_model='anthropic/claude-haiku-4-5', head_sha='d' * 40), 'VERDICT: ACCEPT'))
    monkeypatch.setenv('PG_REVIEWER_TOKEN_DIR', str(tmp_path))
    calls = []
    def killed(*args):
        calls.append(args)
        assert json.loads((tmp_path / 'verdict.json').read_text())['status'] == 'sending'
        raise SystemExit('killed after POST')
    monkeypatch.setattr(seat, 'main', killed)
    keeper = SimpleNamespace(url='unused', item=lambda key: dict(key=key, evidence=[
        dict(kind='commit+ci', verified=True, ref=dict(commit='d' * 40))]))
    if forwarded:
        with pytest.raises(SystemExit):
            notify.deliver_verdict(tmp_path, record, keeper)
    else:
        notify.deliver_verdict(tmp_path, record, keeper)
    notify.deliver_verdict(tmp_path, record, keeper)
    assert len(calls) == int(forwarded)
    journal = json.loads((tmp_path / 'verdict.json').read_text())
    assert journal['status'] == 'skipped'
    assert journal['reason'] == ('ambiguous send, not retried' if forwarded else
        'unwitnessed family or invalid/overlapping writer families')


def test_lock_failure_preserves_attempt_accounting(tmp_path, monkeypatch):
    from plangraph.spawner import notify
    journal = tmp_path / 'verdict.json'
    previous = '{"status": "retry", "attempts": 2}\n'
    journal.write_text(previous)
    monkeypatch.setattr(notify, 'inbox_lock', lambda *a: (_ for _ in ()).throw(OSError('locked')))
    for _ in range(5):
        notify.deliver_verdict(tmp_path, {'role': 'review'}, None)
    assert journal.read_text() == previous
    assert not (tmp_path / 'verdict.delivered').exists()


@pytest.mark.parametrize('failure', ['import', 'unconfigured', 'io'])
def test_verdict_retries_are_bounded(tmp_path, monkeypatch, failure, caplog):
    import builtins
    from pathlib import Path
    from plangraph.spawner import notify
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'tools'))
    import seat_verdict as seat
    record = dict(role='review', item_key='demo:A', given_writer_families=['deepseek'])
    monkeypatch.setattr(seat, 'evidence_from_record', lambda *a: (
        dict(served_model='opencode/mimo-v2.6-free', head_sha='d' * 40), 'VERDICT: ACCEPT'))
    monkeypatch.setattr(seat, 'main', lambda *a: seat.EXIT_IO)
    monkeypatch.setenv('PG_REVIEWER_TOKEN_DIR', str(tmp_path))
    keeper = SimpleNamespace(url='unused', item=lambda key: dict(key=key, evidence=[
        dict(kind='commit+ci', verified=True, ref=dict(commit='d' * 40))]))
    if failure == 'unconfigured':
        keeper = None
    if failure == 'import':
        original = builtins.__import__
        def importing(name, *args, **kwargs):
            if name == 'seat_verdict':
                raise ImportError('unavailable')
            return original(name, *args, **kwargs)
        monkeypatch.setattr(builtins, '__import__', importing)
    if failure == 'lock':
        monkeypatch.setattr(notify, 'inbox_lock', lambda *a: (_ for _ in ()).throw(OSError('locked')))
    for _ in range(5):
        notify.deliver_verdict(tmp_path, record, keeper)
    journal = json.loads((tmp_path / 'verdict.json').read_text())
    assert journal['status'] == 'skipped'
    assert journal['attempts'] == 3
    assert (tmp_path / 'verdict.delivered').exists()
    if failure in ('import', 'lock'):
        assert 'verdict delivery deferred' in caplog.text


def _monitor(tmp_path, monkeypatch, output, *, denied=True, role='review', client='opencode', rc=1):
    runner = SpawnerRunner(default_cwd=str(tmp_path))
    job = JobInfo('verdict-job', 'running', 'review', 'test-model', role=role,
                  command=[client], cwd=str(tmp_path))
    if denied:
        job.events.append({'event': 'opencode_tools_denied'})
    runner.jobs[job.job_id] = job
    witnesses = []

    async def collect(*args):
        return output.encode(), b'Error: terminated'

    async def reap(*args):
        pass

    monkeypatch.setattr('plangraph.spawner.run_watchdog.collect', collect)
    monkeypatch.setattr(runner, '_reap_process', reap)
    monkeypatch.setattr(runner, '_check_git_config_guard', lambda *args: None)
    monkeypatch.setattr('plangraph.spawner.opencode_isolation.fail_unchanged_writer', lambda *args: None)
    monkeypatch.setattr('plangraph.spawner.runner._opencode_tools_denied', lambda *args: denied)
    monkeypatch.setattr('plangraph.spawner.gateway_witness.witness',
                        lambda *args: witnesses.append(job.exit_code))
    asyncio.run(runner._monitor_process(job.job_id, SimpleNamespace(returncode=rc), 'test-model'))
    assert job.exit_code == rc
    assert ({'event': 'opencode_tools_denied'} in job.events) == denied
    assert witnesses == ([rc] if denied and role == 'review' and client == 'opencode' else [])
    return job


@pytest.mark.parametrize('verdict', ['ACCEPT', 'REQUEST_CHANGES'])
def test_denied_review_rc1_final_verdict_completed(tmp_path, monkeypatch, verdict):
    job = _monitor(tmp_path, monkeypatch, f'Full review\nVERDICT: {verdict}\n\n')
    assert job.status == 'completed'
    assert not any(event['event'] == 'harness_failed' for event in job.events)


@pytest.mark.parametrize('output', ['Full review', 'VERDICT: ACCEPT\nMore text',
                                     ' VERDICT: ACCEPT', 'VERDICT: ACCEPT ',
                                     'VERDICT: ACCEPTED'])
def test_denied_review_rc1_without_exact_final_verdict_failed(tmp_path, monkeypatch, output):
    assert _monitor(tmp_path, monkeypatch, output).status == 'failed'


@pytest.mark.parametrize('options', [{'denied': False}, {'role': 'implement'},
                                     {'client': 'other'}, {'rc': 2}])
def test_verdict_without_eligible_denied_review_failed(tmp_path, monkeypatch, options):
    assert _monitor(tmp_path, monkeypatch, 'VERDICT: ACCEPT', **options).status == 'failed'


@pytest.mark.parametrize('verdict', ['ACCEPT', 'REQUEST_CHANGES'])
@pytest.mark.parametrize('finished', [False, True])
def test_framed_denied_review_preserves_verdict(tmp_path, monkeypatch, verdict, finished):
    text = f'Full review\nVERDICT: {verdict}'
    output = json.dumps({'type': 'text', 'part': {'text': text}})
    if finished:
        output += '\n' + json.dumps({'type': 'step_finish', 'part': {'reason': 'stop'}})
    job = _monitor(tmp_path, monkeypatch, output)
    assert job.status == 'completed'
    assert job.stdout == text
    assert not any(e['event'] in ('harness_failed', 'terminated_after_answer') for e in job.events)


@pytest.mark.parametrize('rc', [0, 1, 2])
def test_framed_stdout_callers_receive_plain_answer(tmp_path, monkeypatch, rc):
    from plangraph.review.verdict import parse_verdict
    from plangraph.spawner.runner import spawn_service
    from plangraph.spawner.seat_artifact import publish

    text = 'Review complete\nTEST-GAMING: no - searched tests: 0 hits\nVERDICT: ACCEPT'
    output = json.dumps({'type': 'text', 'part': {'text': text}})
    job = _monitor(tmp_path, monkeypatch, output, rc=rc)
    assert job.stdout == text
    assert parse_verdict(job.stdout) == 'pass'
    service = spawn_service(state=tmp_path / 'state')
    service.runner.jobs[job.job_id] = job
    record = dict(run_id='run-framed', job_id=job.job_id, role='review',
                  status=job.status, exit_code=rc, ended=1, attempts=[])
    service._save(record)
    assert record['stdout_tail'] == text
    if rc == 0:
        job.served_model = 'test/model'
        job.prompt_sha256 = 'a' * 64
        job.response_finish_reason = 'stop'
        publish(tmp_path, record, job)
        assert (tmp_path / 'review.txt').read_text() == text
        assert json.loads((tmp_path / 'evidence.json').read_text())['verdict'] == 'pass'
