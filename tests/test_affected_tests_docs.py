import importlib.util
from pathlib import Path

import pytest


def selector():
    spec = importlib.util.spec_from_file_location(
        "affected_tests", Path(__file__).resolve().parents[1] / "tools/affected_tests.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _repo(tmp_path, extra=None):
    files = {"src/pkg/core.py": "VALUE = 1", "tests/test_core.py": "from pkg import core",
             "changelog.d/FRAG.md": "x", "CHANGELOG.md": "x", "notes/readme.md": "x",
             "notes/ideas.txt": "x", "notes/tool.sh": "x", **(extra or {})}
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    import subprocess
    for cmd in (["init", "-q"], ["add", "-A"]):
        subprocess.run(["git", "-C", str(tmp_path), *cmd], check=True)


@pytest.mark.parametrize("path", ["changelog.d/FRAG.md", "CHANGELOG.md", "notes/readme.md",
                                  "docs/x.md", "changelog.d/README.md"])
def test_docs_and_changelog_fragments_select_no_tests(tmp_path, path):
    _repo(tmp_path)
    assert selector().select_tests(tmp_path, [path])["tests"] == []


@pytest.mark.parametrize("path", ["notes/ideas.txt", "changelog.d/tool.py", "changelog.d/x.toml"])
def test_text_assets_and_non_fragments_are_not_skipped_as_docs(tmp_path, path):
    smoke = "tests/test_affected_tests.py"
    _repo(tmp_path, {smoke: "", path: "x"})
    assert selector().select_tests(tmp_path, [path])["tests"] == [smoke]


def test_unmapped_code_selects_bounded_smoke_not_full_suite(tmp_path):
    named = {f"tests/test_near{i}.py": "# deep/dir" for i in range(20)}
    _repo(tmp_path, {"deep/dir/x.py": "", "tests/test_affected_tests.py": "", **named})
    plan = selector().select_tests(tmp_path, ["deep/dir/x.py"])["tests"]
    assert plan != ["tests"] and len(plan) <= selector().SMOKE_LIMIT + 1
    assert "tests/test_affected_tests.py" in plan and "tests/test_near0.py" in plan


@pytest.mark.parametrize("path", ["pyproject.toml", "tests/conftest.py", "requirements-ci.txt"])
def test_suite_config_and_shared_fixtures_still_run_full_suite(tmp_path, path):
    _repo(tmp_path)
    assert selector().select_tests(tmp_path, [path])["tests"] == ["tests"]


@pytest.mark.parametrize("changed,expected", [
    (["docs/conf.py"], ["tests/test_affected_tests.py"]),
    (["status/x.toml"], ["tests/test_affected_tests.py"]),
    (["docs/requirements.txt"], ["tests"]),
    (["docs/x.md"], []),
    (["docs/img/a.png", "status/y.rst"], []),
])
def test_docs_status_only_documentation_files_skip(tmp_path, changed, expected):
    _repo(tmp_path, {
        "docs/conf.py": "x = 1", "docs/requirements.txt": "sphinx", "docs/x.md": "# x",
        "docs/img/a.png": "png", "status/x.toml": "a = 1", "status/y.rst": "y",
        "tests/test_affected_tests.py": "",
    })
    assert selector().select_tests(tmp_path, changed)["tests"] == expected

@pytest.mark.parametrize('asset', ['tests/shared.py', 'tests/fixtures/capture.json'])
def test_local_shared_asset_selects_its_consumers(tmp_path, asset):
    _repo(tmp_path, {asset: '', 'tests/test_consumer.py':
                    'import shared\n# capture.json', 'tests/test_unrelated.py': ''})
    assert selector().select_tests(tmp_path, [asset], local=True)['tests'] == ['tests/test_consumer.py']


def test_local_run_refuses_full_suite_fallback(tmp_path, monkeypatch, capsys):
    module = selector()
    _repo(tmp_path)
    monkeypatch.setattr('sys.argv', ['affected', '--base', 'HEAD', '--run', '--local', '--changed', 'pyproject.toml'])
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **kw: pytest.fail('full suite launched'))
    assert module.main(tmp_path) == 2
    assert 'full suite' in capsys.readouterr().err


@pytest.mark.parametrize('asset', ['tests/shared.py', 'tests/fixtures/capture.json'])
def test_local_file_only_mode_keeps_shared_asset_consumers(tmp_path, asset):
    _repo(tmp_path, {asset: '', 'tests/test_consumer.py':
                    'import shared\n# capture.json', 'tests/test_unrelated.py': ''})
    plan = selector().select_tests(tmp_path, [asset], local=True, files_only=True)
    assert plan['tests'] == ['tests/test_consumer.py']
