import asyncio
import json
import logging
import sqlite3

import pytest

from plangraph.spawner import resolver, stall_watchdog
from plangraph.spawner.catalog_db import CatalogDB
from plangraph.planner.routing import SpawnRefused, validate_model
from plangraph.spawner.runner import SpawnerRunner


@pytest.mark.parametrize('source', ['list', 'get', 'factory', 'corrupt'])
def test_catalog_db_errors_use_registered_offline_rows(tmp_path, monkeypatch, caplog, source):
    class Broken:
        def list_models(self, **kwargs):
            raise sqlite3.OperationalError('broken catalog')

    db = Broken()
    if source == 'get':
        class Getter:
            def get_models(self):
                raise OSError('broken catalog')
        db = Getter()
    if source == 'corrupt':
        path = tmp_path / 'catalog.db'
        path.write_bytes(b'not a sqlite database')
        db = None
    def factory():
        if source == 'factory':
            raise RuntimeError('broken catalog')
        return CatalogDB(path) if source == 'corrupt' else db
    monkeypatch.setattr(resolver, '_get_default_catalog_db', factory)
    try:
        with caplog.at_level(logging.WARNING):
            rows = resolver.get_catalog_models(active_only=True)
            for _ in range(4):
                assert resolver.get_catalog_models(active_only=True) == rows
        expected = {r['id'] for r in resolver.DEFAULT_FALLBACK_MODELS if resolver.catalog_row_admitted(r)}
        assert {r['id'] for r in rows} == expected
        assert rows and all(r['catalog_source'] == 'offline_fallback_db_error' for r in rows)
        alerts = [r for r in caplog.records if 'catalog_db_error fallback=offline err=' in r.message]
        assert len(alerts) == 1
        assert validate_model('mimo/mimo-v2.6-flash') == 'mimo/mimo-v2.6-flash'
        with pytest.raises(SpawnRefused):
            validate_model('unregistered/model')
        result = asyncio.run(SpawnerRunner().spawn('test', model_or_combo='mimo/mimo-v2.6-flash', dry_run=True))
        assert result
        with pytest.raises(SpawnRefused):
            asyncio.run(SpawnerRunner().spawn('test', model_or_combo='unregistered/model', dry_run=True))
        from plangraph.spawner.cli import main
        for model, rc in [('mimo/mimo-v2.6-flash', 0), ('unregistered/model', 2)]:
            assert main(['--state', str(tmp_path), 'spawn', 'test', '--card', '{"estimate_tokens":100}',
                         '--model', model, '--override-reason', 'test fallback admission', '--dry-run']) == rc
    finally:
        if source == 'corrupt' and db is not None:
            db.close()


@pytest.mark.parametrize('field', ['sent', 'count'])
def test_boolean_numeric_stamp_is_quarantined(tmp_path, field):
    path = tmp_path / 'stamp.json'
    path.write_text(json.dumps(dict(dict(sent=1, count=1), **{field: True})))
    assert stall_watchdog._read_state(tmp_path, path, stamp=True) is None
    assert path.with_suffix('.json.corrupt').exists()


def test_card_cannot_assert_review_exemption():
    with pytest.raises(ValueError, match='caller.*review_exempt'):
        resolver.resolve_review_plan('test/writer', review_exempt=True,
                                     paths=['README.md'], is_pure_doc=True, bucket='S0',
                                     reviewer_count=0)
