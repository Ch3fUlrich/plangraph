"""Paid admission runs the Rust gate before any harness launch."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path
import os

import pytest

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.routing_errors import SpawnRefused
from plangraph.spawner.target import admit_daily_budget


@pytest.fixture(autouse=True)
def private_account_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    if os.name == 'posix':
        import pwd
        monkeypatch.setattr(pwd, 'getpwuid', lambda _: SimpleNamespace(pw_dir=str(tmp_path)))


@pytest.mark.parametrize('code', [1, 2, 3])
def test_paid_refused_and_output_never_echoed(monkeypatch, tmp_path, code):
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
    assert Path(calls[0][0][0]).parent == tmp_path / '.local/bin'
    assert calls[0][0][1:] == ['cost-gate', 'admit']
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
    with pytest.raises(SpawnRefused, match='unavailable'):
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


@pytest.mark.parametrize('dry_run', [False, True])
def test_caller_cannot_choose_successful_gate_or_skip_preview(monkeypatch, tmp_path, dry_run):
    import subprocess
    untrusted = str(tmp_path / 'always-accept')
    monkeypatch.setenv('PLANGRAPH_BIN', untrusted)
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0 if argv[0] == untrusted else 1)
    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(SpawnRefused, match='daily cost gate refused'):
        admit_daily_budget(replace(DEFAULT_LEGS[0], tier='paid'), dry_run=dry_run)
    assert calls and calls[0][0] != untrusted
    assert ('--dry-run' in calls[0]) == dry_run


def test_unadmitted_combo_is_refused_before_budget(monkeypatch, tmp_path):
    from plangraph.planner import routing
    from plangraph.spawner.runner import SpawnerRunner
    monkeypatch.setattr(routing, 'validate_model', lambda model: model)
    monkeypatch.setattr('plangraph.spawner.resolver.get_catalog_models', lambda: [])
    def gate(*args, **kwargs):
        pytest.fail('an unadmitted combo must be refused before budget checks')
    monkeypatch.setattr('plangraph.spawner.target.admit_daily_budget', gate)
    with pytest.raises(SpawnRefused, match='no admitted legs for combo'):
        asyncio.run(SpawnerRunner(default_cwd=str(tmp_path)).spawn(
            'fixture', model_or_combo='l2-worker', dry_run=True))


def test_admitted_paid_preview_still_requires_budget(monkeypatch, tmp_path):
    from plangraph.spawner.runner import SpawnerRunner
    paid = next(leg for leg in DEFAULT_LEGS if leg.id == 'deepseek')
    calls = []
    def gate(leg, **kwargs):
        calls.append((leg, kwargs))
        raise SpawnRefused('fixture gate refused')
    monkeypatch.setattr('plangraph.spawner.target.admit_daily_budget', gate)
    monkeypatch.setattr('plangraph.spawner.resolver.get_catalog_models', lambda: [])
    with pytest.raises(SpawnRefused, match='fixture gate refused'):
        asyncio.run(SpawnerRunner(default_cwd=str(tmp_path)).spawn(
            'fixture', model_or_combo=paid.model, dry_run=True))
    assert calls and calls[0][1]['dry_run'] is True
