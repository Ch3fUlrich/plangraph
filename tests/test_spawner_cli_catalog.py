import json

from plangraph.spawner import cli
from plangraph.routing_support import resolver


def test_catalog_is_read_only_projection(monkeypatch, capsys):
    rows = [dict(id='registered', model='provider/model', roles=['review'], secret='omit')]
    monkeypatch.setattr(resolver, 'get_catalog_models', lambda: rows)
    monkeypatch.setattr(cli, 'SpawnService', lambda **kw: (_ for _ in ()).throw(AssertionError('service invoked')))
    assert cli.main(['catalog']) == 0
    assert json.loads(capsys.readouterr().out) == [
        dict(id='registered', model='provider/model', roles=['review'])]
    assert rows[0]['secret'] == 'omit'


def test_catalog_missing_role_metadata_is_not_inferred(monkeypatch, capsys):
    monkeypatch.setattr(resolver, 'get_catalog_models', lambda: [dict(id='catalog-only')])
    assert cli.main(['catalog']) == 0
    assert json.loads(capsys.readouterr().out) == [dict(id='catalog-only', model='catalog-only', roles=[])]
