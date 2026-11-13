"""Supervisor exit status includes independently refused seats, even after admission."""
import asyncio
import io
import json

import pytest


@pytest.mark.parametrize('grouped', [False, True])
@pytest.mark.parametrize('delayed', [False, True])
@pytest.mark.parametrize('reason', [None, 'base_stale: behind=2', 'base_stale: fetch_failed', 'harness_failed'])
def test_supervisor_refuses_any_stale_seat(monkeypatch, grouped, delayed, reason):
    from plangraph.spawner import cli
    records = [dict(run_id='run-healthy', status='running', exit_code=None),
               dict(run_id='run-checked', status='running', exit_code=None)]
    class Service:
        def __init__(self, **kwargs):
            self.tasks = {}
        async def spawn(self, **kwargs):
            async def finish(record, failure):
                await asyncio.sleep(0)
                record.update(status='failed' if failure else 'completed',
                              exit_code=1 if failure else 0, reason=failure)
            for record, failure in zip(records, [None, reason]):
                self.tasks[record['run_id']] = asyncio.create_task(finish(record, failure))
            if not delayed:
                await asyncio.gather(*self.tasks.values())
            # Real spawn returns persisted copies, not the task's mutable records.
            return json.loads(json.dumps(dict(seats=records, reason='seat_group') if grouped else records[1]))
        def result(self, run_id):
            return dict(next(record for record in records if record['run_id'] == run_id))
    class Output(io.StringIO):
        def close(self):
            pass
    output = Output()
    monkeypatch.setattr(cli, 'SpawnService', Service)
    monkeypatch.setattr(cli.sys, 'stdin', io.StringIO('{"state":"unused"}'))
    monkeypatch.setattr(cli.sys, 'stdout', output)
    assert cli.main(['--supervise']) == int(str(reason).startswith('base_stale:'))
    assert records[1]['status'] == ('failed' if reason else 'completed')
    if grouped:
        assert json.loads(output.getvalue())['reason'] == 'seat_group'
        assert records[0]['status'] == 'completed' and records[0]['exit_code'] == 0
