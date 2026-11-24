"""The scheduled review probe books an OpenCode leg's usage-limit reply as limited and exits 0
(LE-REVIEW-PROBE-UNIT-FAILS-ON-LIMITED-LEGS); a failed leg still exits 1."""
import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from plangraph.planner import legs
from plangraph.routing_support.leg_state import LegStateStore
from plangraph.spawner import cli, leg_probe

LIMIT_REPLY = json.dumps({'type': 'error', 'error': {'status': 429, 'retryAfterMs': 3_600_000}})


def _run(tmp_path, monkeypatch, capsys, reply, leg_ids=('opencode-go-deepseek-v4-flash',)):
    selected = tuple(leg for leg in legs.routed_review_legs() if leg.id in leg_ids)
    assert len(selected) == len(leg_ids)
    monkeypatch.setattr(legs, 'routed_review_legs', lambda: selected)
    monkeypatch.setattr(leg_probe.shutil, 'which', lambda *a, **kw: '/bin/opencode')
    monkeypatch.setattr(leg_probe, '_check_manage_key', lambda runner: None)
    store = LegStateStore(tmp_path / 'legs.sqlite')
    calls = []

    async def spawn(**kwargs):
        calls.append(kwargs['model_or_combo'])
        stdout, stderr = reply(kwargs['model_or_combo'])
        return SimpleNamespace(job_id='j', status='failed', exit_code=1, stdout=stdout, stderr=stderr)

    async def cancel(job_id):
        pass

    runner = SimpleNamespace(leg_reader=store, spawn=spawn, cancel=cancel)
    monkeypatch.setattr(cli, 'SpawnService', lambda **kw: SimpleNamespace(runner=runner))

    def sweep():
        code = cli.main(['--state', str(tmp_path), 'probe', '--all-reviewers', '--json'])
        return code, json.loads(capsys.readouterr().out)

    return sweep, calls, store, selected


def test_an_opencode_usage_limit_reply_is_limited_and_the_unit_exits_zero(tmp_path, monkeypatch, capsys):
    sweep, calls, store, (leg,) = _run(tmp_path, monkeypatch, capsys, lambda model: (LIMIT_REPLY, ''))
    code, reply = sweep()
    assert (code, reply['ok'], reply['limited_legs']) == (0, True, [leg.id])
    assert store.leg_status(leg_probe.key(leg), harness='opencode')['state'] == 'limited'
    # The next run skips the booked leg without spawning it, and still exits 0.
    code, reply = sweep()
    assert (code, reply['probes'][0]['skipped']) == (0, 'limited') and len(calls) == 1


def test_a_failed_opencode_leg_still_fails_the_unit(tmp_path, monkeypatch, capsys):
    sweep, calls, store, (leg,) = _run(tmp_path, monkeypatch, capsys, lambda model: ('', 'connection reset by peer'))
    code, reply = sweep()
    assert (code, reply['ok'], reply['limited_legs']) == (1, False, [])
    assert store.leg_status(leg_probe.key(leg), harness='opencode')['state'] != 'limited'


def test_a_limited_leg_beside_a_failed_leg_still_fails_the_unit(tmp_path, monkeypatch, capsys):
    ids = ('opencode-go-deepseek-v4-flash', 'opencode-go-glm-5.3-flash')
    sweep, calls, store, selected = _run(
        tmp_path, monkeypatch, capsys,
        lambda model: (LIMIT_REPLY, '') if 'deepseek' in model else ('', 'connection reset by peer'), ids)
    code, reply = sweep()
    assert (code, reply['limited_legs']) == (1, [selected[0].id])


def test_any_skipped_row_does_not_fail_the_sweep(tmp_path, monkeypatch):
    selected = tuple(leg for leg in legs.routed_review_legs() if leg.harness == 'opencode')[:2]
    monkeypatch.setattr(legs, 'routed_review_legs', lambda: selected)
    monkeypatch.setattr(leg_probe.shutil, 'which', lambda *a, **kw: '/bin/opencode')
    monkeypatch.setattr(leg_probe, '_check_manage_key', lambda runner: None)

    async def probe(runner, leg, **kwargs):
        return dict(ok=False, reason='held', seconds=0.0, infra=False, skipped='held') if leg is selected[0] \
            else dict(ok=True, reason='', seconds=0.0, infra=False)

    monkeypatch.setattr(leg_probe, 'probe', probe)
    import asyncio
    reply = asyncio.run(leg_probe.probe_many(SimpleNamespace(leg_reader=LegStateStore(':memory:')),
                                             all_reviewers=True))
    assert reply['ok'] and reply['limited_legs'] == []


@pytest.mark.skipif(os.name != 'posix', reason='shell stub')
def test_a_limit_frame_after_a_partial_answer_is_still_limited_through_the_real_runner(tmp_path, monkeypatch):
    """The runner's display output keeps only the partial answer; the confirmed refusal still books the leg."""
    from plangraph.spawner.launch_spec import _LaunchOwned  # noqa: F401
    from plangraph.spawner.opencode_isolation import GO_AUTH_DB_ENV
    from plangraph.spawner.runner import SpawnerRunner
    from test_opencode_isolation_go import source_store
    leg = next(row for row in legs.routed_review_legs() if row.id == 'opencode-go-deepseek-v4-flash')
    text = json.dumps({'type': 'text', 'part': {'type': 'text', 'text': 'Looking at the diff'}})
    error = json.dumps({'type': 'error', 'error': {'status': 429, 'retryAfterMs': 60000}})
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    fake = bin_dir / 'opencode'
    fake.write_text(f"#!/bin/sh\ncat >/dev/null\nprintf '%s\\n' '{text}'\nsleep 1\nprintf '%s\\n' '{error}'\nsleep 5\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setenv('PATH', f'{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin')
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.delenv('INVOCATION_ID', raising=False)
    source = tmp_path / 'source.db'
    source_store(source)
    monkeypatch.setenv(GO_AUTH_DB_ENV, str(source))
    store = LegStateStore(tmp_path / 'legs.sqlite')
    runner = SpawnerRunner(default_cwd=str(tmp_path))
    runner.leg_reader = store
    reply = asyncio.run(leg_probe.probe(runner, leg, timeout_s=30, store=store, role='review'))
    assert reply['reason'] == 'limited' and reply['skipped'] == 'limited', reply
    assert store.leg_status(leg_probe.key(leg), harness='opencode')['state'] == 'limited'
