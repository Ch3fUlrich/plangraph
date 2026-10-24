import asyncio
import json

import pytest

from plangraph.spawner import resolver, stall_watchdog
from plangraph.spawner.catalog_db import CatalogDB
from plangraph.planner.routing import SpawnRefused, validate_model
from plangraph.spawner.runner import SpawnerRunner


@pytest.mark.parametrize('real', [False, True], ids=['double', 'sqlite'])
@pytest.mark.parametrize('mode', ['inactive', 'empty', 'mixed'])
def test_catalog_total_rows_control_fallback(tmp_path, monkeypatch, real, mode):
    model = 'mimo/mimo-v2.6-flash'
    if real:
        db = CatalogDB(tmp_path / 'catalog.db')
        db.init_and_seed()
        db.conn.execute("UPDATE catalog_models SET status = 'retired'")
        if mode == 'empty':
            db.conn.execute('DELETE FROM catalog_route_legs')
            db.conn.execute('DELETE FROM catalog_benchmarks')
            db.conn.execute('DELETE FROM catalog_models')
        elif mode == 'mixed':
            db.conn.execute("UPDATE catalog_models SET status = 'active', available = 1 WHERE model_id = ?", (model,))
        db.conn.commit()
    else:
        class Catalog:
            calls = []

            def list_models(self, *, active_only):
                self.calls.append(active_only)
                rows = [] if mode == 'empty' else [
                    dict(id=model, active=mode == 'mixed', available=1),
                    dict(id='test/inactive', active=False, available=1)]
                return [r for r in rows if resolver.catalog_row_admitted(r)] if active_only else rows
        db = Catalog()
    try:
        monkeypatch.setattr(resolver, '_get_default_catalog_db', lambda: db)
        rows = resolver.get_catalog_models(active_only=True)
        if mode == 'inactive':
            assert rows == []
            with pytest.raises(SpawnRefused):
                validate_model(model)
            with pytest.raises(SpawnRefused):
                asyncio.run(SpawnerRunner().spawn('test', model_or_combo=model, dry_run=True))
        elif mode == 'empty':
            assert rows == [resolver._normalize_catalog_row(r) for r in resolver.DEFAULT_FALLBACK_MODELS
                            if resolver.catalog_row_admitted(r)]
            assert rows and all(r['active'] and r['available'] for r in rows)
            assert validate_model(model) == model
        else:
            assert [r['id'] for r in rows] == [model]
        if not real:
            assert True in db.calls
            if mode == 'mixed':
                assert False not in db.calls
    finally:
        if real:
            db.close()


def test_watchdog_boolean_stamp_count_is_quarantined(tmp_path):
    path = tmp_path / 'stamp.json'
    path.write_text(json.dumps(dict(sent=1, count=True)))
    assert stall_watchdog._read_state(tmp_path, path, stamp=True) is None
    assert path.with_suffix('.json.corrupt').exists()


def test_watchdog_forwards_original_outbox_bytes(tmp_path):
    folder = tmp_path / 'watchdog/outbox'
    folder.mkdir(parents=True)
    path = folder / 'event.json'
    payload = b'{ "z": 1, "message": "\\u00e9" }\n'
    path.write_bytes(payload)
    calls = []
    stall_watchdog.flush_outbox(tmp_path, ['bridge'], run=lambda *a, **kw: calls.append(kw))
    assert calls[0]['input'] == payload
    assert not calls[0].get('text', False)
    assert not path.exists()
