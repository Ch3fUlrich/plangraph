from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest
from git_identity import identity_env

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = '.github/workflows/tests.yml'
WORKFLOW = '''on: [pull_request, push]
jobs:
  ci-gate:
    runs-on: ubuntu-latest
    steps:
      - name: Gate
        run: python3 tools/ci_select.py gate
      - name: Commit trailers
        run: python3 base.py check
  pytest:
    runs-on: ubuntu-latest
    steps:
      - run: python3 -m pytest
'''
WORKFLOW_NO_CI_GATE = '''on: [pull_request, push]
jobs:
  pytest:
    runs-on: ubuntu-latest
    steps:
      - run: python3 -m pytest
'''


def _load_tool():
    spec = importlib.util.spec_from_file_location('gate_wiring_check', REPO_ROOT / 'tools' / 'gate_wiring_check.py')
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


gate_wiring_check = _load_tool()
main = gate_wiring_check.main
wiring_at = gate_wiring_check.wiring_at
has_gate_edit = gate_wiring_check.has_gate_edit


def _run(repo: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=repo, check=True, capture_output=True, text=True,
                          env=env or identity_env('ci', 'ci@example.invalid'))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / 'repo'
    path.mkdir()
    _run(path, 'git', 'init', '-q')
    return path


def _commit(repo: Path, msg: str, files: dict[str, str] | None = None) -> str:
    if files:
        for name, content in files.items():
            target = repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
    _run(repo, 'git', 'add', '-A')
    _run(repo, 'git', 'commit', '-q', '--allow-empty', '-m', msg)
    result = _run(repo, 'git', 'rev-parse', 'HEAD')
    return result.stdout.strip()


def test_unchanged_wiring(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    base = _commit(repo, 'base', {WORKFLOW_PATH: WORKFLOW})
    head = _commit(repo, 'edit pytest only', {WORKFLOW_PATH: WORKFLOW.replace('python3 -m pytest', 'python3 -m pytest -q')})
    monkeypatch.chdir(repo)
    assert main(['--base', base, '--head', head]) == 0
    captured = capsys.readouterr()
    assert 'gate wiring unchanged' in captured.out


def test_gate_change_without_gate_edit(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    base = _commit(repo, 'base', {WORKFLOW_PATH: WORKFLOW})
    changed = WORKFLOW.replace('python3 tools/ci_select.py gate', 'python3 tools/ci_select.py gate || true')
    head = _commit(repo, 'change gate', {WORKFLOW_PATH: changed})
    assert has_gate_edit(base, head, cwd=repo) is False
    monkeypatch.chdir(repo)
    assert main(['--base', base, '--head', head]) == 1
    captured = capsys.readouterr()
    assert 'WITHOUT Gate-Edit' in captured.err
    assert '|| true' in captured.out


def test_gate_change_with_gate_edit(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    base = _commit(repo, 'base', {WORKFLOW_PATH: WORKFLOW})
    changed = WORKFLOW.replace('python3 tools/ci_select.py gate', 'python3 tools/ci_select.py gate || true')
    head = _commit(repo, 'change gate\n\nGate-Edit: D-2209\n', {WORKFLOW_PATH: changed})
    assert has_gate_edit(base, head, cwd=repo) is True
    monkeypatch.chdir(repo)
    assert main(['--base', base, '--head', head]) == 0
    captured = capsys.readouterr()
    assert 'WITH Gate-Edit' in captured.out


def test_gate_edit_leading_space_refused(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    base = _commit(repo, 'base', {WORKFLOW_PATH: WORKFLOW})
    changed = WORKFLOW.replace('python3 tools/ci_select.py gate', 'python3 tools/ci_select.py gate || true')
    head = _commit(repo, 'change gate\n\n Gate-Edit: D-2209\n', {WORKFLOW_PATH: changed})
    assert has_gate_edit(base, head, cwd=repo) is False
    monkeypatch.chdir(repo)
    assert main(['--base', base, '--head', head]) == 1
    captured = capsys.readouterr()
    assert 'WITHOUT Gate-Edit' in captured.err


def test_gate_edit_bad_id_refused(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    base = _commit(repo, 'base', {WORKFLOW_PATH: WORKFLOW})
    changed = WORKFLOW.replace('python3 tools/ci_select.py gate', 'python3 tools/ci_select.py gate || true')
    head = _commit(repo, 'change gate\n\nGate-Edit: D-x\n', {WORKFLOW_PATH: changed})
    assert has_gate_edit(base, head, cwd=repo) is False
    monkeypatch.chdir(repo)
    assert main(['--base', base, '--head', head]) == 1
    captured = capsys.readouterr()
    assert 'WITHOUT Gate-Edit' in captured.err


def test_missing_ci_gate_at_head_refused(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    base = _commit(repo, 'base', {WORKFLOW_PATH: WORKFLOW})
    head = _commit(repo, 'remove ci-gate\n\nGate-Edit: D-2209\n', {WORKFLOW_PATH: WORKFLOW_NO_CI_GATE})
    monkeypatch.chdir(repo)
    assert main(['--base', base, '--head', head]) == 1
    captured = capsys.readouterr()
    assert 'missing at head' in captured.err


def test_triggers_changed_without_gate_edit(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    base = _commit(repo, 'base', {WORKFLOW_PATH: WORKFLOW})
    changed = WORKFLOW.replace('on: [pull_request, push]', 'on: [push]')
    head = _commit(repo, 'drop pull_request', {WORKFLOW_PATH: changed})
    monkeypatch.chdir(repo)
    assert main(['--base', base, '--head', head]) == 1
    captured = capsys.readouterr()
    assert 'WITHOUT Gate-Edit' in captured.err


def test_continue_on_error_changed_without_gate_edit(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    base = _commit(repo, 'base', {WORKFLOW_PATH: WORKFLOW})
    changed = WORKFLOW.replace(
        '      - name: Gate\n        run: python3 tools/ci_select.py gate',
        '      - name: Gate\n        continue-on-error: true\n        run: python3 tools/ci_select.py gate',
    )
    head = _commit(repo, 'continue on error', {WORKFLOW_PATH: changed})
    monkeypatch.chdir(repo)
    assert main(['--base', base, '--head', head]) == 1
    captured = capsys.readouterr()
    assert 'WITHOUT Gate-Edit' in captured.err


def test_missing_ci_gate_at_base(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = _commit(repo, 'base without ci-gate', {WORKFLOW_PATH: WORKFLOW_NO_CI_GATE})
    head = _commit(repo, 'add ci-gate', {WORKFLOW_PATH: WORKFLOW})
    monkeypatch.chdir(repo)
    assert main(['--base', base, '--head', head]) == 2


def test_bad_revision(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    base = _commit(repo, 'base', {WORKFLOW_PATH: WORKFLOW})
    monkeypatch.chdir(repo)
    assert main(['--base', 'does-not-exist', '--head', base]) == 2
    captured = capsys.readouterr()
    assert captured.err


def test_real_repo_workflow_parses() -> None:
    wiring = wiring_at('HEAD', '.github/workflows/tests.yml', 'ci-gate', cwd=REPO_ROOT)
    assert isinstance(wiring, dict)
    assert 'steps' in wiring['job']


def test_gate_edit_must_be_on_every_workflow_commit(repo, monkeypatch, capsys):
    base = _commit(repo, 'base', {WORKFLOW_PATH: WORKFLOW})
    first = WORKFLOW.replace('python3 tools/ci_select.py gate', 'python3 tools/ci_select.py gate -v')
    _commit(repo, 'a\n\nGate-Edit: D-2209\n', {WORKFLOW_PATH: first})
    second = first.replace('-v', '|| true')
    head = _commit(repo, 'b', {WORKFLOW_PATH: second})
    monkeypatch.chdir(repo)
    assert main(['--base', base, '--head', head]) == 1
    captured = capsys.readouterr()
    assert 'WITHOUT Gate-Edit' in captured.err


def test_gate_edit_on_every_workflow_commit_passes(repo, monkeypatch, capsys):
    base = _commit(repo, 'base', {WORKFLOW_PATH: WORKFLOW})
    first = WORKFLOW.replace('python3 tools/ci_select.py gate', 'python3 tools/ci_select.py gate -v')
    _commit(repo, 'a\n\nGate-Edit: D-2209\n', {WORKFLOW_PATH: first})
    second = first.replace('-v', '|| true')
    head = _commit(repo, 'b\n\nGate-Edit: D-2209\n', {WORKFLOW_PATH: second})
    monkeypatch.chdir(repo)
    assert main(['--base', base, '--head', head]) == 0
    captured = capsys.readouterr()
    assert 'WITH Gate-Edit' in captured.out

def test_gate_edit_in_body_does_not_count(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    base = _commit(repo, 'base', {WORKFLOW_PATH: WORKFLOW})
    changed = WORKFLOW.replace('python3 tools/ci_select.py gate', 'python3 tools/ci_select.py gate || true')
    head = _commit(repo, 'x\n\nGate-Edit: D-2209\n\nmore prose after\n', {WORKFLOW_PATH: changed})
    monkeypatch.chdir(repo)
    assert main(['--base', base, '--head', head]) == 1
    captured = capsys.readouterr()
    assert 'WITHOUT Gate-Edit' in captured.err


def test_gate_edit_with_co_author_paragraph_counts(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    base = _commit(repo, 'base', {WORKFLOW_PATH: WORKFLOW})
    changed = WORKFLOW.replace('python3 tools/ci_select.py gate', 'python3 tools/ci_select.py gate || true')
    head = _commit(repo, 'x\n\nGate-Edit: D-2209\n\nCo-Authored-By: A B <a@example.invalid>\n', {WORKFLOW_PATH: changed})
    monkeypatch.chdir(repo)
    assert main(['--base', base, '--head', head]) == 0
    captured = capsys.readouterr()
    assert 'WITH Gate-Edit' in captured.out


def test_control_char_in_message_does_not_hide_commit(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    base = _commit(repo, 'base', {WORKFLOW_PATH: WORKFLOW})
    changed = WORKFLOW.replace('python3 tools/ci_select.py gate', 'python3 tools/ci_select.py gate || false')
    _commit(repo, 'a\x01b\n\nGate-Edit: D-2209\n', {WORKFLOW_PATH: changed})
    changed = WORKFLOW.replace('python3 tools/ci_select.py gate', 'python3 tools/ci_select.py gate || exit 1')
    head = _commit(repo, 'b', {WORKFLOW_PATH: changed})
    monkeypatch.chdir(repo)
    assert main(['--base', base, '--head', head]) == 1
    captured = capsys.readouterr()
    assert 'WITHOUT Gate-Edit' in captured.err


def test_gate_edit_missing_on_earlier_commit_refused(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    base = _commit(repo, 'base', {WORKFLOW_PATH: WORKFLOW})
    first = WORKFLOW.replace('python3 tools/ci_select.py gate', 'python3 tools/ci_select.py gate || true')
    first_sha = _commit(repo, 'a', {WORKFLOW_PATH: first})
    second = first.replace('python3 tools/ci_select.py gate || true', 'python3 tools/ci_select.py gate || true -v')
    second_sha = _commit(repo, 'b\n\nGate-Edit: D-2209\n', {WORKFLOW_PATH: second})
    monkeypatch.chdir(repo)
    assert main(['--base', base, '--head', second_sha]) == 1
    captured = capsys.readouterr()
    assert 'WITHOUT Gate-Edit' in captured.err
    assert first_sha in captured.err

def test_control_char_on_offending_commit_does_not_hide_it(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    base = _commit(repo, 'base', {WORKFLOW_PATH: WORKFLOW})
    first = WORKFLOW.replace('python3 tools/ci_select.py gate', 'python3 tools/ci_select.py gate || false')
    _commit(repo, 'a\n\nGate-Edit: D-2209\n', {WORKFLOW_PATH: first})
    second = first.replace('python3 tools/ci_select.py gate || false', 'python3 tools/ci_select.py gate || false || exit 1')
    second_sha = _commit(repo, 'b\x01c', {WORKFLOW_PATH: second})
    monkeypatch.chdir(repo)
    assert main(['--base', base, '--head', second_sha]) == 1
    captured = capsys.readouterr()
    assert 'WITHOUT Gate-Edit' in captured.err
    assert second_sha in captured.err