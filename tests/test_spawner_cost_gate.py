"""Paid admission runs the Rust gate before any harness launch."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.routing_errors import SpawnRefused
from plangraph.spawner.target import admit_daily_budget


@pytest.mark.parametrize('code', [1, 2, 3])
def test_paid_refused_and_output_never_echoed(monkeypatch, code):
    import subprocess
    calls = []
    def run(argv, **options):
        calls.append((argv, options))
        return SimpleNamespace(returncode=code)
    monkeypatch.setattr(subprocess, 'run', run)
    monkeypatch.setenv('PLANGRAPH_BIN', '/private/plangraph')
    paid = replace(DEFAULT_LEGS[0], tier='paid')
    with pytest.raises(SpawnRefused, match='daily cost gate refused'):
        admit_daily_budget(paid)
    assert calls[0][0] == ['/private/plangraph', 'cost-gate', 'admit']
    assert calls[0][1]['timeout'] == 30
    assert calls[0][1]['stdout'] == subprocess.DEVNULL


def test_gate_unavailable_free_and_dry_run(monkeypatch):
    import subprocess
    def missing(*a, **kw):
        raise FileNotFoundError
    monkeypatch.setattr(subprocess, 'run', missing)
    paid = replace(DEFAULT_LEGS[0], tier='paid')
    with pytest.raises(SpawnRefused, match='unavailable'):
        admit_daily_budget(paid)
    admit_daily_budget(paid, dry_run=True)
    admit_daily_budget(replace(paid, tier='subscription'))


def test_service_launch_boundary_refuses_before_runner(tmp_path, monkeypatch):
    from plangraph.spawner.service import SpawnService
    runner = SimpleNamespace(default_cwd=str(tmp_path))
    service = SpawnService(runner=runner, state=tmp_path)
    def refused(*a, **kw):
        raise SpawnRefused('daily cost gate refused paid launch')
    monkeypatch.setattr('plangraph.spawner.target.admit_daily_budget', refused)
    paid = replace(DEFAULT_LEGS[0], tier='paid')
    with pytest.raises(SpawnRefused, match='daily cost gate refused'):
        asyncio.run(service._launch_leg({'cwd':str(tmp_path)}, paid, prompt_record=None, dry_run=False))


def test_catalog_paid_override_and_unknown_billing_gate(monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=1))
    subscription = next(leg for leg in DEFAULT_LEGS if leg.id == 'agy-gemini-3.8-flash')
    monkeypatch.setattr('plangraph.spawner.resolver.get_catalog_models', lambda:
        [{'id':'agy/' + subscription.model,'tier':'paid'}])
    with pytest.raises(SpawnRefused, match='daily cost gate refused'):
        admit_daily_budget(subscription)
    monkeypatch.setattr('plangraph.spawner.resolver.get_catalog_models', lambda: [{'id':'unknown/model'}])
    with pytest.raises(SpawnRefused, match='daily cost gate refused'):
        admit_daily_budget(model='unknown/model')
