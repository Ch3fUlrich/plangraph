"""D-2655: policy admission and Linux systemd installation doors."""
from pathlib import Path

import pytest

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.routing import SpawnRefused, validate_model
from plangraph.spawner import resolver, stall_watchdog


OFF_LIST = (
    'omniroute/ovh-gpt-oss-120b',
    'omniroute/or-north-mini-code-free',
    'omniroute/or-laguna-s-2.1-free',
    'omniroute/or-nemotron-3-super-free',
    'opencode/muse-spark-1.3-contributor-free',
)


def test_empty_sqlite_policy_only(monkeypatch):
    from plangraph.spawner.catalog_db import CatalogDB
    db = CatalogDB(':memory:')
    db.conn.executescript(Path(resolver.__file__).with_name('catalog_schema.sql').read_text())
    monkeypatch.setattr(resolver, '_get_default_catalog_db', lambda: db)
    allowed = {leg.model for leg in DEFAULT_LEGS}
    assert not allowed.intersection(OFF_LIST)
    assert {r['id'] for r in resolver.get_catalog_models(active_only=True)} == allowed
    for model in allowed:
        assert validate_model(model) == model
    for model in OFF_LIST:
        with pytest.raises(SpawnRefused):
            validate_model(model)


@pytest.mark.parametrize('model', OFF_LIST)
def test_active_off_list_row_refused(model):
    assert not resolver.catalog_row_admitted(dict(id=model, active=True, available=1))


def test_linux_working_directory_unquoted(tmp_path):
    root = Path(__file__).resolve().parents[1]
    stall_watchdog.install('linux', tmp_path, root=root, run=lambda *a, **k: None)
    service = (tmp_path / 'plangraph-stall-watchdog.service').read_text()
    assert f'WorkingDirectory={root}\n' in service


def test_linux_whitespace_root_refused(tmp_path):
    with pytest.raises(ValueError, match='whitespace'):
        stall_watchdog.install('linux', tmp_path / 'units', root=tmp_path / 'has space',
                               run=lambda *a, **k: pytest.fail('must not run systemctl'))
