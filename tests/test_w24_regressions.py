import ast
import asyncio
import json
from pathlib import Path

import pytest

from plangraph.spawner import resolver, stall_watchdog as watchdog
from plangraph.spawner.catalog_db import CatalogDB


@pytest.mark.parametrize('active', [False, None])
def test_dry_run_combo_refuses_non_admitted_row(monkeypatch, active):
    from plangraph.spawner.runner import SpawnerRunner
    from plangraph.routing_errors import SpawnRefused
    monkeypatch.setattr('plangraph.planner.routing.validate_model', lambda model: model)
    row = dict(id='omniroute/cx-gpt-6.1-sol', combos=['l2-worker'], available=1)
    if active is not None:
        row['active'] = active
    monkeypatch.setattr(resolver, 'get_catalog_models', lambda: [row])
    with pytest.raises(SpawnRefused, match='no admitted legs'):
        asyncio.run(SpawnerRunner().spawn('test', model_or_combo='l2-worker', dry_run=True))


def test_admission_has_one_comparison():
    root = Path(__file__).resolve().parents[1] / 'src/plangraph'
    violations = []
    for folder in ('spawner', 'planner'):
        for path in (root / folder).glob('*.py'):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.name == 'catalog_row_admitted':
                        continue
                    for child in ast.walk(node):
                        if isinstance(child, ast.Compare) and any(
                            isinstance(part, ast.Constant) and part.value == 'available'
                            for part in ast.walk(child)):
                            violations.append((path.name, child.lineno))
    assert violations == []


def test_empty_sqlite_fallback_admits_registered_legs(monkeypatch):
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.routing import validate_model
    db = CatalogDB(':memory:')
    db.conn.executescript(Path(resolver.__file__).with_name('catalog_schema.sql').read_text())
    monkeypatch.setattr(resolver, '_DEFAULT_CATALOG_DB', db)
    rows = {r['id']: r for r in resolver.get_catalog_models()}
    for leg in DEFAULT_LEGS:
        assert resolver.catalog_row_admitted(rows[leg.model])
        assert validate_model(leg.model) == leg.model
    assert not resolver.catalog_row_admitted(rows['cxa/gpt-6-astra-max'])
    assert not resolver.catalog_row_admitted(resolver._normalize_catalog_row({'id': 'missing'}))
    assert not resolver.catalog_row_admitted(resolver._normalize_catalog_row(
        {'id': 'inactive', 'available': 1, 'active': False}))
    db.close()


def test_finished_child_backoff_and_activity_reset(tmp_path):
    run = dict(run_id='run-one', parent_session='parent', ended=0, status='done')
    last = [0]
    def cycle(now):
        return watchdog.tick([run], [], lambda _: last[0], now=now,
                             deliver=lambda e: watchdog.deliver_event(tmp_path, e, now=now))
    def sent():
        return sorted(json.loads(p.read_text())['sent'] for p in
                      (tmp_path / 'watchdog/alerts').glob('*.json'))
    cycle(301)
    cycle(360)
    assert max(sent()) == 301
    cycle(601)
    assert max(sent()) == 601
    for now in (1201, 2401, 4801, 8401):
        cycle(now)
        assert max(sent()) == now
        cycle(now + 1)
        assert max(sent()) == now
    last[0] = 8500
    cycle(8501)
    assert not list((tmp_path / 'watchdog/alerts').glob('*.json'))
    run['run_id'] = 'run-two'
    run['ended'] = 8600
    cycle(9001)
    assert max(sent()) == 9001
    run['_completion_acknowledged'] = True
    cycle(9002)
    assert not list((tmp_path / 'watchdog/alerts').glob('*.json'))
