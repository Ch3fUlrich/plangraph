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
    allowed = {leg.model for leg in DEFAULT_LEGS} - set(OFF_LIST)
    assert resolver.admission_models() == allowed
    assert {r['id'] for r in resolver.get_catalog_models(active_only=True)} == allowed
    for model in allowed:
        assert validate_model(model) == model
    for model in OFF_LIST:
        with pytest.raises(SpawnRefused):
            validate_model(model)


@pytest.mark.parametrize('model', OFF_LIST)
def test_active_off_list_row_refused(model):
    assert not resolver.catalog_row_admitted(dict(id=model, active=True, available=1))


def test_custom_catalog_row_admitted(monkeypatch):
    row = dict(id='test/custom', active=True, available=1)
    assert resolver.catalog_row_admitted(row)
    monkeypatch.setattr(resolver, 'get_catalog_models', lambda: [row])
    assert validate_model('test/custom') == 'test/custom'


@pytest.mark.parametrize('model', OFF_LIST)
def test_removed_model_refused_even_without_catalog_row(monkeypatch, model):
    monkeypatch.setattr(resolver, 'get_catalog_models', lambda: [])
    with pytest.raises(SpawnRefused):
        validate_model(model)


def test_seeded_removed_catalog_row_refused(monkeypatch):
    from plangraph.spawner.catalog_db import CatalogDB
    db = CatalogDB(':memory:')
    db.conn.executescript(Path(resolver.__file__).with_name('catalog_schema.sql').read_text())
    db.conn.execute("INSERT INTO catalog_providers "
                    "(provider_id, tier, transport, updated_at) VALUES ('omniroute', 'free', 'cli', 'now')")
    db.conn.execute("INSERT INTO catalog_models "
                    "(model_id, provider_id, upstream_id, family, context_tokens, max_output_tokens, status, available) "
                    "VALUES (?, 'omniroute', 'ovh', 'openai', 128000, 1000, 'active', 1)",
                    (OFF_LIST[0],))
    db.conn.commit()
    monkeypatch.setattr(resolver, '_get_default_catalog_db', lambda: db)
    assert resolver.get_catalog_models(active_only=True) == []
    with pytest.raises(SpawnRefused):
        validate_model(OFF_LIST[0])
    db.close()


@pytest.mark.parametrize('model,leg', [
    (OFF_LIST[0], 'ovh-gpt-oss-120b'),
    (OFF_LIST[-1], 'opencode-zen-free'),
])
@pytest.mark.parametrize('flag', ['--model', '--leg'])
def test_cli_removed_model_refused(tmp_path, capsys, model, leg, flag):
    from plangraph.spawner.cli import main
    assert main(['--state', str(tmp_path), 'spawn', 'task', '--card',
                 '{"estimate_tokens": 1000}', flag, model if flag == '--model' else leg,
                 '--override-reason', 'operator regression check', '--dry-run', '--json']) == 2
    assert 'unknown model: ' + model in capsys.readouterr().err


def test_linux_working_directory_unquoted(tmp_path):
    root = Path(__file__).resolve().parents[1]
    stall_watchdog.install('linux', tmp_path, root=root, run=lambda *a, **k: None)
    service = (tmp_path / 'plangraph-stall-watchdog.service').read_text()
    assert f'WorkingDirectory={root}\n' in service


def test_linux_whitespace_root_refused(tmp_path):
    with pytest.raises(ValueError, match='whitespace'):
        stall_watchdog.install('linux', tmp_path / 'units', root=tmp_path / 'has space',
                               run=lambda *a, **k: pytest.fail('must not run systemctl'))
