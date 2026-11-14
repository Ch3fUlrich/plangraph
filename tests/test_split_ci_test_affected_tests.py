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
