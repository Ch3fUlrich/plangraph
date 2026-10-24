import asyncio
import json
from pathlib import Path

import pytest

from plangraph.spawner import resolver, stall_watchdog as watchdog


@pytest.mark.parametrize('damage', ['invalid', 'empty', 'list', 'unreadable'])
@pytest.mark.parametrize('operation', ['stamp', 'clear-stamp', 'clear-outbox', 'outbox'])
def test_watchdog_corrupt_state_skipped_and_recorded(tmp_path, monkeypatch, damage, operation):
    event = dict(rule='finished-child', run_id='run-one', kind='alert', recipient='routing')
    watchdog.deliver_event(tmp_path, event, now=1)
    folder = 'outbox' if 'outbox' in operation else 'alerts'
    path = next((tmp_path / 'watchdog' / folder).glob('*.json'))
    path.write_text({'invalid': '{', 'empty': '', 'list': '[]', 'unreadable': '{}'}[damage])
    if damage == 'unreadable':
        original = Path.read_text
        def denied(self, *args, **kwargs):
            if self == path:
                raise PermissionError('permission denied')
            return original(self, *args, **kwargs)
        monkeypatch.setattr(Path, 'read_text', denied)
    delivered = []
    for _ in range(2):
        if operation.startswith('clear'):
            watchdog.deliver_event(tmp_path, dict(event, kind='clear'), now=4000)
        elif operation == 'stamp':
            watchdog.deliver_event(tmp_path, event, now=4000)
        else:
            watchdog.flush_outbox(tmp_path, ['bridge'], run=lambda *a, **kw: delivered.append(kw))
    records = json.loads((tmp_path / 'watchdog/bad-state.json').read_text())
    assert [r['path'] for r in records].count(str(path)) == 1
    assert not delivered
    assert not path.exists() or operation == 'stamp'


def test_empty_catalog_uses_admitted_fallback():
    class Catalog:
        def list_models(self, *, active_only):
            assert active_only is True
            return []
    assert resolver.get_catalog_models(Catalog(), active_only=True) == [
        resolver._normalize_catalog_row(r) for r in resolver.DEFAULT_FALLBACK_MODELS
        if resolver.catalog_row_admitted(r)]


def test_nonempty_catalog_does_not_mix_fallback():
    row = dict(id='test/only', active=True, available=1)
    class Catalog:
        def list_models(self, *, active_only):
            assert active_only is True
            return [row, dict(id='test/inactive', active=False, available=1)]
    assert resolver.get_catalog_models(Catalog(), active_only=True) == [resolver._normalize_catalog_row(row)]


def test_spawn_refused_identity_and_dry_run_combo_record(tmp_path, monkeypatch):
    from plangraph.planner import routing
    from plangraph import routing_errors
    from plangraph.spawner.runner import spawn_service, SpawnerRunner
    assert routing.SpawnRefused is routing_errors.SpawnRefused
    monkeypatch.setattr(routing, 'validate_model', lambda model: model)
    monkeypatch.setattr(resolver, 'get_catalog_models', lambda: [])
    service = spawn_service(state=tmp_path)
    with pytest.raises(routing.SpawnRefused, match='no admitted legs') as caught:
        asyncio.run(SpawnerRunner().spawn('test', model_or_combo='l2-worker', dry_run=True))
    service.save_refusal(caught.value)
    record = service.ps()['runs'][0]
    assert record['status'] == 'refused'
    assert record['exit_code'] == 2
