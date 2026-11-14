"""Job IDs must not alias gateway witnesses across launches."""
from plangraph.spawner import gateway_witness as gw


def test_job_session_tags_keep_uuid_entropy(tmp_path, monkeypatch):
    import asyncio
    import uuid
    import itertools
    from plangraph.spawner import runner as module
    ids = (uuid.UUID(f'12345678-0000-4000-8000-{i:012x}') for i in itertools.count(1))
    monkeypatch.setattr(module.uuid, 'uuid4', lambda: next(ids))
    runner = module.SpawnerRunner(default_cwd=str(tmp_path))
    async def run():
        return [await runner.spawn('prompt', model_or_combo='omniroute/cx-gpt-6-luna',
                                  client='opencode', dry_run=True) for _ in range(2)]
    jobs = asyncio.run(run())
    assert gw.session_tag(jobs[0].job_id) != gw.session_tag(jobs[1].job_id)
