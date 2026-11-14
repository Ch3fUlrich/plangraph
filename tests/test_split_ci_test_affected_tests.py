from pathlib import Path
import pytest
from test_affected_tests import selector

def test_ci_pr_uses_affected_and_nightly_keeps_full():
    import yaml

    workflow = yaml.safe_load((Path(__file__).resolve().parents[1]
                              / ".github/workflows/tests.yml").read_text())
    # pytest job runs all affected tests
    pytest_steps = workflow["jobs"]["pytest"]["steps"]
    pytest_checkout = next(s for s in pytest_steps if s.get("uses") == "actions/checkout@v4")
    assert pytest_checkout["with"]["fetch-depth"] == 0
    pytest_cmd = next(s["run"] for s in pytest_steps if s.get("name", "").startswith("pytest"))
    assert 'pull_request' in pytest_cmd
    assert 'tools/affected_tests.py --base "$BASE_SHA" --run -n auto' in pytest_cmd
    assert 'python -m pytest -q -n auto' in pytest_cmd

    # pytest-postgres job restricts affected tests via --within
    pg_steps = workflow["jobs"]["pytest-postgres"]["steps"]
    pg_checkout = next(s for s in pg_steps if s.get("uses") == "actions/checkout@v4")
    assert pg_checkout["with"]["fetch-depth"] == 0
    pg_cmd = next(s["run"] for s in pg_steps if s.get("name", "").startswith("pytest"))
    assert 'pull_request' in pg_cmd
    assert '--within tests/contract tests/test_keeper_pg_unit.py tests/test_keeper_pg_parity.py tests/test_keeper_pg_copy.py tests/test_projector_ddl.py tests/test_worker_claim.py tests/test_claim_parity_dispatch.py $FILES' in pg_cmd
    assert 'tools/affected_tests.py --base "$BASE_SHA"' in pg_cmd
    # On push to main, the full PG set also includes claim contract coverage.
    assert 'tests/test_projector_ddl.py tests/test_worker_claim.py tests/test_claim_parity_dispatch.py $FILES' in pg_cmd
    assert 'python -m pytest -q -n auto --dist loadgroup --durations=30 tests/contract tests/test_keeper_pg_unit.py tests/test_keeper_pg_parity.py tests/test_keeper_pg_copy.py' in pg_cmd

    # PyYAML's YAML 1.1 loader treats the GitHub key `on` as boolean True.
    assert workflow.get("on", workflow.get(True))["schedule"]


def test_missing_pytest_has_named_fix(tmp_path, monkeypatch, capsys):
    import sys
    sel = selector()
    monkeypatch.setattr(sel, 'select_tests', lambda *a, **k: {'tests': ['tests/test_x.py']})
    monkeypatch.setattr(sel.importlib.util, 'find_spec', lambda name: None)
    monkeypatch.setattr(sel.subprocess, 'run', lambda *a, **k: pytest.fail('pytest process started'))
    monkeypatch.setattr(sys, 'argv', ['affected_tests.py', '--base', 'HEAD', '--changed', 'src/x.py', '--run'])
    assert sel.main(root=tmp_path) == 1
    out = capsys.readouterr()
    assert out.err.count('FAIL pytest-missing:') == 1
    assert '-m pip install' in out.err


@pytest.mark.parametrize('broad', ['.github/workflows/tests.yml', 'tests/conftest.py', 'tests/helpers.py', 'pyproject.toml'])
def test_local_selection_keeps_changed_tests_and_reverse_consumers(tmp_path, broad):
    files = {'src/pkg/core.py': 'VALUE = 1\n',
             'tests/test_api.py': 'from pkg.core import VALUE\n',
             'tests/test_written.py': 'def test_written(): pass\n',
             'tests/test_contract.py': repr(broad) + '\n',
             'tests/test_unrelated.py': 'def test_unrelated(): pass\n'}
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    module = selector()
    changed = [broad, 'src/pkg/core.py', 'tests/test_written.py']
    assert module.select_tests(tmp_path, changed)['tests'] == ['tests']
    plan = module.select_tests(tmp_path, changed, files_only=True)
    assert set(plan['tests']) == {'tests/test_api.py', 'tests/test_written.py', 'tests/test_contract.py'}
    assert all((tmp_path / path).is_file() for path in plan['tests'])


def test_local_shared_controls_keep_all_named_consumers(tmp_path):
    directory = tmp_path / 'tests'
    directory.mkdir()
    for index in range(30):
        (directory / f'test_ref_{index:02}.py').write_text('"tests/conftest.py"\n')
    (directory / 'test_written.py').write_text('def test_written(): pass\n')
    module = selector()
    plan = module.select_tests(tmp_path, ['tests/conftest.py', 'tests/test_written.py'], files_only=True)
    assert 'tests/test_written.py' in plan['tests']
    assert len(plan['tests']) == 31
    assert 'tests' not in plan['tests']


def test_local_unreadable_graph_refuses_instead_of_full_collection(tmp_path, monkeypatch, capsys):
    import sys
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests/test_broken.py').write_text('def invalid(\n')
    module = selector()
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **k: pytest.fail('unreadable graph launched pytest'))
    monkeypatch.setattr(sys, 'argv', ['affected_tests.py', '--base', 'HEAD', '--changed', 'tests/test_broken.py', '--files-only', '--run'])
    assert module.main(root=tmp_path) == 1
    assert 'local-selection' in capsys.readouterr().err


import importlib.util
import subprocess

MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "ci_select.py"


def test_three_shards_cover_only_affected_selection(tmp_path, shard_selector):
    import inspect
    import os
    import sys
    spec = importlib.util.spec_from_file_location('shard_affected', MODULE_PATH.with_name('affected_tests.py'))
    affected = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(affected)
    package = tmp_path / 'src/pkg'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('')
    (package / 'one.py').write_text('VALUE = 1\n')
    tests = tmp_path / 'tests'
    tests.mkdir()
    (tests / 'test_selected.py').write_text('from pkg.one import VALUE\ndef test_selected():\n    assert VALUE == 1\n')
    (tests / 'test_unselected.py').write_text('raise RuntimeError("unaffected file must never be collected")\n')
    plan = affected.select_tests(tmp_path, ['src/pkg/one.py'])
    assert plan['tests'] == ['tests/test_selected.py']
    import conftest
    hooks = vars(conftest)
    source = 'import os, sys, hashlib, pytest\nfrom pathlib import Path\n_PG = False\n'
    source += '\n'.join(inspect.getsource(hooks[name]) for name in (
        'shard_items', 'pytest_collection_modifyitems', 'pytest_sessionfinish') if name in hooks)
    source += '\ndef pytest_runtest_logreport(report):\n    if report.when == "call":\n        with Path(os.environ["SHARD_TRACE"]).open("a") as stream:\n            stream.write(report.nodeid + "\\n")\n'
    (tmp_path / 'conftest.py').write_text(source)
    (tmp_path / 'pytest.ini').write_text('[pytest]\n')
    executed, empty = [], 0
    for index in range(3):
        trace = tmp_path / f'trace-{index}'
        env = dict(os.environ, PYTEST_SHARD=f'{index}/3', PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',
                   PYTHONPATH=os.pathsep.join((str(tmp_path / 'src'), str(MODULE_PATH.parents[1] / 'tests'))), SHARD_TRACE=str(trace))
        result = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-c', str(tmp_path / 'pytest.ini'),
            '--confcutdir', str(tmp_path), *plan['tests']], cwd=tmp_path, env=env,
            capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stdout + result.stderr
        ran = trace.read_text().splitlines() if trace.exists() else []
        empty += not ran
        executed.extend(ran)
    assert empty == 2
    assert executed == ['tests/test_selected.py::test_selected']


@pytest.mark.parametrize('shard', ['0/3', '1/3', '2/3'])
def test_empty_affected_selection_never_invokes_default_collection(tmp_path, monkeypatch, capsys, shard):
    import sys
    spec = importlib.util.spec_from_file_location('empty_affected', MODULE_PATH.with_name('affected_tests.py'))
    affected = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(affected)
    monkeypatch.setenv('PYTEST_SHARD', shard)
    monkeypatch.setattr(affected, 'select_tests', lambda *a, **k: {'tests': []})
    monkeypatch.setattr(affected.subprocess, 'run', lambda *a, **k: pytest.fail('empty selection started pytest'))
    monkeypatch.setattr(sys, 'argv', ['affected_tests.py', '--base', 'HEAD', '--changed', 'docs/a.md', '--run'])
    assert affected.main(root=tmp_path) == 0
